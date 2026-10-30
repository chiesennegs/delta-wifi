"""Regression cases from independently observed hotspot access and unauthenticated Delta Wi-Fi."""
import json
from pathlib import Path
import unittest
from delta_wifi.connectivity import classify


class FlightCaptureTests(unittest.TestCase):
    def cases(self, prefix):
        captures = json.loads((Path(__file__).parent / 'fixtures' / 'flight_connectivity.json').read_text())
        return [case for case in captures if case['case'].startswith(prefix)]

    def test_known_working_hotspot_handles_one_timed_out_general_site(self):
        for case in self.cases('hotspot'):
            with self.subTest(case=case['case']):
                self.assertEqual(classify(case['probes']), (case['expected'], False))

    def test_known_unauthenticated_delta_never_looks_online_despite_google_204(self):
        for case in self.cases('delta-before-login'):
            with self.subTest(case=case['case']):
                self.assertEqual(classify(case['probes']), (case['expected'], False))
                self.assertNotEqual(classify(case['probes'])[0], 'online')
