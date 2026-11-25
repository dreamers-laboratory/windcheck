"""Active runway end: least crosswind, ties go to the headwind end."""

import unittest

import metar

KMYF = [{"ident": "05", "heading_deg": 65}, {"ident": "10L", "heading_deg": 115},
        {"ident": "23", "heading_deg": 245}, {"ident": "28R", "heading_deg": 295}]


class RunwayEndTests(unittest.TestCase):
    def pick(self, wind_dir):
        return metar._pick_end_for_obs(KMYF, wind_dir)["ident"]

    def test_opposite_ends_go_to_the_headwind_end(self):
        # Before the fix these fell to float noise: 298 -> 10L, 248 -> 05.
        for wind_dir, want in [(298, "28R"), (349, "28R"), (115, "10L"), (130, "10L"),
                               (248, "23"), (245, "23"), (65, "05"), (80, "05")]:
            self.assertEqual(self.pick(wind_dir), want, wind_dir)

    def test_every_direction_picks_a_headwind_end(self):
        for wind_dir in range(0, 360):
            end = metar._pick_end_for_obs(KMYF, wind_dir)
            headwind = metar.math.cos(metar.math.radians(wind_dir - end["heading_deg"]))
            self.assertGreaterEqual(headwind, -1e-9, (wind_dir, end["ident"]))


if __name__ == "__main__":
    unittest.main()
