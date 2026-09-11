import io
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from gas import create_app
from gas.db import SCHEMA, connection, initialize
from gas.text_readings import extract_text_reading
from test_app import AUTH, API_AUTH, PASSWORD, TOKEN, image_bytes


SAMPLE = (Path(__file__).parent / 'fixtures' / 'iphone_ocr.txt').read_text()


class TextParsingTest(unittest.TestCase):
    def test_example_typographical_variants_and_month_errors(self):
        for reading in ('195kWh', '195 KWH', '195 k W h', '195kWn', '195kVvh', '195kW'):
            with self.subTest(reading=reading):
                text = SAMPLE.replace('195kWh', reading).replace('August 2026', 'Augusi 2026')
                values, warning = extract_text_reading(text.replace('\n', '\r\n'))
                self.assertEqual(values, ['195'])
                self.assertIn('Monat', warning)

    def test_zero_decimals_and_thousands(self):
        for reading, expected in [('0kWh', '0'), ('3.400,5kWh', '3400.5'), ('195.5kWh', '195.5')]:
            self.assertEqual(extract_text_reading(SAMPLE.replace('195kWh', reading))[0], [expected])

    def test_isolated_number_without_unit_uses_layout_and_warns(self):
        values, warning = extract_text_reading(SAMPLE.replace('195kWh', '195'))
        self.assertEqual(values, ['195'])
        self.assertIn('Einheit ist unklar', warning)
        self.assertEqual(extract_text_reading('Gasverbrauch\n\n195\n\nAugust 2026')[0], [])

    def test_never_joins_current_month_number_to_unit_on_next_line(self):
        for replacement in ('', 'unlesbar', '195MWh', '195 Liter', '195xyz'):
            for current in ('94\nKWh', '94\n\n\nKWh'):
                with self.subTest(replacement=replacement, current=current):
                    self.assertEqual(extract_text_reading(SAMPLE.replace('195kWh', replacement).replace('94\nKWh', current))[0], [])

    def test_ambiguous_inline_values_stay_ambiguous_and_title_is_required(self):
        values, warning = extract_text_reading(SAMPLE.replace('94\nKWh', '94kWh'))
        self.assertEqual(values, ['195', '94'])
        self.assertIn('Kein eindeutiger', warning)
        self.assertEqual(extract_text_reading(SAMPLE.replace('Gasverbrauch', 'Andere Anzeige'))[0], [])
        self.assertEqual(extract_text_reading('Gasverbrauch\n195kWn\n94\nKWh')[0], [])


class TextUploadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(dict(TESTING=True, DATA_DIR=self.temp.name, APP_PASSWORD=PASSWORD,
            UPLOAD_TOKEN=TOKEN, SECRET_KEY='test-secret-123456789', TODAY=date(2026, 9, 11)))
        self.client = self.app.test_client()
        self.path = self.app.config['DATABASE']
        with connection(self.path) as db:
            db.execute("INSERT INTO settings VALUES(1,'6520','6.57','1000','2026-01')")

    def upload(self, text=SAMPLE, **kwargs):
        return self.client.post('/api/uploads/text', headers=API_AUTH, data=text.encode('utf-8'),
                                content_type='text/plain; charset=utf-8', **kwargs)

    def post_form(self, url, data):
        with self.client.session_transaction() as session:
            session['csrf'] = 'test-csrf-token'
        return self.client.post(url, data={**data, 'csrf': 'test-csrf-token'}, headers=AUTH)

    def test_plain_text_draft_confirm_and_repeat_without_photo_or_tesseract(self):
        with patch('gas.recognize') as tesseract:
            response = self.upload()
            tesseract.assert_not_called()
        self.assertEqual(response.status_code, 201)
        data = response.json
        self.assertEqual((data['month'], data['source_type'], data['candidates_kwh']), ('2026-08', 'text', ['195']))
        self.assertFalse(data['photo_available'])
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT ocr_text FROM uploads').fetchone()[0], SAMPLE)
        self.assertEqual(list((Path(self.temp.name) / 'photos').iterdir()), [])
        self.assertEqual(self.client.get(data['review_url']).status_code, 401)
        page = self.client.get(data['review_url'], headers=AUTH)
        self.assertIn('Übertragener iPhone-Text', page.text)
        self.assertIn('value="195"', page.text)
        self.assertNotIn('Das Foto ist nicht verfügbar', page.text)
        self.assertNotIn('Texterkennung erneut starten', page.text)
        self.assertEqual(self.client.get('/photos/' + data['id'], headers=AUTH).status_code, 410)
        self.assertEqual(self.post_form(data['review_url'] + '/ocr', {}).status_code, 409)
        self.assertEqual(self.post_form(data['review_url'], {'action': 'confirm', 'month': '2026-08', 'kwh': '195'}).status_code, 302)
        self.assertEqual(self.upload().json['status'], 'confirmed')
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT kwh FROM consumption').fetchone()[0], '195')
        create_app(dict(self.app.config))  # Startup must not try to unlink the photo directory.
        self.assertTrue((Path(self.temp.name) / 'photos').is_dir())

    def test_form_month_override_and_subpath_guide(self):
        for stripped in (False, True):
            app = create_app({**self.app.config, 'APP_BASE_PATH': '/utilmanager'})
            client = app.test_client()
            response = client.post(('' if stripped else '/utilmanager') + '/api/uploads/text',
                headers=API_AUTH, data={'text': SAMPLE, 'month': '2026-07'})
            self.assertIn(response.status_code, (200, 201))
            self.assertEqual(response.json['month'], '2026-07')
            self.assertTrue(response.json['review_url'].startswith('/utilmanager/uploads/'))
            guide = client.get('/utilmanager/help/iphone-text-shortcut', headers=AUTH)
            self.assertEqual(guide.status_code, 200)
            self.assertIn('data-public-path="/utilmanager/api/uploads/text"', guide.text)
            self.assertIn('Text aus Bild extrahieren', guide.text)
            self.assertNotIn(TOKEN, guide.text)
            self.assertIn('iPhone-Text', client.get('/utilmanager/uploads', headers=AUTH).text)
            self.assertEqual(client.get('/utilmanager/help/iphone-text-shortcut').status_code, 401)

    def test_idempotency_cross_source_conflicts_and_explicit_raw_month(self):
        first = self.upload(query_string={'month': '2026-07'})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.upload(query_string={'month': '2026-07'}).json['id'], first.json['id'])
        with self.assertLogs(self.app.logger, level='WARNING'):
            self.assertEqual(self.upload().status_code, 409)
        response = self.client.post('/api/uploads/text', data={'text': SAMPLE+'\n', 'month': '2026-08'},
                                    headers={**API_AUTH, 'Idempotency-Key': 'shared-key'})
        self.assertEqual(response.status_code, 201)
        with self.assertLogs(self.app.logger, level='WARNING'):
            conflict = self.client.post('/api/uploads', headers={**API_AUTH, 'Idempotency-Key': 'shared-key'},
                data={'month': '2026-08', 'image': (io.BytesIO(image_bytes()), 'photo.jpg')})
            self.assertEqual(conflict.status_code, 409)

    def test_unrecognized_text_is_reviewable_and_escaped_then_rejectable(self):
        data = self.upload('Gasverbrauch\n\n<script>alert(1)</script>\n94\nKWh').json
        self.assertEqual(data['candidates_kwh'], [])
        page = self.client.get(data['review_url'], headers=AUTH)
        self.assertIn('&lt;script&gt;', page.text)
        self.assertNotIn('<script>alert', page.text)
        self.assertEqual(self.post_form(data['review_url'], {'action': 'reject'}).status_code, 302)
        self.assertIn('Übertragener iPhone-Text', self.client.get(data['review_url'], headers=AUTH).text)
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 0)

    def test_invalid_requests_are_bounded_and_logged_without_their_body(self):
        with self.assertLogs(self.app.logger, level='WARNING') as logs:
            cases = [self.client.post('/api/uploads/text', data='private-body'),
                     self.upload(' '), self.upload('x'*16001), self.upload(query_string={'month': '2026-09'}),
                     self.client.post('/api/uploads/text', headers=API_AUTH, data=b'\xff', content_type='text/plain'),
                     self.client.post('/api/uploads/text', headers=API_AUTH, data=b'x'*65537, content_type='text/plain'),
                     self.client.post('/api/uploads/text', headers=API_AUTH, json={'text': SAMPLE}),
                     self.client.post('/api/uploads/text', headers=API_AUTH, data={'text': SAMPLE, 'file': (io.BytesIO(b'x'), 'bad.txt')})]
        self.assertEqual([r.status_code for r in cases], [401, 400, 400, 400, 400, 413, 415, 400])
        with connection(self.path) as db:
            failures = [dict(row) for row in db.execute('SELECT * FROM upload_failures')]
            self.assertEqual(len(failures), len(cases))
            self.assertTrue(all(row['source'] == 'iPhone / Text' for row in failures))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0], 0)
        self.assertNotIn('private-body', str(failures) + str(logs.output))
        self.assertIn('64 KiB', cases[5].json['error'])

    def test_backup_and_restore_text_with_pending_photo(self):
        text = self.upload().json
        with patch('gas.recognize', return_value=('195 kWh', ['195'], '')):
            photo = self.client.post('/api/uploads', headers=API_AUTH,
                data={'image': (io.BytesIO(image_bytes()), 'photo.jpg')}).json
        with tempfile.TemporaryDirectory() as parent:
            destination = Path(parent) / 'backup'
            result = self.app.test_cli_runner().invoke(args=['backup', str(destination)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual([p.name for p in (destination / 'photos').iterdir()], [photo['id'] + '.jpg'])
            restored = create_app({**self.app.config, 'DATA_DIR': str(destination)})
            self.assertEqual(restored.test_client().get(text['review_url'], headers=AUTH).status_code, 200)

    def test_version_two_database_upgrades_existing_photo_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            legacy = SCHEMA.replace(",\n source_type TEXT NOT NULL DEFAULT 'photo' CHECK(source_type IN ('photo','text'))", '')
            with sqlite3.connect(path) as db:
                db.executescript(legacy + '\nPRAGMA user_version=2;')
                db.execute("INSERT INTO uploads(id,sha256,month,filename,ocr_text,candidates,warning) VALUES('old','hash','2026-08','old.jpg','text','[]','')")
            initialize(path)
            initialize(path)
            with connection(path) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
                self.assertEqual(db.execute('SELECT source_type,filename FROM uploads').fetchone()[:], ('photo', 'old.jpg'))


if __name__ == '__main__':
    unittest.main()
