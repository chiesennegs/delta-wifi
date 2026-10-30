import unittest
from unittest.mock import patch
from delta_wifi import cli


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        provider = patch.object(cli, 'provider_status', return_value={'session': 'unknown', 'device_policy': None})
        provider.start()
        self.addCleanup(provider.stop)
        diagnostic = patch.object(cli.network, 'diagnostics', side_effect=lambda ip: dict(
            dns='working', wifi_interface='en0', gateway='192.168.1.1', gateway_status='responding',
            route_interface='en0', route_status='wifi', vpn='not-detected', proxy='not-detected'))
        diagnostic.start()
        self.addCleanup(diagnostic.stop)

    def test_confirmation_cannot_override_other_network_or_missing_ip(self):
        self.assertTrue(cli.network_allowed(None, '172.19.1.52', True))
        self.assertFalse(cli.network_allowed('Home', '172.19.1.52', True))
        self.assertFalse(cli.network_allowed(None, None, True))
        self.assertFalse(cli.network_allowed(None, '172.19.1.52', False))

    @patch.object(cli, 'fetch', return_value=(200, b'', None))
    @patch.object(cli, 'probe_internet', return_value=('online', False))
    @patch.object(cli, 'association', return_value=(None, '172.19.1.52', None))
    def test_confirmation_keeps_actual_ssid_unknown(self, *mocks):
        state = cli.inspect(assume_delta=True)
        self.assertEqual(state.wifi, 'user-confirmed')
        self.assertIsNone(state.ssid)
        self.assertEqual(state.internet, 'online')

    @patch.object(cli, 'fetch')
    @patch.object(cli, 'association', return_value=(None, '172.19.1.52', None))
    def test_hidden_ssid_has_distinct_status(self, association, fetch):
        self.assertEqual(cli.inspect().wifi, 'connected-unidentified')
        fetch.assert_not_called()

    @patch.object(cli.platform, 'system', return_value='Darwin')
    @patch.object(cli.native_helper, 'read', return_value={'ssid': cli.SSID, 'interface': 'en0'})
    @patch.object(cli, 'run')
    def test_native_ssid_takes_priority(self, run, native, platform):
        run.return_value.stdout = '172.19.1.52\n'
        self.assertEqual(cli.association(), (cli.SSID, '172.19.1.52', None))
        run.assert_called_once_with(['ipconfig', 'getifaddr', 'en0'])

    def test_credential_origins(self):
        for url in ['https://delta.com/login', 'https://wifi.deltawifi.com/login']:
            self.assertTrue(cli.trusted(url))
        for url in ['http://delta.com', 'https://delta.com.evil.example', 'https://evildelta.com', 'https://delta.com:8080']:
            self.assertFalse(cli.trusted(url))

    @patch.object(cli, 'fetch')
    @patch.object(cli, 'association', return_value=('Home', '192.168.1.2', None))
    def test_other_network_is_not_probed(self, association, fetch):
        self.assertEqual(cli.inspect().internet, 'not-tested')
        fetch.assert_not_called()

    @patch.object(cli, 'fetch', return_value=(None, b'', None))
    @patch.object(cli, 'probe_internet', return_value=('unavailable', False))
    @patch.object(cli, 'association', return_value=(cli.SSID, '192.168.1.2', None))
    def test_outage_is_not_expired_session(self, *mocks):
        self.assertEqual(cli.inspect().session, 'unknown')

    @patch.object(cli, 'fetch', return_value=(200, b'<form><input type="password"></form>', None))
    @patch.object(cli, 'probe_internet', return_value=('unavailable', False))
    @patch.object(cli, 'association', return_value=(cli.SSID, '192.168.1.2', None))
    def test_login_form_is_evidence(self, *mocks):
        self.assertEqual(cli.inspect().session, 'login-required')

    @patch.object(cli, 'fetch', return_value=(200, b'', None))
    @patch.object(cli, 'probe_internet', return_value=('online', False))
    @patch.object(cli, 'association', return_value=(cli.SSID, '192.168.1.2', None))
    def test_working_access_does_not_claim_authentication(self, *mocks):
        self.assertEqual(cli.inspect().session, 'unknown')

    @patch.object(cli, 'fetch', return_value=(200, b'', None))
    @patch.object(cli, 'probe_internet', return_value=('unavailable', False))
    @patch.object(cli, 'association', return_value=(cli.SSID, '172.19.1.52', None))
    def test_provider_policy_is_independent_of_internet_outage(self, *mocks):
        with patch.object(cli, 'provider_status', return_value={'session': 'premium-access', 'device_policy': 'DalPedPremium'}):
            result = cli.inspect()
        self.assertEqual(result.session, 'premium-access')
        self.assertEqual(result.internet, 'unavailable')
        self.assertEqual(result.provider_policy, 'DalPedPremium')

    @patch.object(cli.connectivity, 'sample', return_value={'internet': 'limited', 'captive_evidence': False})
    def test_partial_internet_is_not_online(self, sample):
        self.assertEqual(cli.probe_internet(), ('limited', False))

    @patch.object(cli, 'fetch', return_value=(200, b'', None))
    @patch.object(cli, 'probe_internet', return_value=('online', False))
    @patch.object(cli, 'association', side_effect=[(cli.SSID, '172.19.1.52', None), ('TestHotspot', '192.168.1.2', None)])
    def test_switching_to_hotspot_invalidates_delta_success(self, *mocks):
        result = cli.inspect()
        self.assertEqual(result.internet, 'network-changed')
        self.assertEqual(result.session, 'unknown')

    @patch.object(cli, 'fetch', return_value=(200, b'', None))
    @patch.object(cli, 'probe_internet', return_value=('online', True))
    @patch.object(cli, 'association', return_value=(cli.SSID, '172.19.1.52', None))
    def test_redirect_evidence_prevents_success_claim(self, *mocks):
        result = cli.inspect()
        self.assertEqual(result.internet, 'captive')
        self.assertEqual(result.session, 'login-required')



if __name__ == '__main__':
    unittest.main()
