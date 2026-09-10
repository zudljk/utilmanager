import base64
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from gas import create_app
from gas.db import connection
from gas.domain import ValidationError, ledger, number, previous_month, quantity
from gas.importer import apply_import, read_ods
from gas.ocr import candidates_from_text, normalize_image, recognize

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'Gasverbrauch.ods'
PASSWORD = 'test-password-123456789'
TOKEN = 'test-upload-token-123456789'
AUTH = {'Authorization': 'Basic ' + base64.b64encode(f'admin:{PASSWORD}'.encode()).decode()}
API_AUTH = {'Authorization': 'Bearer ' + TOKEN}


def image_bytes():
    result = io.BytesIO()
    image = Image.new('RGB', (800, 180), 'white')
    font = ImageFont.load_default(size=70)
    ImageDraw.Draw(image).text((30, 45), '3400 kWh', font=font, fill='black')
    image.save(result, format='JPEG')
    return result.getvalue()


class AppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(dict(TESTING=True, DATA_DIR=self.temp.name, APP_PASSWORD=PASSWORD,
                                   UPLOAD_TOKEN=TOKEN, SECRET_KEY='test-secret-123456789', TODAY=date(2026, 9, 9)))
        self.client = self.app.test_client()
        self.path = self.app.config['DATABASE']

    def tearDown(self):
        self.temp.cleanup()

    def post(self, url, data=None, **kwargs):
        self.client.get('/', headers=AUTH)
        with self.client.session_transaction() as session:
            session['csrf'] = 'test-csrf-token'
        return self.client.post(url, data={**(data or {}), 'csrf': 'test-csrf-token'}, headers=AUTH, **kwargs)

    def setup_tank(self, start='2026-01', liters='1000'):
        response = self.post('/setup', dict(capacity_liters='6520', factor='6,57', opening_liters=liters, start_month=start))
        self.assertEqual(response.status_code, 302)

    def import_source(self):
        with connection(self.path) as db:
            return apply_import(db, read_ods(SOURCE.read_bytes()), SOURCE.name)

    def upload(self, payload=None, month='2026-08', key=None):
        return self.client.post('/api/uploads', headers={**API_AUTH, **({'Idempotency-Key': key} if key else {})},
                                data={'month': month, 'image': (io.BytesIO(payload or image_bytes()), 'display.jpg')})

    def test_auth_required_and_health_public(self):
        self.assertEqual(self.client.get('/').status_code, 401)
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.assertEqual(self.client.get('/api/uploads/nope', headers=AUTH).status_code, 401)
        self.assertEqual(self.client.get('/', headers=API_AUTH).status_code, 401)
        self.assertEqual(self.client.get('/', headers=AUTH).status_code, 200)

    def test_csrf_required(self):
        response = self.client.post('/setup', headers=AUTH, data={})
        self.assertEqual(response.status_code, 400)
        with connection(self.path) as db:
            self.assertIsNone(db.execute('SELECT * FROM settings').fetchone())

    def test_setup_validation(self):
        response = self.post('/setup', dict(capacity_liters='6520', factor='0', opening_liters='100', start_month='2026-01'))
        self.assertEqual(response.status_code, 400)
        self.setup_tank()
        self.assertEqual(self.client.get('/', headers=AUTH).status_code, 200)

    def test_real_ods_import_and_full_reconciliation(self):
        self.import_source()
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 81)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 6)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM deliveries WHERE unit_price IS NOT NULL').fetchone()[0], 0)
            rows = {r['month']: r for r in ledger(db, '2026-12')}
            last = rows['2026-07']
            self.assertEqual((last['rest'] * Decimal('6.57')).quantize(Decimal('.01')), Decimal('7268.89'))
            self.assertEqual(last['rest'].quantize(Decimal('.01')), Decimal('1106.38'))
            self.assertEqual(rows['2025-06']['kwh'], Decimal(0))
            self.assertIsNone(rows['2026-08']['kwh'])
            self.assertIsNone(rows['2026-08']['rest'])
            self.assertEqual(rows['2026-08']['forecast'].quantize(Decimal('.01')), Decimal('1053.71'))
            self.assertEqual(last['cost'].quantize(Decimal('.01')), Decimal('23.72'))
            self.assertEqual(rows['2020-02']['prior'], Decimal(4500))
        response = self.client.get('/?year=2026', headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertIn('1.106,38', response.text)
        self.assertIn('fehlt', response.text)

    def test_import_is_idempotent_and_refuses_existing_data(self):
        self.assertTrue(self.import_source())
        self.assertFalse(self.import_source())
        parsed = read_ods(SOURCE.read_bytes())
        parsed['sha256'] = 'changed'
        with connection(self.path) as db:
            with self.assertRaises(ValidationError):
                apply_import(db, parsed, SOURCE.name)
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 81)

    def test_import_detects_wrong_cached_balance(self):
        data = io.BytesIO()
        with zipfile.ZipFile(SOURCE) as src, zipfile.ZipFile(data, 'w') as dst:
            for name in src.namelist():
                contents = src.read(name)
                if name == 'content.xml':
                    contents = contents.replace(b'office:value="29160.92"', b'office:value="29161.92"', 1)
                dst.writestr(name, contents)
        with self.assertRaisesRegex(ValidationError, 'Bestandsrechnung'):
            read_ods(data.getvalue())

    def test_import_web_flow(self):
        response = self.post('/import', {'file': (io.BytesIO(SOURCE.read_bytes()), 'Gasverbrauch.ods')})
        self.assertEqual(response.status_code, 302)
        self.assertIn('81 Verbrauchswerte', self.client.get('/', headers=AUTH).text)

    def test_zero_missing_gap_and_correction(self):
        self.setup_tank(start='2026-06')
        self.assertEqual(self.post('/consumption/new', dict(month='2026-06', kwh='0')).status_code, 302)
        self.assertEqual(self.post('/consumption/new', dict(month='2026-08', kwh='657')).status_code, 302)
        with connection(self.path) as db:
            rows = ledger(db, '2026-08')
            self.assertEqual(rows[0]['rest'], Decimal('1000'))
            self.assertIsNone(rows[1]['rest'])
            self.assertIsNone(rows[2]['rest'])
        self.post('/consumption/new', dict(month='2026-07', kwh='657'))
        self.post('/consumption/2026-08/edit', dict(kwh='1314', revision='1'))
        with connection(self.path) as db:
            self.assertEqual(ledger(db, '2026-08')[-1]['rest'], Decimal('700'))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='update'").fetchone()[0], 1)
        self.assertEqual(self.post('/consumption/2026-08/edit', dict(kwh='10', revision='1')).status_code, 409)

    def test_duplicate_and_invalid_months_rejected(self):
        self.setup_tank()
        self.assertEqual(self.post('/consumption/new', dict(month='2026-08', kwh='100')).status_code, 302)
        self.assertEqual(self.post('/consumption/new', dict(month='2026-08', kwh='200')).status_code, 409)
        for month in ('2026-09', '2027-01', '2025-12', '2026-13'):
            self.assertEqual(self.post('/consumption/new', dict(month=month, kwh='100')).status_code, 400)
        for value in ('', '-1', 'nan', 'Infinity', '1,234.56'):
            self.assertEqual(self.post('/consumption/new', dict(month='2026-07', kwh=value)).status_code, 400)

    def test_manual_delivery_multiple_same_month_idempotent_and_edit(self):
        self.setup_tank(start='2026-08')
        self.post('/consumption/new', dict(month='2026-08', kwh='657'))
        first = dict(month='2026-08', delivery_date='2026-08-03', liters='500', unit_price='0,8', submission_id='a'*24)
        for _ in range(2):
            self.assertEqual(self.post('/deliveries/new', first).status_code, 302)
        self.assertEqual(self.post('/deliveries/new', {**first, 'liters':'900'}).status_code, 409)
        self.post('/deliveries/new', {**first, 'delivery_date':'2026-08-20', 'liters':'200', 'unit_price':'0,9', 'submission_id':'b'*24})
        with connection(self.path) as db:
            row = ledger(db, '2026-08')[0]
            self.assertEqual(row['rest'], Decimal('1600'))
            self.assertEqual(row['price'], Decimal('.9'))
            self.assertEqual(row['cost'], Decimal('90'))
            self.assertEqual(len(row['deliveries']), 2)
        self.assertEqual(self.post('/deliveries/2/edit', {**first, 'liters':'300', 'revision':'1'}).status_code, 302)
        self.assertEqual(self.post('/deliveries/2/edit', {**first, 'revision':'1'}).status_code, 409)

    def test_bad_delivery_dates(self):
        self.setup_tank()
        data = dict(month='2026-08', liters='500', submission_id='c'*24)
        for raw_date in ('2026-08-99', '2026-09-01'):
            self.assertEqual(self.post('/deliveries/new', {**data, 'delivery_date':raw_date}).status_code, 400)

    def test_delete_requires_confirmation_and_revision(self):
        self.setup_tank()
        self.post('/consumption/new', dict(month='2026-08', kwh='100'))
        self.assertEqual(self.post('/delete/consumption/2026-08', dict(revision='1')).status_code, 400)
        self.assertEqual(self.post('/delete/consumption/2026-08', dict(confirm='yes', revision='2')).status_code, 409)
        self.assertEqual(self.post('/delete/consumption/2026-08', dict(confirm='yes', revision='1')).status_code, 302)
        with connection(self.path) as db:
            self.assertIsNone(db.execute('SELECT * FROM consumption').fetchone())
            self.assertIsNotNone(db.execute("SELECT * FROM audit WHERE action='delete'").fetchone())

    @patch('gas.recognize', return_value=('3400 kWh', ['3400'], ''))
    def test_upload_pending_duplicate_confirm_and_delete(self, mock_ocr):
        self.setup_tank()
        result = self.upload(key='unique-photo')
        self.assertEqual(result.status_code, 201)
        data = result.get_json()
        self.assertEqual(data['status'], 'pending')
        self.assertTrue(data['photo_available'])
        photo = Path(self.temp.name) / 'photos' / (data['id'] + '.jpg')
        self.assertTrue(photo.exists())
        self.assertEqual(data['candidates_kwh'], ['3400'])
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 0)
        again = self.upload(key='unique-photo')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json['id'], data['id'])
        self.assertEqual(mock_ocr.call_count, 1)
        self.assertEqual(self.client.get(data['review_url'], headers=AUTH).status_code, 200)
        confirmed = self.post(data['review_url'], dict(action='confirm', month='2026-08', kwh='3401'))
        self.assertEqual(confirmed.status_code, 302)
        self.assertFalse(photo.exists())
        self.assertEqual(self.client.get('/photos/'+data['id'], headers=AUTH).status_code, 410)
        self.assertNotIn('class="display-photo"', self.client.get(data['review_url'], headers=AUTH).text)
        repeated = self.upload(key='unique-photo')
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.json['photo_available'])
        self.assertFalse(photo.exists())
        self.assertEqual(self.client.get('/api/uploads/'+data['id'], headers=API_AUTH).json['status'], 'confirmed')
        self.assertEqual(self.post(data['review_url'], dict(action='confirm', month='2026-08', kwh='3401')).status_code, 409)
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT kwh FROM consumption').fetchone()[0], '3401')
        self.post('/delete/consumption/2026-08', dict(confirm='yes', revision='1'))
        self.assertEqual(self.client.get('/api/uploads/'+data['id'], headers=API_AUTH).json['status'], 'rejected')

    @patch('gas.recognize', return_value=('', [], 'OCR fehlgeschlagen'))
    def test_upload_failure_manual_confirmation_and_rejection(self, _):
        self.setup_tank()
        result = self.upload()
        self.assertEqual(result.status_code, 201)
        self.assertIn('OCR fehlgeschlagen', self.client.get(result.json['review_url'], headers=AUTH).text)
        response = self.post(result.json['review_url'], dict(action='reject'))
        self.assertEqual(response.status_code, 302)
        self.assertFalse((Path(self.temp.name) / 'photos' / (result.json['id']+'.jpg')).exists())
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 0)

    def test_upload_invalid_and_oversized(self):
        self.setup_tank()
        self.assertEqual(self.upload(b'<html>not a photo</html>').status_code, 400)
        self.app.config['MAX_CONTENT_LENGTH'] = 200
        response = self.upload()
        self.assertEqual(response.status_code, 413)
        self.assertIn('error', response.json)

    @patch('gas.recognize', return_value=('3400 kWh', ['3400'], ''))
    def test_idempotency_conflicts_and_existing_reading(self, _):
        self.setup_tank()
        result = self.upload(key='key-1')
        self.assertEqual(self.upload(month='2026-07').status_code, 409)
        self.assertEqual(self.upload(image_bytes()+b' ', key='key-1').status_code, 409)
        self.post('/consumption/new', dict(month='2026-08', kwh='500'))
        self.assertEqual(self.post(result.json['review_url'], dict(action='confirm', month='2026-08', kwh='3400')).status_code, 409)
        self.assertEqual(self.client.get('/api/uploads/'+result.json['id'], headers=API_AUTH).json['status'], 'pending')
        self.assertTrue((Path(self.temp.name) / 'photos' / (result.json['id']+'.jpg')).exists())

    @patch('gas.recognize', return_value=('3400 kWh', ['3400'], ''))
    def test_failed_booking_preserves_photo_and_rolls_back(self, _):
        self.setup_tank()
        data = self.upload().json
        with patch('gas.audit', side_effect=sqlite3.IntegrityError('simulated transaction failure')):
            result = self.post(data['review_url'], dict(action='confirm', month='2026-08', kwh='3400'))
        self.assertEqual(result.status_code, 409)
        self.assertTrue((Path(self.temp.name) / 'photos' / (data['id']+'.jpg')).exists())
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT status FROM uploads').fetchone()[0], 'pending')

    @patch('gas.recognize', return_value=('3400 kWh', ['3400'], ''))
    def test_failed_photo_deletion_is_retried_at_start_pending_preserved(self, _):
        self.setup_tank()
        done = self.upload().json
        pending = self.upload(image_bytes()+b'other', month='2026-07').json
        with patch.object(Path, 'unlink', side_effect=PermissionError('simulated deletion failure')):
            with self.assertLogs(self.app.logger, level='ERROR'):
                result = self.post(done['review_url'], dict(action='confirm', month='2026-08', kwh='3400'))
        self.assertEqual(result.status_code, 302)
        self.assertIn('konnte noch nicht gelöscht werden', self.client.get('/', headers=AUTH).text)
        done_path = Path(self.temp.name) / 'photos' / (done['id']+'.jpg')
        pending_path = Path(self.temp.name) / 'photos' / (pending['id']+'.jpg')
        self.assertTrue(done_path.exists())
        create_app(dict(self.app.config))
        self.assertFalse(done_path.exists())
        self.assertTrue(pending_path.exists())
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT kwh FROM consumption').fetchone()[0], '3400')

    @patch('gas.recognize', return_value=('3400 kWh', ['3400'], ''))
    def test_backup_copies_only_pending_photos(self, _):
        self.setup_tank()
        done = self.upload().json
        self.post(done['review_url'], dict(action='confirm', month='2026-08', kwh='3400'))
        pending = self.upload(image_bytes()+b'other', month='2026-07').json
        with tempfile.TemporaryDirectory() as parent:
            destination = Path(parent)/'snapshot'
            result = self.app.test_cli_runner().invoke(args=['backup', str(destination)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual([p.name for p in (destination/'photos').iterdir()], [pending['id']+'.jpg'])
            with closing(sqlite3.connect(destination/'gas.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 1)

    @patch('gas.recognize', return_value=('3400 kWh', ['3400'], ''))
    def test_web_upload(self, _):
        self.setup_tank()
        response = self.post('/uploads/new', {'month':'2026-08', 'image':(io.BytesIO(image_bytes()), 'gas.jpg')})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/uploads/', response.location)

    @patch('gas.recognize', return_value=('3400 kWh', ['3400'], ''))
    def test_backup_and_restore_preserve_data_and_photos(self, _):
        self.import_source()
        self.upload()
        with tempfile.TemporaryDirectory() as parent:
            destination = Path(parent)/'snapshot'
            result = self.app.test_cli_runner().invoke(args=['backup', str(destination)])
            self.assertEqual(result.exit_code, 0, result.output)
            with closing(sqlite3.connect(destination/'gas.sqlite3')) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 81)
                photo = db.execute('SELECT filename FROM uploads').fetchone()[0]
                self.assertTrue((destination/'photos'/photo).is_file())
            restored = create_app(dict(TESTING=True, DATA_DIR=str(destination), APP_PASSWORD=PASSWORD,
                                      UPLOAD_TOKEN=TOKEN, SECRET_KEY='test-secret-123456789', TODAY=date(2026,9,9)))
            self.assertEqual(restored.test_client().get('/', headers=AUTH).status_code, 200)

    def test_all_pages_and_security_headers(self):
        self.import_source()
        for path in ('/', '/?year=2019', '/?year=2027', '/consumption/new', '/consumption/2026-07/edit',
                     '/deliveries/new', '/deliveries/1/edit', '/uploads', '/uploads/new', '/history'):
            response = self.client.get(path, headers=AUTH)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(self.client.get('/?year=garbage', headers=AUTH).status_code, 400)


class UnitTest(unittest.TestCase):
    def test_numbers_and_year_boundary(self):
        self.assertEqual(number('0'), Decimal(0))
        self.assertEqual(number('6,57'), Decimal('6.57'))
        self.assertEqual(quantity(Decimal('3400.5')), '3.400,5')
        self.assertEqual(quantity(Decimal('0')), '0')
        self.assertEqual(previous_month(date(2026, 1, 1)), '2025-12')
        self.assertIsNone(number('', optional=True))
        with self.assertRaises(ValidationError):
            number('-2')

    def test_ocr_parsing(self):
        self.assertEqual(candidates_from_text('Verbrauch 3.400 kWh'), ['3400'])
        self.assertEqual(candidates_from_text('Verbrauch 3.400,5 kWh'), ['3400.5'])
        self.assertEqual(candidates_from_text('Heizung 3000 kWh\nWarmwasser 400 kWh'), ['3000','400'])
        self.assertEqual(candidates_from_text('0 kWh'), ['0'])
        self.assertEqual(candidates_from_text('2026-08\n3400\nTemperatur 42'), ['3400'])

    def test_image_validation_and_real_tesseract(self):
        import shutil
        jpeg = normalize_image(image_bytes())
        self.assertTrue(jpeg.startswith(b'\xff\xd8'))
        if not shutil.which('tesseract'):
            self.skipTest('Tesseract nicht lokal installiert')
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent)/'sample.jpg'
            path.write_bytes(jpeg)
            text, values, warning = recognize(path)
            self.assertIn('3400', values, (text, warning))


if __name__ == '__main__':
    unittest.main()
