import base64
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
from delta_wifi import cli
from delta_wifi.http_portal import (HttpPortalClient, Transport, PortalError, PORTAL, SIGNIN,
                                  PREMIUM, allowed, pkce, provider_status)


class FakePortal:
    """Observed wire contract with entirely synthetic credentials and identifiers."""
    def __init__(self, policy='DalPedDefault'):
        self.policy = policy
        self.device_ip = '172.19.0.81'
        self.device_id = 'device-A'
        self.events = []
        self.activation_failures = 0
        self.reset_effective = True
        self.initial_status = 'IS_USER_REMEMBERED'

    def close(self):
        pass

    def request(self, method, url, body=None, content_type='application/json', headers=None):
        path = urllib.parse.urlsplit(url).path
        self.events.append(dict(method=method, path=path, body=body, content_type=content_type, headers=headers))
        if path == '/api/device-details':
            return dict(deviceLanIp=self.device_ip, deviceId=self.device_id, devicePolicy=self.policy)
        if path == '/api/flight-data':
            return {'flightId': 'flight-A'}
        if path == '/as/authorization.oauth2':
            return {'id': 'flow-A', 'status': self.initial_status}
        if path == '/pf-ws/authn/flows/flow-A':
            action = content_type.split('pingidentity.')[1].split('+')[0]
            if action == 'submitDeviceInformation':
                return {'id': 'flow-A', 'status': 'USERNAME_PASSWORD_REQUIRED'}
            if action == 'checkUsernamePassword':
                assert body == {'username': 'fixture-user', 'password': 'fixture-password'}
                return {'id': 'flow-A', 'status': 'SHOULD_REMEMBER_USER'}
            if action == 'rememberDevice':
                assert body == {'rememberUser': False}
                return {'id': 'flow-A', 'status': 'REMEMBERED_DEVICE_DETAILS'}
            if action == 'submitContinue':
                return {'id': 'flow-A', 'status': 'COMPLETED', 'authorizeResponse': {'code': 'fixture-code'}}
        if path == '/as/token.oauth2':
            assert body['code'] == 'fixture-code'
            assert content_type == 'application/x-www-form-urlencoded'
            return {'access_token': 'fixture-access-token', 'expires_in': 3600}
        if path == '/api/portableElectronicDevice/v1/flights/flight-A/devices/device-A/sessions':
            if body == {'type': 'freeWithSkyMiles'}:
                assert headers == {'UserAuth': 'fixture-access-token'}
                if self.activation_failures:
                    self.activation_failures -= 1
                    raise PortalError('temporary-failure', 'HTTP 503', True)
                self.policy = PREMIUM
            elif body == {'type': 'default'}:
                if self.reset_effective:
                    self.policy = 'DalPedDefault'
            else:
                raise AssertionError('Unexpected policy mutation')
            return {}
        raise AssertionError('Unexpected HTTP endpoint')


