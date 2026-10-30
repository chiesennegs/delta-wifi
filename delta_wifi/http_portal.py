"""Browser-free adapter derived from wifi.delta.com's public production portal.

Observed main.c9a4c2346f166521.js, 2026-10-30: Ping pi.flow + PKCE,
then POST freeWithSkyMiles to the current flight/device sessions resource.
No cookies, flow IDs, device IDs, OAuth codes or tokens are persisted or logged.
"""
import base64
import hashlib
import http.cookiejar
import ipaddress
import json
import math
import uuid
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from .recovery import LoginResult

PORTAL = 'https://wifi.delta.com'
SIGNIN = 'https://signin.delta.com'
# Public OAuth client identifier embedded in the observed production portal; not a secret.
CLIENT_ID = 'FEVhKePVS3FfeBfxjBVNHmmy'
PREMIUM = 'DalPedPremium'
BASIC_POLICIES = frozenset(('DalPedDefault', 'DalDppPedDefault', 'DalPedCna', 'DalPedBasic', 'DalDppPedCna'))
CONTENT = 'application/vnd.pingidentity.{}+json'


class PortalError(RuntimeError):
    def __init__(self, state, message, retryable=False):
        super().__init__(message)
        self.state, self.retryable = state, retryable


def allowed(url):
    try:
        p = urllib.parse.urlsplit(url)
        return (p.scheme == 'https' and p.hostname in ('wifi.delta.com', 'signin.delta.com')
                and p.port in (None, 443) and p.username is None and p.password is None)
    except ValueError:
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value) or value in ('.', '..', 'null'):
        raise PortalError('schema-changed', 'The portal returned an unrecognized flight, device, or flow identifier; use --mode browser.')
    return urllib.parse.quote(value, safe='')


def pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')
    return verifier, challenge


class Transport:
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(self.cookies))
        self.seed_csrf()

    def seed_csrf(self):
        self.xsrf = secrets.token_hex(32)
        self.cookies.set_cookie(http.cookiejar.Cookie(
            version=0, name='XSRF-TOKEN', value=self.xsrf, port=None, port_specified=False,
            domain='wifi.delta.com', domain_specified=False, domain_initial_dot=False,
            path='/', path_specified=True, secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False))

    def close(self):
        self.cookies.clear()
        # The opener still refers to this jar when watch later rejoins Delta.
        # Keep only a fresh, non-identity CSRF seed for the next activation.
        self.seed_csrf()

    def request(self, method, url, body=None, content_type='application/json', headers=None):
        if not allowed(url):
            raise PortalError('unexpected-origin', 'HTTP endpoint is not an expected Delta HTTPS origin; no request sent.')
        request_headers = {'Accept': 'application/json', 'User-Agent': 'DeltaWiFiCLI/0.3',
                           'Cache-Control': 'no-cache, no-store', 'Origin': PORTAL, 'Referer': PORTAL + '/'}
        if headers:
            request_headers.update(headers)
        payload = None
        if body is not None:
            payload = urllib.parse.urlencode(body).encode() if content_type == 'application/x-www-form-urlencoded' else json.dumps(body).encode()
            request_headers['Content-Type'] = content_type
        elif method == 'GET' and 'pingidentity' in content_type:
            request_headers['Content-Type'] = content_type
        if urllib.parse.urlsplit(url).hostname == 'signin.delta.com':
            request_headers['X-XSRF-Header'] = 'PingFederate'
        elif method != 'GET':
            request_headers['X-XSRF-TOKEN'] = next((c.value for c in self.cookies if c.name == 'XSRF-TOKEN' and c.domain in ('wifi.delta.com', '.wifi.delta.com')), self.xsrf)
        request = urllib.request.Request(url, data=payload, method=method, headers=request_headers)
        try:
            with self.opener.open(request, timeout=12) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise PortalError('schema-changed', 'Portal response is larger than expected; HTTP flow stopped.')
                if not raw:
                    return {}
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise PortalError('interaction-required', 'Portal returned a non-JSON page, possibly an interactive challenge; use --mode browser.') from None
                if not isinstance(data, dict):
                    raise PortalError('schema-changed', 'Portal JSON structure changed; use --mode browser.')
                return data
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if 300 <= status < 400:
                raise PortalError('interaction-required', 'Portal requires a redirect-based flow; HTTP mode stopped. Try --mode browser.') from None
            if status == 429:
                raise PortalError('rate-limited', 'Portal rate limit; login paused.') from None
            if status in (401, 403):
                raise PortalError('authentication-denied', f'Portal denied this request (HTTP {status}); login paused.') from None
            if status >= 500:
                raise PortalError('temporary-failure', f'Portal service error (HTTP {status}).', True) from None
            raise PortalError('request-rejected', f'Portal rejected this HTTP flow (HTTP {status}); use --mode browser.') from None
        except (OSError, urllib.error.URLError):
            raise PortalError('connection-error', 'Portal connection failed or timed out.', True) from None


