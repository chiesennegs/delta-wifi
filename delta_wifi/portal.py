"""Adapter for the observed wifi.delta.com SkyMiles login dialog.

Retains the browser context in memory for the lifetime of watch; no cookie files.
"""
import re
import json
from dataclasses import replace
from pathlib import Path
import time
import urllib.parse
from .recovery import LoginResult, page_failure


def auth_payload(body, account, password):
    """Identify only responses to a credential-bearing request, not telemetry POSTs."""
    if not body or not account or not password:
        return False
    def values(value):
        if isinstance(value, dict):
            return [item for entry in value.values() for item in values(entry)]
        if isinstance(value, list):
            return [item for entry in value for item in values(entry)]
        return [str(value)]
    try:
        entries = values(json.loads(body))
    except (ValueError, TypeError):
        entries = values(urllib.parse.parse_qs(body))
    return account in entries and password in entries


class PortalClient:
    def __init__(self, portal, trusted, probe, credentials, network_check, headless=False, verify_seconds=90, browser_name="chrome"):
        self.portal = portal
        self.trusted = trusted
        self.probe = probe
        self.credentials = credentials
        self.network_check = network_check
        self.headless = headless
        self.browser_name = browser_name
        self.verify_seconds = verify_seconds
        self.submitted = False
        self.video_skip_clicked = False
        self.pw = self.browser = self.context = self.page = None

    def close(self):
        if self.pw:
            try:
                if self.browser:
                    self.browser.close()
            finally:
                self.pw.stop()
                self.pw = self.browser = self.context = self.page = None

    def start(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        app_name = 'Google Chrome Canary' if self.browser_name == 'canary' else 'Google Chrome'
        executable = Path('/Applications') / (app_name + '.app') / 'Contents' / 'MacOS' / app_name
        if not executable.exists():
            raise RuntimeError(app_name + ' must be installed in /Applications')
        self.browser = self.pw.chromium.launch(executable_path=str(executable), headless=self.headless,
                                               chromium_sandbox=True)
        self.context = self.browser.new_context(service_workers='block')
        def guard(route):
            req = route.request
            # Block all non-allowlisted outgoing traffic once credentials may be in the page.
            # This includes GET requests, subresources, and POSTs; TLS validation remains enabled.
            if self.trusted(req.url):
                route.continue_()
            else:
                route.abort()
        self.context.route('**/*', guard)
        self.page = self.context.new_page()

    def recover(self, account, force=False):
        self.submitted = False
        self.video_skip_clicked = False
        if not self.network_check():
            problem = getattr(self.network_check, 'problem', None)
            retryable = getattr(self.network_check, 'retryable', True)
            return LoginResult('network-changed', problem if isinstance(problem, str) else 'Delta Wi-Fi is not confirmed; waiting.',
                               retryable if isinstance(retryable, bool) else True)
        internet, captive = self.probe()
        if internet == 'online' and not captive and not force:
            if not self.network_check():
                return LoginResult('network-changed', 'Network changed during verification; no success claimed.', True)
            return LoginResult('online', 'General-web access verified; no login submitted. Authentication remains unconfirmed.')
        try:
            return replace(self._recover(account), submitted=self.submitted)
        except ImportError:
            return LoginResult('setup-required', 'Install browser dependencies; see README.')
        except Exception:
            # Do not expose browser exceptions, which can contain filled field values or URLs.
            self.close()
            return LoginResult('browser-error', 'Browser could not complete the flow; login paused. Run portal-info for layout diagnostics.', submitted=self.submitted)

    def open_login(self):
        if not self.page or self.page.is_closed():
            self.close()
            try:
                self.start()
            except Exception:
                self.close()
                return LoginResult('browser-unavailable', 'Automation browser could not start. Login paused; run portal-info from Terminal to check browser startup.')
        response = self.page.goto(self.portal, wait_until='domcontentloaded', timeout=30000)
        if not self.trusted(self.page.url):
            return LoginResult('unexpected-origin', 'Unexpected portal destination; manual login required.')
        if response and response.status == 429:
            return LoginResult('rate-limited', 'Portal rate limit; login paused.')
        if response and response.status >= 500:
            return LoginResult('temporary-failure', 'Portal server unavailable.', True)
        user = self.page.locator('input#userName')
        if not user.is_visible():
            button = self.page.get_by_role('button', name=re.compile(r'^log\s*in$', re.I))
            button.wait_for(state='visible', timeout=15000)
            if not user.is_visible() and button.count() == 1:
                button.click(timeout=10000)
        user.wait_for(state='visible', timeout=15000)
        return None

    def try_video_skip(self):
        """Use only an offered, actionable Skip control on the trusted portal.

        Angular's generated _ngcontent identifier is deliberately not part of the
        selector. This never changes timers, disabled states, or video playback.
        """
        if self.video_skip_clicked or not self.page or not self.trusted(self.page.url):
            return False
        try:
            button = self.page.get_by_role('button', name=re.compile(r'^\s*skip\s*$', re.I))
            count = button.count()
            if count > 1:
                return False
            if count == 1:
                candidate = button
            else:
                candidate = self.page.locator('div.content-container:visible').filter(
                    has_text=re.compile(r'^\s*skip\s*$', re.I))
                if candidate.count() != 1:
                    return False
            if not candidate.is_visible() or not candidate.is_enabled():
                return False
            blocked = candidate.evaluate("el => !!el.closest('[disabled], [aria-disabled=\"true\"], [inert]')")
            if blocked:
                return False
            candidate.click(timeout=1500)
            # A timer-gated handler can ignore a click even on a visible div.
            # Retry on a later poll unless the control actually disappears.
            candidate.wait_for(state='hidden', timeout=1500)
            self.video_skip_clicked = True
            return True
        except Exception:
            # An overlay, re-render, or unavailable control should not cause a
            # second credential submission or prevent connectivity verification.
            return False

    def _recover(self, account):
        submitted = False
        responses = []
        try:
            result = self.open_login()
            if result:
                return result
            page = self.page
            failure = page_failure(page.locator('body').inner_text())
            if failure:
                return failure
            user = page.locator('input#userName:visible')
            secret = page.locator('input#password[type="password"]:visible')
            submit = page.locator('button#login:visible')
            if user.count() != 1 or secret.count() != 1 or submit.count() != 1:
                return LoginResult('layout-changed', 'Expected Delta login dialog not found; manual login required.')
            form = secret.locator('xpath=ancestor::form')
            if form.count() != 1:
                return LoginResult('layout-changed', 'Expected Delta form not found; manual login required.')
            action = urllib.parse.urljoin(page.url, form.get_attribute('action') or page.url)
            if not self.trusted(action) or not self.trusted(page.url) or not self.network_check():
                return LoginResult('network-changed', 'Network or portal destination changed; no credentials submitted.')
            try:
                password = self.credentials(account)
            except Exception:
                return LoginResult('keychain-error', 'Cannot read Keychain credential; run credentials ACCOUNT.')
            auth_values = (account, password)
            def response_received(response):
                request = response.request
                if (request.method == 'POST' and self.trusted(response.url)
                        and auth_payload(request.post_data, *auth_values)):
                    responses.append(response.status)
            page.on('response', response_received)
            try:
                user.fill(account)
                secret.fill(password)
                password = None
                # Prevent a re-render or redirect from silently replacing the destination.
                if not self.trusted(page.url) or not self.network_check():
                    return LoginResult('network-changed', 'Network changed before submission; login paused.')
                submitted = True
                self.submitted = True
                submit.click(timeout=15000)
                deadline = time.monotonic() + self.verify_seconds
                while time.monotonic() < deadline:
                    page.wait_for_timeout(1500)
                    failure = page_failure(page.locator('body').inner_text())
                    if failure and not failure.retryable:
                        return failure
                    # Authentication rejection always outranks unrelated network access.
                    if 429 in responses:
                        return LoginResult('rate-limited', 'Portal rate limit; login paused.')
                    if 401 in responses:
                        return LoginResult('credentials-rejected', 'Authentication rejected; login paused.')
                    if 403 in responses:
                        return LoginResult('interaction-required', 'Portal denied authentication; manual login required.')
                    if any(code >= 500 for code in responses):
                        return LoginResult('temporary-failure', 'Login service returned a server error.', True)
                    self.try_video_skip()
                    internet, captive = self.probe()
                    if internet == 'online' and not captive:
                        if not self.network_check():
                            return LoginResult('network-changed', 'Network changed after submission; login outcome unknown.')
                        return LoginResult('online', 'General-web access verified after login; session identity is not independently confirmed.' +
                                           (' Portal Skip control selected.' if self.video_skip_clicked else ''))
                    if failure:
                        return failure
                return LoginResult('unconfirmed', 'Login submitted, but internet was not restored. Outcome unknown; login paused.')
            finally:
                page.remove_listener('response', response_received)
                auth_values = ('', '')
                # Clear credentials from the live DOM when the form remains visible.
                for locator in (user, secret):
                    try:
                        if locator.is_visible():
                            locator.fill('')
                    except Exception:
                        pass
        except Exception as exc:
            # Navigation timeouts are retryable only before credential submission.
            from playwright.sync_api import TimeoutError as BrowserTimeout, Error as BrowserError
            if isinstance(exc, (BrowserTimeout, BrowserError)):
                return LoginResult('unconfirmed' if submitted else 'temporary-failure',
                                   'Browser operation failed after submission; login paused.' if submitted
                                   else 'Portal could not load; will retry.', not submitted)
            raise

    def info(self):
        """Allowlisted structural metadata only: never values, HTML, tokens, or full URLs."""
        try:
            result = self.open_login()
            if result:
                return {'state': result.state, 'message': result.message}
            return {'host': urllib.parse.urlsplit(self.page.url).hostname,
                    'username_fields': self.page.locator('input#userName:visible').count(),
                    'password_fields': self.page.locator('input#password:visible').count(),
                    'submit_buttons': self.page.locator('button#login:visible').count(),
                    'adapter': 'delta-skymiles-dialog-v1'}
        except Exception:
            return {'state': 'unavailable', 'message': 'Portal could not be inspected; check browser setup and connectivity.'}
