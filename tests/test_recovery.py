import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from delta_wifi.recovery import LoginResult, RetryPolicy, page_failure
from delta_wifi.portal import PortalClient, auth_payload
from delta_wifi import cli


class RetryTests(unittest.TestCase):
    @patch('delta_wifi.recovery.random.uniform', return_value=1)
    def test_temporary_failures_back_off_then_stop(self, jitter):
        policy = RetryPolicy(3, 30)
        result = LoginResult('temporary-failure', 'server down', True)
        policy.record(result, 100)
        self.assertFalse(policy.ready(129))
        self.assertTrue(policy.ready(130))
        policy.record(result, 130)
        self.assertEqual(policy.next_at, 190)
        policy.record(result, 190)
        self.assertEqual(policy.paused, 'attempt-limit')
        self.assertFalse(policy.ready(10000))

    def test_rejected_credentials_remain_paused_even_after_recovery(self):
        policy = RetryPolicy()
        policy.record(LoginResult('credentials-rejected', 'rejected'), 0)
        for _ in range(10):
            policy.observe('online')
        self.assertEqual(policy.paused, 'action-required')
        self.assertFalse(policy.ready(10000))

    def test_flapping_connection_does_not_reset_attempt_limit(self):
        policy = RetryPolicy(1)
        policy.record(LoginResult('temporary-failure', 'down', True), 0)
        for _ in range(5):
            policy.observe('online')
            policy.observe('unavailable')
        self.assertFalse(policy.ready(1000))
        for _ in range(3):
            policy.observe('online')
        self.assertTrue(policy.ready(1000))

    def test_only_authentication_responses_are_classified(self):
        self.assertTrue(auth_payload('{"credentials":{"user":"a","password":"b"}}', 'a', 'b'))
        self.assertTrue(auth_payload('user=a&password=b', 'a', 'b'))
        self.assertFalse(auth_payload('{"event":"pageView"}', 'a', 'b'))
        self.assertFalse(auth_payload(None, 'a', 'b'))

    def test_explicit_failure_classification(self):
        self.assertEqual(page_failure('Incorrect password').state, 'credentials-rejected')
        self.assertFalse(page_failure('Verify your identity').retryable)
        self.assertTrue(page_failure('Service unavailable').retryable)
        self.assertIsNone(page_failure('Welcome to Fast, Free Wi-Fi'))


class WatchTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(cli.storage, 'record')
        patcher.start()
        self.addCleanup(patcher.stop)
        for method in ('last_access', 'set_last_access', 'recovery_paused'):
            other = patch.object(cli.storage, method, return_value=None)
            other.start()
            self.addCleanup(other.stop)

    def test_outage_retries_temporary_error_then_pauses_on_rejection(self):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        status = cli.Status('now', 'connected', cli.SSID, '172.19.1.52',
                            'unknown', 'unavailable', 'unknown', 'reachable', None)
        client = Mock()
        client.recover.side_effect = [LoginResult('temporary-failure', '503', True),
                                      LoginResult('credentials-rejected', 'rejected')]
        args = SimpleNamespace(max_attempts=3, retry_delay=30, assume_delta=False,
                               headless=False, browser='canary', interval=20,
                               auto_login='fixture-user', json=True)
        with patch.object(cli, 'portal_client', return_value=client), \
             patch.object(cli, 'inspect', side_effect=[status] * 8 + [KeyboardInterrupt()]), \
             patch.object(cli, 'display'), patch.object(cli, 'event'), patch('builtins.print'), \
             patch.object(cli.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(cli.time, 'sleep', side_effect=sleep), \
             patch.object(cli.random, 'uniform', return_value=1):
            with self.assertRaises(KeyboardInterrupt):
                cli.watch(args)
        self.assertEqual(client.recover.call_count, 2)
        client.close.assert_called_once()


class AdapterTests(unittest.TestCase):
    def client(self, internet='unavailable', network=True):
        return PortalClient(cli.PORTAL, cli.trusted, Mock(return_value=(internet, False)),
                            Mock(return_value='not-a-real-password'), Mock(return_value=network))

    def test_online_connection_does_not_access_credentials_or_browser(self):
        for internet in ('online',):
            client = self.client(internet)
            with patch.object(client, 'start') as start:
                self.assertEqual(client.recover('test').state, 'online')
                start.assert_not_called()
                client.credentials.assert_not_called()

    def test_partial_access_does_not_skip_login_inspection(self):
        for internet in ('limited', 'degraded', 'captive'):
            client = self.client(internet)
            result = LoginResult('layout-changed', 'inspection needed')
            with patch.object(client, '_recover', return_value=result) as recover:
                self.assertEqual(client.recover('test').state, 'layout-changed')
                recover.assert_called_once_with('test')

    def test_network_change_after_probes_does_not_claim_success(self):
        client = self.client('online')
        client.network_check.side_effect = [True, False]
        self.assertEqual(client.recover('test').state, 'network-changed')
        client.credentials.assert_not_called()

    def test_redirect_evidence_overrides_online_label(self):
        client = self.client('online')
        client.probe.return_value = ('online', True)
        with patch.object(client, '_recover', return_value=LoginResult('layout-changed', 'inspect')) as recover:
            self.assertNotEqual(client.recover('test').state, 'online')
            recover.assert_called_once()

    def test_explicit_force_inspects_form_despite_online_checks(self):
        client = self.client('online')
        with patch.object(client, '_recover', return_value=LoginResult('layout-changed', 'inspect')) as recover:
            self.assertEqual(client.recover('test', force=True).state, 'layout-changed')
            recover.assert_called_once_with('test')

    def test_other_network_never_probes_or_reads_keychain(self):
        client = self.client(network=False)
        self.assertEqual(client.recover('test').state, 'network-changed')
        client.probe.assert_not_called()
        client.credentials.assert_not_called()

    def test_unexpected_destination_stops_before_credentials(self):
        client = self.client()
        with patch.object(client, 'open_login', return_value=LoginResult('unexpected-origin', 'blocked')):
            self.assertEqual(client.recover('test').state, 'unexpected-origin')
            client.credentials.assert_not_called()

    def test_server_failure_can_retry_without_reading_password(self):
        client = self.client()
        with patch.object(client, 'open_login', return_value=LoginResult('temporary-failure', '503', True)):
            self.assertTrue(client.recover('test').retryable)
            client.credentials.assert_not_called()

    def test_no_raw_exception_details_in_result(self):
        client = self.client()
        with patch.object(client, '_recover', side_effect=ValueError('secret-password')):
            result = client.recover('test')
        self.assertNotIn('secret-password', result.message)
        self.assertFalse(result.retryable)


if __name__ == '__main__':
    unittest.main()
