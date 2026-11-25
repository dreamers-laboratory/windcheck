"""Magnetic bearings are display metadata, never crosswind inputs."""

import copy
import math
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import metar
from scripts.update_runway_headings import magnetic_heading


class MagneticHeadingTests(unittest.TestCase):
    def test_variation_and_wrap(self):
        for args, expected in [((224, 12, "E"), 212),
                               ((224, 12, "W"), 236),
                               ((5, 12, "E"), 353),
                               ((360, 0, ""), 360),
                               ((12, 12, "E"), 360)]:
            self.assertEqual(magnetic_heading(*args), expected)

    def test_missing_or_invalid_values_are_not_invented(self):
        for args in [("", 12, "E"), (224, "", "E"), (224, 12, ""),
                     (None, 12, "E"), (math.nan, 12, "E"),
                     (224, math.inf, "E"), (361, 12, "E")]:
            self.assertIsNone(magnetic_heading(*args), args)

    def test_published_snapshot(self):
        data = metar._runway_display_data()
        self.assertEqual(data["cycle"], "2026-10-29")
        self.assertGreater(len(data["airports"]), 4000)
        self.assertEqual(data["airports"]["KSMO"]["runways"],
                         {"03": 32, "21": 212})
        self.assertEqual(data["airports"]["L22"]["runways"]["06"], 56)

    def test_aliases_and_foreign_local_code_collision(self):
        self.assertEqual(metar._magnetic_runway_headings(
            {"iso_country": "US", "local_code": "L22"}, "KL22")["06"], 56)
        self.assertEqual(metar._magnetic_runway_headings(
            {"iso_country": "CA", "local_code": "L22"}, "FOREIGN"), {})


class RunwayApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windcheck-headings-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        db = patch.object(metar, "DB_PATH", Path(tmp.name) / "metar.db")
        db.start()
        self.addCleanup(db.stop)
        metar.db_init()
        with closing(metar.db_connect()) as conn, conn:
            conn.executemany("INSERT INTO meta_kv (k,v) VALUES (?,?)",
                             [("airports_loaded_at", "fixture"),
                              ("runways_loaded_at", "fixture")])
            conn.execute("INSERT INTO airports (ident,name,iso_country,local_code) "
                         "VALUES ('KSMO','Santa Monica','US','SMO')")
            conn.executemany("INSERT INTO runway_ends "
                             "(airport,ident,heading_deg,length_ft,surface,closed) "
                             "VALUES ('KSMO',?,?,3500,'ASPH',0)",
                             [("03", 45), ("21", 225), ("99", 100)])
            conn.execute("INSERT INTO metar "
                         "(airport,obs_time_utc,wind_dir,wind_speed_kt) "
                         "VALUES ('KSMO','2026-11-01T19:00:00Z',270,10)")

    def test_api_exposes_both_bearings_and_null_when_unknown(self):
        with metar.app.test_client() as client:
            response = client.get('/api/info?airport=KSMO')
        self.assertEqual(response.status_code, 200)
        ends = {r['ident']: r for r in response.get_json()['runways']}
        self.assertEqual(ends['21']['heading_magnetic_deg'], 212)
        self.assertEqual(ends['21']['heading_deg'], 225)
        self.assertIsNone(ends['99']['heading_magnetic_deg'])

    def test_stats_and_forecast_math_ignore_display_metadata(self):
        for mode in (225, 'auto'):
            enriched = metar.compute_stats('KSMO', mode, list(range(24)))
            with patch.object(metar, '_magnetic_runway_headings', return_value={}):
                plain = metar.compute_stats('KSMO', mode, list(range(24)))
            for result in (enriched, plain):
                for runway in result['runways']:
                    runway.pop('heading_magnetic_deg')
            self.assertEqual(enriched, plain)
        runways = metar.get_runways('KSMO')
        plain = copy.deepcopy(runways)
        for runway in plain:
            runway.pop('heading_magnetic_deg')
        for direction in range(360):
            self.assertEqual(metar._forecast_xwind(runways, direction, 10),
                             metar._forecast_xwind(plain, direction, 10))
        fixed = metar.compute_stats('KSMO', 225, list(range(24)))
        self.assertEqual(fixed['runway_heading'], 225)
        self.assertAlmostEqual(next(r for r in fixed['rows'] if r['obs_count'])
                               ['xwind']['mean'], 10 * math.sin(math.pi / 4))


if __name__ == '__main__':
    unittest.main()
