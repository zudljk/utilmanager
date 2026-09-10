import io
import re
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from flask import url_for
from gas import create_app
from gas.mount import normalize_base_path
from test_app import AUTH, API_AUTH, PASSWORD, TOKEN, image_bytes


class BasePathTest(unittest.TestCase):
    def test_mount_flows(self):
        for prefix in ('', '/', '/utilmanager', '/apps/utilmanager/'):
            for stripped in (False, True):
                with self.subTest(prefix=prefix, stripped=stripped), tempfile.TemporaryDirectory() as directory:
                    with patch.dict('os.environ', APP_BASE_PATH=prefix):
                        app = create_app(dict(TESTING=True, DATA_DIR=directory,
                            APP_PASSWORD=PASSWORD, UPLOAD_TOKEN=TOKEN,
                            SECRET_KEY='test-secret-123456789', TODAY=date(2026, 9, 9)))
                    base = prefix.rstrip('/')
                    client = app.test_client()
                    def get(path, **kwargs):
                        return client.get(('' if stripped else base) + path, **kwargs)
                    page = get('/', headers=AUTH)
                    self.assertEqual(page.status_code, 200)
                    for link in re.findall(r'(?:href|src|action)="([^"]+)"', page.text):
                        self.assertTrue(link.startswith(base + '/'), link)
                    self.assertIn(f'href="{base}/static/app.css"', page.text)
                    self.assertIn('Path=' + (base or '/'), page.headers['Set-Cookie'])
                    with get('/static/app.css', headers=AUTH) as stylesheet:
                        self.assertEqual(stylesheet.status_code, 200)
                    self.assertEqual(get('/healthz').status_code, 200)
                    self.assertEqual(client.get('/healthz').status_code, 200)
                    self.assertEqual(get('/').status_code, 401)
                    self.assertEqual(get('/api/uploads/missing', headers=AUTH).status_code, 401)
                    self.assertEqual(get('/api/uploads/missing', headers=API_AUTH).status_code, 404)
                    token = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
                    # A browser submits the generated external form URL.
                    response = client.post(base + '/setup', headers=AUTH, data=dict(
                        csrf=token, capacity_liters='6520', factor='6.57',
                        opening_liters='1000', start_month='2026-01'))
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.location, base + '/')
                    self.assertEqual(client.get(response.location, headers=AUTH).status_code, 200)
                    with patch('gas.recognize', return_value=('3400 kWh', ['3400'], '')):
                        upload = client.post(base + '/api/uploads', headers=API_AUTH,
                            data={'month': '2026-08', 'image': (io.BytesIO(image_bytes()), 'meter.jpg')})
                    self.assertEqual(upload.status_code, 201)
                    self.assertEqual(upload.json['review_url'], base + '/uploads/' + upload.json['id'])
                    review = client.get(upload.json['review_url'], headers=AUTH)
                    self.assertEqual(review.status_code, 200)
                    photo_url = base + '/photos/' + upload.json['id']
                    self.assertIn('src="' + photo_url + '"', review.text)
                    with client.get(photo_url, headers=AUTH) as photo:
                        self.assertEqual(photo.status_code, 200)
                    app.config['SERVER_NAME'] = 'example.test'
                    with app.app_context():
                        self.assertEqual(url_for('static', filename='app.css'),
                                         'http://example.test' + base + '/static/app.css')
                    if base:
                        response = client.get(base + '?year=2026', headers=AUTH)
                        self.assertEqual(response.status_code, 308)
                        self.assertTrue(response.location.endswith(base + '/?year=2026'))
                        # An already mounted WSGI request must not double the prefix.
                        response = client.get('/', headers=AUTH, environ_overrides={'SCRIPT_NAME': base})
                        self.assertIn(f'href="{base}/static/app.css"', response.text)

    def test_invalid_configuration(self):
        for value in ('utilmanager', '//host', '/a//b', '/a/../b', '/.',
                      '/a?b', '/a#b', '/a%2fb', '/a\\b', '/a\nb', 'https://host/a'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                normalize_base_path(value)
