import tempfile
import unittest
from datetime import date
from decimal import Decimal

from gas import create_app
from gas.analytics import chart_geometry, monthly_analysis
from gas.db import connection
from gas.domain import shift_month
from test_app import AUTH, PASSWORD, TOKEN


class AnalyticsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(dict(TESTING=True, DATA_DIR=self.temp.name,
            APP_PASSWORD=PASSWORD, UPLOAD_TOKEN=TOKEN,
            SECRET_KEY='test-secret-123456789', TODAY=date(2026, 10, 9)))
        self.path = self.app.config['DATABASE']

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, missing=None):
        with connection(self.path) as db:
            db.execute("INSERT INTO settings VALUES(1,'6520','2','3000','2025-10')")
            for i in range(12):
                month = shift_month('2025-10', i)
                if month != missing:
                    db.execute('INSERT INTO consumption(month,kwh) VALUES(?,?)', (month, str(i * 20)))
            db.execute("INSERT INTO deliveries(month,liters,submission_id) VALUES('2026-10','100','test')")

    def rows(self):
        with connection(self.path) as db:
            return monthly_analysis(db, self.app.config['TODAY'])

    def test_window_actuals_seasonal_forecast_and_deliveries(self):
        self.seed()
        rows = self.rows()
        self.assertEqual(len(rows), 24)
        self.assertEqual((rows[0]['month'], rows[-1]['month']), ('2025-10', '2027-09'))
        self.assertEqual(sum(r['projected'] for r in rows), 12)
        self.assertEqual(rows[0]['kwh'], Decimal(0))
        self.assertEqual(rows[11]['rest'], Decimal(2340))
        self.assertEqual(rows[12]['rest'], Decimal(2440))
        self.assertEqual(rows[-1]['rest'], Decimal(1780))
        for i in range(12):
            self.assertEqual(rows[i + 12]['kwh'], rows[i]['kwh'])
            self.assertEqual(rows[i + 12]['prior_month'], rows[i]['month'])

    def test_missing_history_is_not_estimated_or_zero_filled(self):
        self.seed(missing='2026-02')
        rows = self.rows()
        self.assertIsNone(rows[4]['kwh'])
        self.assertIsNone(rows[4]['rest'])
        self.assertIsNotNone(rows[5]['kwh'])
        self.assertIsNone(rows[5]['rest'])
        self.assertEqual(rows[12]['kwh'], Decimal(0))
        self.assertTrue(all(r['rest'] is None for r in rows[12:]))
        self.assertIsNone(rows[16]['kwh'])
        self.assertEqual(rows[17]['kwh'], Decimal(100))
        chart = chart_geometry(rows)
        self.assertEqual(len(chart['series'][0]['paths'][False]), 2)
        self.assertEqual(len(chart['series'][0]['paths'][True]), 2)

    def test_negative_stock_is_preserved_and_in_axis_range(self):
        self.seed()
        with connection(self.path) as db:
            db.execute("UPDATE settings SET opening_liters='1000'")
        rows = self.rows()
        self.assertEqual(rows[-1]['rest'], Decimal(-220))
        chart = chart_geometry(rows)
        axis = chart['ticks'][1]['values']
        self.assertLessEqual(axis[0]['value'], Decimal(-220))
        self.assertTrue(all(60 <= p['y'] <= 330 for p in chart['series'][1]['points']))

    def test_new_tank_and_year_boundary(self):
        with connection(self.path) as db:
            db.execute("INSERT INTO settings VALUES(1,'6520','2','3000','2026-10')")
        self.assertTrue(all(r['rest'] is None for r in self.rows()))
        self.app.config['TODAY'] = date(2027, 1, 1)
        rows = self.rows()
        self.assertEqual((rows[0]['month'], rows[-1]['month']), ('2026-01', '2027-12'))

    def test_page_authentication_setup_redirect_and_mount(self):
        client = self.app.test_client()
        self.assertEqual(client.get('/analytics').status_code, 401)
        self.assertEqual(client.get('/analytics', headers=AUTH).location, '/')
        self.seed()
        for prefix in ('', '/utilmanager'):
            app = create_app({**self.app.config, 'APP_BASE_PATH': prefix})
            response = app.test_client().get(prefix + '/analytics', headers=AUTH)
            self.assertEqual(response.status_code, 200)
            self.assertIn('Content-Security-Policy', response.headers)
            self.assertIn(f'href="{prefix}/analytics"', response.text)
            self.assertIn('viewBox="0 0 1100 430"', response.text)
            self.assertIn('10/2025 bis 09/2027', response.text)
            self.assertIn('2.440,00', response.text)
            self.assertNotIn('<script', response.text)


if __name__ == '__main__':
    unittest.main()
