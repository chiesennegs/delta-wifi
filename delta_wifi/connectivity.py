"""Fresh HTTPS evidence. Connectivity-check endpoints alone never prove general access."""
import concurrent.futures
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str
    purpose: str
    status: int
    markers: tuple[bytes, ...]


POLICY = 'two-of-three-general-web-passes-no-portal-redirect-v2'

ENDPOINTS = (
    Endpoint('apple-check', 'https://www.apple.com/library/test/success.html', 'connectivity-check', 200, (b'Success',)),
    Endpoint('google-check', 'https://connectivitycheck.gstatic.com/generate_204', 'connectivity-check', 204, ()),
    Endpoint('cloudflare-web', 'https://www.cloudflare.com/cdn-cgi/trace', 'general-web', 200,
             (b'h=www.cloudflare.com', b'visit_scheme=https')),
    Endpoint('wikipedia-web', 'https://www.wikipedia.org/', 'general-web', 200, (b'Wikipedia',)),
    Endpoint('python-web', 'https://www.python.org/robots.txt', 'general-web', 200, (b'User-agent:', b'Disallow:')),
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def error_kind(exc):
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, (ssl.SSLCertVerificationError, ssl.SSLError)):
        return 'tls-error'
    if isinstance(reason, socket.gaierror):
        return 'dns-error'
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'timeout'
    return 'connection-error'


def same_origin(first, second):
    try:
        a, b = urllib.parse.urlsplit(first), urllib.parse.urlsplit(second)
        return (b.scheme == 'https' and b.username is None and b.password is None
                and (a.scheme, a.hostname, a.port or 443) == (b.scheme, b.hostname, b.port or 443))
    except ValueError:
        return False


def portal_host(host):
    host = (host or '').lower()
    return host == 'wifi.delta.com' or host == 'deltawifi.com' or host.endswith('.deltawifi.com')


def check(endpoint):
    started = time.monotonic()
    deadline = started + 8
    query = urllib.parse.urlencode({'delta_wifi_probe': uuid.uuid4().hex})
    url = endpoint.url + ('&' if '?' in endpoint.url else '?') + query
    result = dict(name=endpoint.name, url=endpoint.url, purpose=endpoint.purpose,
                  result='connection-error', status_code=None, elapsed_ms=None,
                  response_bytes=0, redirect_host=None, redirects=[], error_type=None, error_errno=None)
    opener = urllib.request.build_opener(NoRedirect)
    # At most three same-origin HTTPS redirects, under a shared request budget.
    for hop in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result['result'] = 'timeout'
            break
        request = urllib.request.Request(url, headers={
            'Cache-Control': 'no-cache, no-store, max-age=0', 'Pragma': 'no-cache',
            'User-Agent': 'DeltaWiFiCLI/0.2 (connection diagnostics)', 'Accept-Encoding': 'identity'})
        try:
            with opener.open(request, timeout=min(6, remaining)) as response:
                body = response.read(65536)
                result['status_code'] = response.status
                result['response_bytes'] = len(body)
                expected = response.status == endpoint.status and all(marker in body for marker in endpoint.markers)
                if endpoint.status == 204:
                    expected = expected and not body
                result['result'] = 'pass' if expected else 'unexpected-response'
            break
        except urllib.error.HTTPError as exc:
            result['status_code'] = exc.code
            location = exc.headers.get('Location')
            status = exc.code
            exc.close()
            if not (300 <= status < 400 and location):
                result['result'] = 'http-error'
                break
            try:
                target = urllib.parse.urljoin(url, location)
                host = urllib.parse.urlsplit(target).hostname
            except ValueError:
                result['result'] = 'invalid-redirect'
                break
            normal = same_origin(endpoint.url, target)
            result['redirect_host'] = host
            result['redirects'].append(dict(status_code=status, host=host, same_origin_https=normal))
            if not normal:
                result['result'] = 'portal-redirect' if portal_host(host) else 'external-redirect'
                break
            if hop == 3:
                result['result'] = 'redirect-limit'
                break
            url = target
        except (OSError, urllib.error.URLError) as exc:
            reason = getattr(exc, 'reason', exc)
            result['result'] = error_kind(exc)
            result['error_type'] = type(reason).__name__
            number = getattr(reason, 'errno', None)
            result['error_errno'] = number if isinstance(number, int) else None
            break
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return result


def classify(results):
    general_passes = sum(item['purpose'] == 'general-web' and item['result'] == 'pass' for item in results)
    captive = any(item['result'] == 'portal-redirect' for item in results)
    # A normal canonical redirect is not a captive portal. A failing general site
    # also cannot veto two independent general sites that actually respond.
    if captive:
        return 'captive', True
    if general_passes >= 2:
        return 'online', False
    if any(item['result'] == 'pass' for item in results):
        return 'limited', False
    return 'unavailable', False


def sample():
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
        results = list(pool.map(check, ENDPOINTS))
    internet, captive = classify(results)
    return dict(internet=internet, captive_evidence=captive, probes=results,
                policy=POLICY)
