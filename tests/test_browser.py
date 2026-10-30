"""Opt-in browser tests using synthetic pages and fake credentials; no Delta requests."""
import os
import unittest
from unittest.mock import Mock
from delta_wifi import cli
from delta_wifi.portal import PortalClient

HTML = '''<!doctype html><button type="submit" onclick="document.querySelector('form').hidden=false;this.hidden=true">LOG IN</button>
<form hidden onsubmit="event.preventDefault(); fetch('/authenticate', {method:'POST',body:JSON.stringify({user:document.querySelector('#userName').value,password:document.querySelector('#password').value})}).then(r=>r.text()).then(t=>document.querySelector('#result').textContent=t)">
<input id="userName" placeholder="SkyMiles Number or Username"><input type="password" id="password" name="psw"><button id="login" type="submit">LOG IN</button></form><div id="result"></div>'''


@unittest.skipUnless(os.getenv('DELTA_WIFI_BROWSER_TESTS') == '1', 'opt-in local browser tests')
class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.probe = Mock(return_value=('unavailable', False))
        self.client = PortalClient(cli.PORTAL, cli.trusted, self.probe,
                                   Mock(return_value='fixture-password'), Mock(return_value=True),
                                   headless=True, verify_seconds=2, browser_name="canary")
        self.addCleanup(self.client.close)
        self.client.start()
        self.posts = []
        self.status = 200
        self.message = ''
        def fixture(route):
            if route.request.method == 'POST':
                self.posts.append(route.request.post_data_json)
                route.fulfill(status=self.status, content_type='text/plain', body=self.message)
            else:
                route.fulfill(status=200, content_type='text/html', body=HTML)
        self.client.page.route('**/*', fixture)

    def tearDown(self):
        self.client.close()

    def test_actual_dialog_submission_and_connectivity_verification(self):
        self.probe.side_effect = [('unavailable', False), ('online', False)]
        result = self.client.recover('fixture-user')
        self.assertEqual(result.state, 'online')
        self.assertEqual(self.posts, [{'user':'fixture-user', 'password':'fixture-password'}])
        self.assertEqual(self.client.page.locator('#password').input_value(), '')

    def test_explicit_authentication_rejection_stops_retries(self):
        self.status = 401
        self.message = 'Incorrect password'
        result = self.client.recover('fixture-user')
        self.assertEqual(result.state, 'credentials-rejected')
        self.assertFalse(result.retryable)
        self.assertEqual(len(self.posts), 1)

    def test_server_failure_is_retryable(self):
        self.status = 503
        result = self.client.recover('fixture-user')
        self.assertEqual(result.state, 'temporary-failure')
        self.assertTrue(result.retryable)

    def test_unconfirmed_post_does_not_retry(self):
        result = self.client.recover('fixture-user')
        self.assertEqual(result.state, 'unconfirmed')
        self.assertFalse(result.retryable)

    def test_rate_limit_pauses(self):
        self.status = 429
        result = self.client.recover('fixture-user')
        self.assertEqual(result.state, 'rate-limited')
        self.assertFalse(result.retryable)

    def test_rejection_is_not_overridden_by_working_network(self):
        self.status = 401
        self.probe.return_value = ('online', False)
        result = self.client.recover('fixture-user', force=True)
        self.assertEqual(result.state, 'credentials-rejected')
        self.assertFalse(result.retryable)
