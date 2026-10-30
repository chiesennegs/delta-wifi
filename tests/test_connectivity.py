import io
import json
import ssl
import unittest
import urllib.error
from unittest.mock import Mock, patch
from delta_wifi import cli, connectivity
from delta_wifi.monitor import Timeline
from delta_wifi.recovery import RetryPolicy
from test_features import state


def results(passes=(), redirects=()):
    return [dict(name=endpoint.name, purpose=endpoint.purpose,
                 result='pass' if endpoint.name in passes else 'portal-redirect' if endpoint.name in redirects else 'timeout')
            for endpoint in connectivity.ENDPOINTS]


class EvidenceTests(unittest.TestCase):
    def test_one_allowlisted_check_is_not_general_access(self):
        self.assertEqual(connectivity.classify(results(('apple-check',))), ('limited', False))

    def test_both_os_checks_still_do_not_prove_access(self):
        self.assertEqual(connectivity.classify(results(('apple-check', 'google-check'))), ('limited', False))

    def test_two_general_sites_confirm_access(self):
        self.assertEqual(connectivity.classify(results(('cloudflare-web', 'wikipedia-web'))), ('online', False))

    def test_redirect_vetoes_other_positive_results(self):
        self.assertEqual(connectivity.classify(results(('cloudflare-web', 'wikipedia-web'), ('apple-check',))), ('captive', True))

    def test_hotspot_pattern_with_failed_cloudflare_is_online_when_python_responds(self):
        sample = results(('apple-check', 'google-check', 'wikipedia-web', 'python-web'))
        self.assertEqual(connectivity.classify(sample), ('online', False))

    def test_only_google_success_on_delta_remains_limited(self):
        self.assertEqual(connectivity.classify(results(('google-check',))), ('limited', False))

    def test_normal_external_redirect_is_not_proof_of_a_portal(self):
        sample = results()
        sample[3]['result'] = 'external-redirect'
        self.assertEqual(connectivity.classify(sample), ('unavailable', False))

    def test_no_positive_evidence_is_unavailable(self):
        self.assertEqual(connectivity.classify(results()), ('unavailable', False))

    def test_partial_access_never_ends_outage_or_sends_recovery_alert(self):
        notify, save = Mock(), Mock()
        timeline = Timeline(record=Mock(), notify=notify, save_online=save)
        timeline.observe(state(), 0)
        for value in ('limited', 'degraded', 'captive'):
            timeline.observe(state(value), 100)
        self.assertEqual(timeline.outage_since, 0)
        self.assertIsNone(timeline.last_online)
        notify.assert_not_called()
        save.assert_not_called()
        action = timeline.runtime(state('limited'), RetryPolicy(), 100, None)['action']
        self.assertNotIn('Internet access works', action)

    @patch.object(cli.network, 'diagnostics', return_value={})
    @patch.object(cli.connectivity, 'sample', return_value={'internet': 'online', 'captive_evidence': False, 'probes': []})
    @patch.object(cli, 'association', side_effect=[(cli.SSID, '172.19.0.81', None), ('TestHotspot', '192.168.1.2', None)])
    def test_report_invalidates_sample_spanning_network_change(self, *mocks):
        report = cli.connectivity_report()
        self.assertTrue(report['network_changed'])
        self.assertEqual(report['internet'], 'network-changed')
        self.assertNotIn('172.19.0.81', json.dumps(report))


class RequestTests(unittest.TestCase):
    def opener(self, body, status=200):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = status
        response.read.return_value = body
        opener = Mock()
        opener.open.return_value = response
        return opener

    def test_fresh_requests_validate_content_and_discard_body(self):
        endpoint = connectivity.ENDPOINTS[2]
        opener = self.opener(b'h=www.cloudflare.com\nip=private-address\nvisit_scheme=https\n')
        with patch.object(connectivity.urllib.request, 'build_opener', return_value=opener):
            first = connectivity.check(endpoint)
            connectivity.check(endpoint)
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertNotEqual(requests[0].full_url, requests[1].full_url)
        self.assertIn('no-cache', requests[0].get_header('Cache-control'))
        self.assertEqual(first['result'], 'pass')
        self.assertNotIn('private-address', json.dumps(first))
        self.assertNotIn('delta_wifi_probe', json.dumps(first))

    def test_200_with_captive_html_is_not_success(self):
        with patch.object(connectivity.urllib.request, 'build_opener', return_value=self.opener(b'<h1>Log in</h1>')):
            self.assertEqual(connectivity.check(connectivity.ENDPOINTS[2])['result'], 'unexpected-response')

    def test_redirect_discards_query_token(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://public.test', 302, 'redirect',
                {'Location': 'https://wifi.delta.com/login?secret=do-not-log'}, io.BytesIO(b''))
        with patch.object(connectivity.urllib.request, 'build_opener', return_value=opener):
            result = connectivity.check(connectivity.ENDPOINTS[0])
        self.assertEqual(result['redirect_host'], 'wifi.delta.com')
        self.assertEqual(result['result'], 'portal-redirect')
        self.assertNotIn('do-not-log', json.dumps(result))

    def test_wikipedia_same_origin_canonical_redirect_is_followed(self):
        response = self.opener(b'<title>Wikipedia</title>').open.return_value
        opener = Mock()
        opener.open.side_effect = [urllib.error.HTTPError('https://www.wikipedia.org/', 301, 'redirect',
                                  {'Location': 'https://www.wikipedia.org/'}, io.BytesIO(b'')), response]
        with patch.object(connectivity.urllib.request, 'build_opener', return_value=opener):
            result = connectivity.check(connectivity.ENDPOINTS[3])
        self.assertEqual(result['result'], 'pass')
        self.assertEqual(len(result['redirects']), 1)
        self.assertTrue(result['redirects'][0]['same_origin_https'])
        self.assertEqual(opener.open.call_count, 2)

    def test_redirect_loops_are_bounded_without_being_called_captive(self):
        opener = Mock()
        opener.open.side_effect = [urllib.error.HTTPError('https://www.wikipedia.org/', 301, 'redirect',
                                  {'Location': '/'}, io.BytesIO(b'')) for _ in range(4)]
        with patch.object(connectivity.urllib.request, 'build_opener', return_value=opener):
            result = connectivity.check(connectivity.ENDPOINTS[3])
        self.assertEqual(result['result'], 'redirect-limit')
        self.assertEqual(opener.open.call_count, 4)

    def test_tls_error_has_safe_category(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLError('private detail'))
        with patch.object(connectivity.urllib.request, 'build_opener', return_value=opener):
            result = connectivity.check(connectivity.ENDPOINTS[0])
        self.assertEqual(result['result'], 'tls-error')
        self.assertNotIn('private detail', json.dumps(result))
