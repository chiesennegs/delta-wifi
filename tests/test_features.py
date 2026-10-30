import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from delta_wifi import cli, network, storage, notifications
from delta_wifi.monitor import Timeline, Panel
from delta_wifi.recovery import RetryPolicy, LoginResult


def state(internet='unavailable', wifi='connected', **extra):
    values = dict(timestamp='2026-10-30T12:00:00-04:00', wifi=wifi,
                  ssid=cli.SSID if wifi == 'connected' else 'Home', local_ip='172.19.1.52',
                  session='unknown', internet=internet, dns='working', portal='reachable', note=None,
                  gateway_status='responding', route_status='wifi', vpn='not-detected', proxy='not-detected')
    values.update(extra)
    return cli.Status(**values)


class StorageTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name) / 'state'
        env = patch.dict(os.environ, DELTA_WIFI_STATE_DIR=str(self.root))
        env.start()
        self.addCleanup(env.stop)

    def test_private_bounded_history_drops_identifiers(self):
        with patch.object(storage, 'MAX_EVENTS', 3):
            for attempt in range(5):
                storage.record('login-result', state='online', attempt=attempt,
                               password='secret', account='user', ssid='private', local_ip='10.1.1.1', cookies='token')
        rows = storage.history()
        self.assertEqual([row['attempt'] for row in rows], [2, 3, 4])
        serialized = json.dumps(rows)
        for key in ('password', 'account', 'ssid', 'local_ip', 'cookies'):
            self.assertNotIn(key, serialized)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'state.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_clear_history_preserves_preference_and_last_access(self):
        storage.set_account('test-user')
        storage.set_last_access('last-success')
        storage.set_recovery_paused(True)
        storage.record('connection', internet='online')
        storage.clear_history()
        self.assertEqual(storage.preferred_account(), 'test-user')
        self.assertEqual(storage.last_access(), 'last-success')
        self.assertTrue(storage.recovery_paused())
        self.assertEqual(storage.history(), [])
        storage.set_account(None)
        self.assertIsNone(storage.preferred_account())

    def test_legacy_partial_success_timestamp_is_not_reused(self):
        with storage.database() as db:
            db.execute("INSERT INTO preferences VALUES ('last_access', 'unverified-old-value')")
        self.assertIsNone(storage.last_access())
        storage.set_last_access('verified-v2-value')
        self.assertEqual(storage.last_access(), 'verified-v2-value')

    def test_existing_account_can_be_selected_without_reading_password(self):
        with patch.object(cli, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
             patch('sys.argv', ['delta-wifi', 'account', 'use', 'test-user']), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.main()
        self.assertEqual(storage.preferred_account(), 'test-user')
        self.assertNotIn('-w', run.call_args.args[0])

    def test_removing_another_account_keeps_preferred_account(self):
        storage.set_account('keep-user')
        with patch.object(cli, 'run', return_value=SimpleNamespace(returncode=0)), \
             patch('sys.argv', ['delta-wifi', 'account', 'forget', 'other-user']), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.main()
        self.assertEqual(storage.preferred_account(), 'keep-user')


class TimelineTests(unittest.TestCase):
    def test_outage_duration_and_single_recovery_alert(self):
        record, notify, save = Mock(), Mock(), Mock()
        timeline = Timeline(record=record, notify=notify, save_online=save, threshold=60)
        timeline.observe(state('online'), 0)
        timeline.observe(state(), 10)
        timeline.observe(state(), 70)
        timeline.observe(state('online'), 80)
        timeline.observe(state('online'), 90)
        notify.assert_called_once_with('restored')
        ends = [c for c in record.call_args_list if c.args == ('outage-end',)]
        self.assertEqual(ends[0].kwargs['duration_seconds'], 70)
        self.assertEqual(len([c for c in record.call_args_list if c.args == ('outage-start',)]), 1)
        self.assertEqual(timeline.last_online, '2026-10-30T12:00:00-04:00')
        self.assertTrue(save.called)

    def test_short_outage_does_not_notify_and_attention_is_deduplicated(self):
        notify = Mock()
        timeline = Timeline(record=Mock(), notify=notify)
        timeline.observe(state(), 0)
        timeline.observe(state('online'), 10)
        notify.assert_not_called()
        timeline.attention()
        timeline.attention()
        notify.assert_called_once_with('attention')

    def test_leaving_network_ends_outage_without_claiming_recovery(self):
        record, notify = Mock(), Mock()
        timeline = Timeline(record=record, notify=notify)
        timeline.observe(state(), 0)
        timeline.observe(state('not-tested', 'other-network'), 100)
        self.assertIsNone(timeline.outage_since)
        notify.assert_not_called()
        self.assertTrue(any(c.args == ('outage-interrupted',) for c in record.call_args_list))
        runtime = timeline.runtime(state('not-tested', 'other-network'), RetryPolicy(), 100, 'test')
        self.assertIn('Waiting for DeltaWiFi.com', runtime['action'])

    def test_route_conflict_reports_suspended_login(self):
        runtime = Timeline(record=Mock()).runtime(state(route_status='vpn-route'), RetryPolicy(), 0, 'test')
        self.assertIn('suspended', runtime['action'])

    def test_panel_restores_terminal_and_filters_escape_characters(self):
        output = io.StringIO()
        output.isatty = lambda: True
        with patch('sys.stdout', output):
            panel = Panel(enabled=True)
            current = state(ssid='Delta\x1b[31m')
            runtime = Timeline(record=Mock()).runtime(current, RetryPolicy(), 0, None)
            panel.render(current, runtime, 10)
            panel.close()
        self.assertNotIn('Delta\x1b', output.getvalue())
        self.assertIn('\x1b[?25h\x1b[?1049l', output.getvalue())


class NetworkTests(unittest.TestCase):
    def diagnose(self, route_interface='en0', ping_code=0, resolved=True, proxies=None):
        def fake_command(args, timeout=4):
            if 'ping' in args[0]:
                return SimpleNamespace(returncode=ping_code, stdout='')
            if '-listallhardwareports' in args:
                output = 'Hardware Port: Wi-Fi\nDevice: en0\n'
            elif 'route' in args[0]:
                scoped = '-ifscope' in args
                output = f'  interface: {"en0" if scoped else route_interface}\n  gateway: 172.19.1.1\n'
            elif 'dscacheutil' in args[0]:
                output = 'ip_address: 203.0.113.10\n' if resolved else ''
            else:
                output = ''
            return SimpleNamespace(returncode=0, stdout=output)
        with patch.object(network, 'command', side_effect=fake_command), \
             patch.object(network.urllib.request, 'getproxies', return_value=proxies or {}):
            return network.diagnostics('172.19.1.52')

    def test_local_gateway_and_upstream_route_are_separate(self):
        result = self.diagnose(route_interface='utun4')
        self.assertEqual(result['gateway_status'], 'responding')
        self.assertEqual(result['route_status'], 'vpn-route')
        self.assertEqual(result['vpn'], 'detected')

    def test_missing_ping_reply_does_not_claim_gateway_is_down(self):
        result = self.diagnose(ping_code=2)
        self.assertEqual(result['gateway_status'], 'no-reply')
        self.assertEqual(result['route_status'], 'wifi')

    def test_proxy_bypass_list_alone_is_not_a_proxy(self):
        self.assertEqual(self.diagnose(proxies={'no': 'localhost'})['proxy'], 'not-detected')
        self.assertEqual(self.diagnose(proxies={'https': 'http://127.0.0.1:1080'})['proxy'], 'configured')

    def test_dns_failure_is_independent_of_default_route(self):
        result = self.diagnose(resolved=False)
        self.assertEqual(result['dns'], 'failed')
        self.assertEqual(result['route_status'], 'wifi')


class NotificationTests(unittest.TestCase):
    @patch.object(notifications, 'invoke', return_value={'state': 'denied'})
    def test_delivery_failure_is_not_repeated(self, invoke):
        notifier = notifications.Notifier(True)
        notifier.send('attention')
        notifier.send('restored')
        invoke.assert_called_once()
        self.assertIn('notify-perms', notifier.problem)

    @patch.object(notifications, 'invoke')
    def test_disabled_notifications_stay_quiet(self, invoke):
        notifications.Notifier(False).send('restored')
        invoke.assert_not_called()

class WatchTransitionTests(unittest.TestCase):
    def setUp(self):
        pause = patch.object(cli.storage, 'recovery_paused', return_value=False)
        pause.start()
        self.addCleanup(pause.stop)

    def args(self):
        return SimpleNamespace(max_attempts=3, retry_delay=30, assume_delta=False,
                               headless=False, browser='canary', interval=20, auto_login='test-user',
                               json=True, display='log', notify=False, notify_after=60)

    def run_watch(self, states):
        client = Mock()
        client.recover.return_value = LoginResult('temporary-failure', 'temporary failure', True)
        with patch.object(cli, 'portal_client', return_value=client), \
             patch.object(cli, 'inspect', side_effect=states + [KeyboardInterrupt()]), \
             patch.object(cli.storage, 'record') as record, \
             patch.object(cli.storage, 'last_access', return_value=None), \
             patch.object(cli.storage, 'set_last_access'), patch.object(cli.time, 'sleep'), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                cli.watch(self.args())
        return client, record

    def test_waits_before_boarding_and_returns_to_waiting_after_leaving(self):
        client, record = self.run_watch([state('not-tested', 'other-network'),
                                         state('online'), state('not-tested', 'other-network')])
        client.recover.assert_not_called()
        connections = [call.kwargs['wifi'] for call in record.call_args_list if call.args == ('connection',)]
        self.assertEqual(connections, ['other-network', 'connected', 'other-network'])
        self.assertGreaterEqual(client.close.call_count, 3)

    def test_active_premium_session_prevents_reauthentication_during_outage(self):
        client, _ = self.run_watch([state(session='premium-access')] * 3)
        client.recover.assert_not_called()

    def test_logout_pause_prevents_watch_from_undoing_the_logout(self):
        with patch.object(cli.storage, 'recovery_paused', return_value=True):
            client, _ = self.run_watch([state()] * 3)
        client.recover.assert_not_called()

    def test_vpn_and_dns_failures_never_trigger_credential_recovery(self):
        for extra in ({'route_status': 'vpn-route'}, {'proxy': 'configured'}, {'dns': 'failed', 'portal': 'unreachable'}):
            client, _ = self.run_watch([state(**extra)] * 3)
            client.recover.assert_not_called()

    def test_reachable_portal_can_recover_when_external_dns_is_blocked(self):
        client, _ = self.run_watch([state(dns='failed', portal='reachable')] * 3)
        self.assertTrue(client.recover.called)

    def test_json_is_parseable_and_contains_next_action(self):
        output = io.StringIO()
        with patch.object(cli, 'portal_client'), \
             patch.object(cli, 'inspect', side_effect=[state('online'), KeyboardInterrupt()]), \
             patch.object(cli.storage, 'record'), patch.object(cli.storage, 'last_access', return_value=None), \
             patch.object(cli.storage, 'set_last_access'), patch.object(cli.time, 'sleep'), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                cli.watch(self.args())
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0]['internet'], 'online')
        self.assertIn('preserving session', rows[0]['action'])
        self.assertEqual(rows[1]['event'], 'next-check')
        self.assertNotIn('test-user', output.getvalue())


if __name__ == '__main__':
    unittest.main()