class HttpFlowTests(unittest.TestCase):
    def client(self, policy='DalPedDefault'):
        portal = FakePortal(policy)
        client = HttpPortalClient(Mock(return_value=('online', False)), Mock(return_value='fixture-password'),
                                  Mock(return_value=True), Mock(return_value='172.19.0.81'),
                                  verify_seconds=1, transport=portal)
        return client, portal

    def test_full_observed_flow_and_access_activation_are_browser_free(self):
        client, portal = self.client()
        result = client.recover('fixture-user')
        self.assertEqual(result.state, 'online')
        self.assertTrue(result.submitted)
        self.assertEqual(portal.policy, PREMIUM)
        client.credentials.assert_called_once_with('fixture-user')
        token_request = next(event for event in portal.events if event['path'] == '/as/token.oauth2')
        self.assertGreaterEqual(len(token_request['body']['code_verifier']), 43)
        self.assertNotIn('fixture-access-token', result.message)
        client.close()
        self.assertIsNone(client.token)

    def test_premium_policy_preserves_existing_session_during_upstream_outage(self):
        client, portal = self.client(PREMIUM)
        client.probe.return_value = ('unavailable', False)
        self.assertEqual(client.recover('fixture-user').state, 'session-active')
        client.credentials.assert_not_called()
        self.assertFalse(any(event['method'] == 'POST' for event in portal.events))

    def test_premium_and_working_internet_skip_credentials(self):
        client, portal = self.client(PREMIUM)
        self.assertEqual(client.recover('fixture-user').state, 'online')
        client.credentials.assert_not_called()

    def test_basic_policy_still_authenticates_even_if_general_web_checks_pass(self):
        client, portal = self.client()
        self.assertEqual(client.recover('fixture-user').state, 'online')
        client.credentials.assert_called_once()

    def test_unknown_flow_pauses_without_reading_password(self):
        client, portal = self.client()
        portal.initial_status = 'MFA_REQUIRED'
        result = client.recover('fixture-user')
        self.assertEqual(result.state, 'interaction-required')
        self.assertFalse(result.retryable)
        client.credentials.assert_not_called()

    def test_wrong_device_ip_never_gets_credentials_or_policy_mutation(self):
        client, portal = self.client()
        portal.device_ip = '172.19.9.9'
        self.assertEqual(client.recover('fixture-user').state, 'device-mismatch')
        client.credentials.assert_not_called()
        self.assertFalse(any(event['method'] == 'POST' for event in portal.events))

    def test_transient_activation_failure_reuses_token_without_password_retry(self):
        client, portal = self.client()
        portal.activation_failures = 1
        first = client.recover('fixture-user')
        self.assertTrue(first.retryable)
        self.assertTrue(first.submitted)
        second = client.recover('fixture-user')
        self.assertEqual(second.state, 'online')
        self.assertFalse(second.submitted)
        client.credentials.assert_called_once()

    def test_password_post_connection_failure_does_not_automatically_retry(self):
        client, portal = self.client()
        original = portal.request
        def fail(method, url, body=None, content_type='application/json', headers=None):
            if 'checkUsernamePassword' in content_type:
                raise PortalError('connection-error', 'connection failed', True)
            return original(method, url, body, content_type, headers)
        portal.request = fail
        result = client.recover('fixture-user')
        self.assertTrue(result.submitted)
        self.assertFalse(result.retryable)

    def test_logout_dry_run_is_read_only_and_does_not_pause_watch(self):
        client, portal = self.client(PREMIUM)
        pause = Mock()
        result = client.logout(dry_run=True, before_reset=pause)
        self.assertEqual(result.state, 'ready')
        pause.assert_not_called()
        self.assertEqual(portal.policy, PREMIUM)
        self.assertFalse(any(event['method'] == 'POST' for event in portal.events))

    def test_logout_resets_only_this_device_and_keeps_password_untouched(self):
        client, portal = self.client(PREMIUM)
        pause = Mock()
        result = client.logout(before_reset=pause)
        self.assertEqual(result.state, 'logged-out')
        self.assertEqual(portal.policy, 'DalPedDefault')
        client.credentials.assert_not_called()
        pause.assert_called_once()
        posts = [event for event in portal.events if event['method'] == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['body'], {'type': 'default'})
        self.assertNotIn('signin', posts[0]['path'])

    def test_wrong_device_cannot_be_logged_out_or_pause_watch(self):
        client, portal = self.client(PREMIUM)
        portal.device_ip = '172.19.9.9'
        pause = Mock()
        self.assertEqual(client.logout(before_reset=pause).state, 'device-mismatch')
        pause.assert_not_called()
        self.assertFalse(any(event['method'] == 'POST' for event in portal.events))

    def test_unconfirmed_logout_does_not_claim_success(self):
        client, portal = self.client(PREMIUM)
        client.verify_seconds = 0
        portal.reset_effective = False
        self.assertEqual(client.logout().state, 'unconfirmed')
        self.assertEqual(portal.policy, PREMIUM)

    def test_info_does_not_expose_device_identifiers_or_read_credentials(self):
        client, portal = self.client(PREMIUM)
        info = client.info()
        self.assertEqual(info['session'], 'premium-access')
        self.assertFalse(info['credentials_read'])
        serialized = json.dumps(info)
        for secret in ('device-A', 'flight-A', '172.19.0.81'):
            self.assertNotIn(secret, serialized)
        client.credentials.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_pkce_uses_s256(self):
        verifier, challenge = pkce()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        self.assertEqual(challenge, expected)

    def test_only_exact_https_origins_are_allowed(self):
        for url in (PORTAL + '/api/device-details', SIGNIN + '/as/token.oauth2'):
            self.assertTrue(allowed(url))
        for url in ('http://wifi.delta.com', 'https://wifi.delta.com.evil.test', 'https://user:pass@signin.delta.com', 'https://signin.delta.com:8443'):
            self.assertFalse(allowed(url))

    def test_redirects_are_never_followed_with_passwords(self):
        transport = Transport()
        transport.opener = Mock()
        transport.opener.open.side_effect = urllib.error.HTTPError(SIGNIN, 302, 'redirect',
                                                {'Location':'https://untrusted.test'}, io.BytesIO())
        with self.assertRaises(PortalError) as caught:
            transport.request('POST', SIGNIN + '/pf-ws/authn/flows/test', {'password':'fixture-secret'})
        self.assertEqual(caught.exception.state, 'interaction-required')
        self.assertNotIn('fixture-secret', str(caught.exception))
        transport.opener.open.assert_called_once()

    def test_unexpected_origin_is_rejected_before_any_request(self):
        transport = Transport()
        transport.opener = Mock()
        with self.assertRaises(PortalError):
            transport.request('POST', 'https://attacker.test', {'password':'fixture-secret'})
        transport.opener.open.assert_not_called()

    def test_cli_factory_defaults_to_http_without_creating_browser_client(self):
        with patch.object(cli, 'HttpPortalClient') as http, patch.object(cli, 'PortalClient') as browser:
            self.assertIs(cli.portal_client(), http.return_value)
            browser.assert_not_called()
        args = cli.build_parser().parse_args(['login'])
        self.assertEqual(args.mode, 'http')

    def test_rejoining_network_has_fresh_csrf_cookie_without_identity_cookies(self):
        transport = Transport()
        original = transport.xsrf
        # Seed an additional synthetic auth cookie on the allowed sign-in origin.
        import http.cookiejar
        transport.cookies.set_cookie(http.cookiejar.Cookie(
            0, 'identity', 'fixture-sensitive-token', None, False, 'signin.delta.com',
            False, False, '/', True, True, None, True, None, None, {}, False))
        transport.close()
        self.assertNotEqual(original, transport.xsrf)
        remaining = [(cookie.name, cookie.domain) for cookie in transport.cookies]
        self.assertEqual(remaining, [('XSRF-TOKEN', 'wifi.delta.com')])

    def test_optional_browser_mode_stays_explicit(self):
        with patch.object(cli, 'HttpPortalClient') as http, patch.object(cli, 'PortalClient') as browser:
            self.assertIs(cli.portal_client(mode='browser', browser_name='canary'), browser.return_value)
            http.assert_not_called()