def policy_label(policy):
    if policy == PREMIUM:
        return 'premium-access'
    if policy in BASIC_POLICIES:
        return 'login-required'
    return 'unknown'


def check_ip(device, local_ip):
    try:
        matches = ipaddress.ip_address(device.get('deviceLanIp', '')) == ipaddress.ip_address(local_ip or '')
    except ValueError:
        matches = False
    if not matches:
        raise PortalError('device-mismatch', 'Portal device IP does not match this laptop Wi-Fi address; refusing to change a phone or another device session.')


def provider_status(local_ip):
    transport = Transport()
    try:
        data = transport.request('GET', PORTAL + '/api/device-details')
        check_ip(data, local_ip)
        policy = data.get('devicePolicy')
        return dict(session=policy_label(policy), device_policy=policy if policy == PREMIUM or policy in BASIC_POLICIES else None)
    except PortalError:
        return dict(session='unknown', device_policy=None)
    finally:
        transport.close()


class HttpPortalClient:
    def __init__(self, probe, credentials, network_check, local_ip, verify_seconds=90, transport=None):
        self.probe, self.credentials = probe, credentials
        self.network_check, self.local_ip = network_check, local_ip
        self.verify_seconds = verify_seconds
        self.http = transport or Transport()
        self.token = None
        self.token_expiry = 0
        self.token_device = None
        self.submitted = False
        self.stage = 'idle'

    def close(self):
        self.token = None
        self.token_expiry = 0
        self.token_device = None
        self.http.close()

    def guard(self):
        if not self.network_check():
            problem = getattr(self.network_check, 'problem', None)
            raise PortalError('network-changed', problem if isinstance(problem, str) else 'Delta Wi-Fi is not confirmed; no request submitted.')

    def device(self):
        data = self.http.request('GET', PORTAL + '/api/device-details')
        check_ip(data, self.local_ip())
        identifier(data.get('deviceId'))
        return data

    def bootstrap(self):
        self.guard()
        self.stage = 'device-discovery'
        device = self.device()
        self.stage = 'flight-discovery'
        flight = self.http.request('GET', PORTAL + '/api/flight-data')
        flight_id, device_id = identifier(flight.get('flightId')), identifier(device['deviceId'])
        return device, flight_id, device_id

    def flow_post(self, flow, action, body):
        return self.http.request('POST', SIGNIN + '/pf-ws/authn/flows/' + identifier(flow),
                                 body, CONTENT.format(action))

    def authenticate(self, account):
        verifier, challenge = pkce()
        params = dict(client_id=CLIENT_ID, response_type='code', scope='openid loyaltyProfileLite.profileLite.read',
                      response_mode='pi.flow', code_challenge_method='S256', code_challenge=challenge, v='1')
        self.stage = 'authorization'
        data = self.http.request('GET', SIGNIN + '/as/authorization.oauth2?' + urllib.parse.urlencode(params),
                                 content_type=CONTENT.format('initiateOAuthAuthorization'))
        sent_credentials = False
        sent_device_information = False
        for _ in range(8):
            status = data.get('status')
            flow = data.get('id')
            if status == 'IS_USER_REMEMBERED' and not sent_device_information:
                self.stage = 'device-information'
                sent_device_information = True
                # The observed portal generates a fresh UUID when no remembered
                # fingerprint/token exists. Never imitate a stored device identity.
                data = self.flow_post(flow, 'submitDeviceInformation', {'fingerprint': str(uuid.uuid4())})
            elif status == 'USERNAME_PASSWORD_REQUIRED':
                if sent_credentials:
                    raise PortalError('credentials-rejected', 'Credentials were not accepted; no repeated password submission.')
                self.guard()
                try:
                    password = self.credentials(account)
                except Exception:
                    raise PortalError('keychain-error', 'Cannot read Keychain credential; run credentials ACCOUNT.') from None
                self.stage = 'credentials'
                self.submitted = sent_credentials = True
                try:
                    data = self.flow_post(flow, 'checkUsernamePassword', dict(username=account, password=password))
                finally:
                    password = None
            elif status == 'SHOULD_REMEMBER_USER':
                self.stage = 'remember-choice'
                data = self.flow_post(flow, 'rememberDevice', {'rememberUser': False})
            elif status == 'REMEMBERED_DEVICE_DETAILS':
                self.stage = 'continue'
                data = self.flow_post(flow, 'submitContinue', {'continue': True})
            elif status == 'COMPLETED':
                code = data.get('authorizeResponse', {}).get('code')
                if not isinstance(code, str) or not code:
                    raise PortalError('schema-changed', 'OAuth completion did not include an authorization code.')
                self.stage = 'token-exchange'
                tokens = self.http.request('POST', SIGNIN + '/as/token.oauth2',
                    dict(grant_type='authorization_code', code=code, code_verifier=verifier, client_id=CLIENT_ID),
                    'application/x-www-form-urlencoded')
                token = tokens.get('access_token')
                expiry = tokens.get('expires_in')
                if not isinstance(token, str) or not token or not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or not math.isfinite(expiry) or expiry <= 0:
                    raise PortalError('schema-changed', 'OAuth token response was not recognized; no activation attempted.')
                self.token = token
                self.token_expiry = time.monotonic() + expiry
                self.stage = 'identity-authenticated'
                return
            elif data.get('error') or data.get('errors'):
                raise PortalError('authentication-denied', 'Authentication returned an error; no password retry. Try --mode browser for details.')
            else:
                # Includes MFA, fingerprint challenges, enrollment, and future states.
                raise PortalError('interaction-required', 'OAuth requested an unsupported or interactive step; use --mode browser. No challenge bypass attempted.')
        raise PortalError('interaction-required', 'OAuth flow exceeded the expected number of steps; use --mode browser.')

    def recover(self, account, force=False):
        self.submitted = False
        if self.token_device and self.token_device[3] != account:
            self.close()
        try:
            device, flight, device_id = self.bootstrap()
            if device.get('devicePolicy') == PREMIUM and not force:
                internet, captive = self.probe()
                self.guard()
                if internet == 'online' and not captive:
                    return LoginResult('online', 'Delta reports premium access and general-web access is verified; no login submitted.')
                return LoginResult('session-active', 'Delta reports premium access, but general-web checks fail. Preserving the session; investigate upstream connectivity.')
            if device.get('devicePolicy') != PREMIUM and device.get('devicePolicy') not in BASIC_POLICIES:
                raise PortalError('unsupported-policy', 'Device has an unrecognized policy; refusing to change its access.')
            identity = (flight, device_id, self.local_ip(), account)
            if not self.token or self.token_expiry <= time.monotonic() + 5 or self.token_device != identity:
                self.authenticate(account)
                self.token_device = identity
            self.guard()
            fresh = self.device()
            if fresh['deviceId'] != device['deviceId'] or self.local_ip() != identity[2]:
                raise PortalError('device-mismatch', 'Device changed during authentication; access activation stopped.')
            self.stage = 'activation'
            self.http.request('POST', f'{PORTAL}/api/portableElectronicDevice/v1/flights/{flight}/devices/{device_id}/sessions',
                              {'type': 'freeWithSkyMiles'}, headers={'UserAuth': self.token})
            self.stage = 'verification'
            deadline = time.monotonic() + self.verify_seconds
            while time.monotonic() < deadline:
                self.guard()
                current = self.device()
                if current['deviceId'] != device['deviceId']:
                    raise PortalError('device-mismatch', 'Portal switched devices during verification; outcome unknown.')
                if current.get('devicePolicy') == PREMIUM:
                    internet, captive = self.probe()
                    self.guard()
                    if internet == 'online' and not captive:
                        return LoginResult('online', 'SkyMiles OAuth completed; Delta premium access and general-web connectivity verified (HTTP only).', submitted=self.submitted)
                time.sleep(2)
            return LoginResult('unconfirmed', 'HTTP login/activation was submitted but access was not fully verified; no automatic password retry.', submitted=self.submitted)
        except PortalError as exc:
            retryable = exc.retryable
            # A broken connection after the password POST may have processed it.
            if self.submitted and exc.state == 'connection-error' and not self.token:
                retryable = False
            return LoginResult(exc.state, f'{exc} (HTTP step: {self.stage})', retryable, self.submitted)

    def info(self):
        try:
            device, _, _ = self.bootstrap()
            policy = device.get('devicePolicy')
            return dict(adapter='delta-ping-pkce-http-v1', mode='http', session=policy_label(policy),
                        device_policy=policy if policy == PREMIUM or policy in BASIC_POLICIES else None,
                        device_matches_local_ip=True, identity_session='not-checked',
                        credentials_read=False)
        except PortalError as exc:
            return dict(adapter='delta-ping-pkce-http-v1', state=exc.state, message=str(exc), credentials_read=False)

    def logout(self, dry_run=False, before_reset=None):
        """Reset this onboard device to the portal's default policy; never global SkyMiles SLO."""
        try:
            device, flight, device_id = self.bootstrap()
            policy = device.get('devicePolicy')
            if policy != PREMIUM and policy not in BASIC_POLICIES:
                raise PortalError('unsupported-policy', 'Device has an unrecognized policy; access was not changed.')
            if dry_run:
                return LoginResult('ready', 'Logout preflight passed: laptop IP matches portal device. Would POST default policy to this device; no session changed.')
            if before_reset:
                before_reset()
            if policy in BASIC_POLICIES:
                return LoginResult('logged-out', 'Delta already reports basic/default access for this device; no reset submitted.')
            self.guard()
            fresh = self.device()
            if fresh['deviceId'] != device['deviceId']:
                raise PortalError('device-mismatch', 'Device changed before logout; no reset submitted.')
            self.http.request('POST', f'{PORTAL}/api/portableElectronicDevice/v1/flights/{flight}/devices/{device_id}/sessions', {'type': 'default'})
            deadline = time.monotonic() + self.verify_seconds
            while time.monotonic() < deadline:
                self.guard()
                current = self.device()
                if current['deviceId'] != device['deviceId']:
                    raise PortalError('device-mismatch', 'Device changed while checking logout; result unknown.')
                if current.get('devicePolicy') in BASIC_POLICIES:
                    return LoginResult('logged-out', 'Delta confirms this device is back on basic/default access. Saved Keychain credentials were kept.')
                time.sleep(2)
            return LoginResult('unconfirmed', 'Reset was submitted but Delta did not confirm basic/default policy. Logout is unconfirmed.')
        except PortalError as exc:
            # Logout is a single explicit operation; never automatically retry a reset POST.
            return LoginResult(exc.state, str(exc))
