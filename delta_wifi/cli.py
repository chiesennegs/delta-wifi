"""macOS connection observer with a deliberately opt-in portal form adapter."""
import argparse
import datetime as dt
import getpass
import json
import platform
import random
import re
import shlex
import subprocess
import math
import time
import textwrap
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from . import __version__
from . import native_helper, storage, network, notifications, connectivity
from .monitor import Timeline, Panel
from .portal import PortalClient
from .http_portal import HttpPortalClient, provider_status
from .recovery import RetryPolicy

SSID = 'DeltaWiFi.com'
PORTAL = 'https://wifi.delta.com/'
SERVICE = 'delta-wifi.skymiles'



def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=12, **kwargs)


def trusted(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and parsed.port in (None, 443) and any(
        host == domain or host.endswith('.' + domain)
        for domain in ('delta.com', 'deltawifi.com'))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url):
    try:
        with urllib.request.build_opener(NoRedirect).open(url, timeout=6) as response:
            return response.status, response.read(65536), None
    except urllib.error.HTTPError as exc:
        return exc.code, b'', exc.headers.get('Location')
    except (OSError, urllib.error.URLError):
        return None, b'', None


def association():
    if platform.system() != 'Darwin':
        return None, None, 'macOS is required for SSID detection'
    native = None
    try:
        native = native_helper.read()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if native and native.get('ssid') and native.get('interface'):
        ip = run(['ipconfig', 'getifaddr', native['interface']]).stdout.strip() or None
        return native['ssid'], ip, None
    hardware = run(['networksetup', '-listallhardwareports']).stdout
    match = re.search(r'Hardware Port: (?:Wi-Fi|AirPort)\nDevice: (\S+)', hardware)
    if not match:
        return None, None, 'Wi-Fi interface unavailable'
    interface = match.group(1)
    result = run(['networksetup', '-getairportnetwork', interface])
    line = result.stdout.strip()
    name = line.split(': ', 1)[1] if line.startswith('Current Wi-Fi Network: ') else None
    if name and name.lower() in ('<redacted>', 'redacted', '<unknown>', 'unknown'):
        name = None
    ip = run(['ipconfig', 'getifaddr', interface]).stdout.strip() or None
    note = None if name else 'SSID unavailable; run delta-wifi wifi-perms to enable native detection'
    if not name and native:
        note = ('Native SSID unavailable; Location Services: '
                + ('enabled' if native.get('location_services') else 'disabled')
                + '; permission: ' + native.get('authorization', 'unknown'))
    return name, ip, note


@dataclass
class Status:
    timestamp: str
    wifi: str
    ssid: str | None
    local_ip: str | None
    session: str
    internet: str
    dns: str
    portal: str
    note: str | None
    wifi_interface: str | None = None
    gateway: str | None = None
    gateway_status: str = 'not-tested'
    route_interface: str | None = None
    route_status: str = 'not-tested'
    vpn: str = 'not-tested'
    proxy: str = 'not-tested'
    last_online: str | None = None
    provider_policy: str | None = None


def probe_internet():
    result = connectivity.sample()
    return result['internet'], result['captive_evidence']


def connectivity_report():
    before = association()
    result = connectivity.sample()
    details = network.diagnostics(before[1]) if before[1] else {}
    after = association()
    changed = before[:2] != after[:2]
    if changed:
        result['internet'] = 'network-changed'
        result['captive_evidence'] = False
    return dict(timestamp=dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                network_before=dict(ssid=before[0], has_local_ip=bool(before[1])),
                network_after=dict(ssid=after[0], has_local_ip=bool(after[1])),
                network_changed=changed,
                route_status=details.get('route_status', 'unknown'),
                route_interface=details.get('route_interface'),
                vpn=details.get('vpn', 'unknown'), proxy=details.get('proxy', 'unknown'),
                dns=details.get('dns', 'unknown'), gateway_status=details.get('gateway_status', 'unknown'),
                **result)


def network_allowed(ssid, ip, assume_delta=False):
    # An explicit confirmation only fills missing evidence; it cannot override a known SSID.
    return ssid == SSID or (assume_delta and ssid is None and bool(ip))


def inspect(assume_delta=False):
    ssid, ip, note = association()
    confirmed = assume_delta and ssid is None and bool(ip)
    if confirmed:
        note = 'Delta network confirmed by user; SSID is not independently verified'
    # Avoid probing or acting on an unrelated network.
    if not network_allowed(ssid, ip, assume_delta):
        return Status(dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                      'other-network' if ssid else 'connected-unidentified' if ip else 'unknown', ssid, ip,
                      'unknown', 'not-tested', 'not-tested', 'not-tested', note)
    if not ip:
        return Status(dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                      'connected', ssid, None, 'unknown', 'not-tested', 'not-tested', 'not-tested',
                      'Associated with Wi-Fi; waiting for a local IP address.')
    details = network.diagnostics(ip)
    internet, captive = probe_internet()
    code, body, redirect = fetch(PORTAL)
    portal = 'reachable' if code is not None else 'unreachable'
    lower = body.lower()
    login_form = b'<form' in lower and (b'password' in lower or b'skymiles' in lower)
    provider = provider_status(ip)
    session = provider['session']
    if session == 'unknown' and (captive or login_form):
        session = 'login-required'
    if internet == 'online' and captive:
        internet = 'captive'
    explanations = []
    if note:
        explanations.append(note)
    if details['route_status'] in ('vpn-route', 'other-interface') or details['proxy'] == 'configured':
        explanations.append('External checks may use a VPN, proxy, or another interface; they do not prove Delta internet access.')
    if details['dns'] == 'failed':
        explanations.append('External DNS lookups failed; the Delta portal may still be reachable.')
    elif details['gateway_status'] == 'responding' and portal == 'reachable' and internet == 'unavailable':
        explanations.append('Local gateway and portal respond; external internet checks are failing.')
    elif details['gateway_status'] == 'no-reply':
        explanations.append('Gateway did not answer ICMP; it may filter pings.')
    if internet in ('limited', 'degraded'):
        explanations.append('Only selected destinations respond; general internet access and login are not confirmed.')
    note = ' '.join(explanations) or None
    after = association()
    if after[:2] != (ssid, ip):
        return Status(dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                      'changing', after[0], after[1], 'unknown', 'network-changed', 'not-tested', 'not-tested',
                      'Network changed during the checks; discard this sample and check again.')
    return Status(dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                  'user-confirmed' if confirmed else 'connected', ssid, ip, session, internet,
                  details.pop('dns'), portal, note, provider_policy=provider['device_policy'], **details)


def credentials(account, password=None):
    if platform.system() != 'Darwin':
        raise RuntimeError('Keychain storage requires macOS')
    if password is None:
        result = run(['security', 'find-generic-password', '-s', SERVICE, '-a', account, '-w'])
        if result.returncode:
            raise RuntimeError('No accessible Keychain entry; run credentials first')
        return result.stdout.rstrip('\n')
    if any(c in password for c in ('\n', '\r')):
        raise RuntimeError('Passwords with line breaks are not supported by this Keychain adapter')
    # security interactive mode keeps the password out of the process argument list.
    command = shlex.join(['add-generic-password', '-U', '-s', SERVICE, '-a', account, '-w', password])
    result = run(['security', '-i'], input=command + '\n')
    if result.returncode or 'error' in result.stderr.lower() or 'SecKeychain' in result.stderr:
        raise RuntimeError('Keychain did not accept the credential')


def portal_client(assume_delta=False, headless=False, browser_name="chrome", verify_timeout=90, mode="http", honor_pause=False):
    def network_check():
        if honor_pause and storage.recovery_paused():
            network_check.problem = 'Automatic recovery paused by logout; run login or restart watch --auto-login to resume.'
            return False
        ssid, ip, _ = association()
        network_check.problem = 'Delta Wi-Fi or a local Wi-Fi address is unavailable; waiting.'
        network_check.retryable = True
        if not network_allowed(ssid, ip, assume_delta) or not ip:
            return False
        detail = network.diagnostics(ip)
        if detail['route_status'] in ('vpn-route', 'other-interface') or detail['proxy'] == 'configured':
            network_check.problem = 'Automatic login suspended: a VPN, proxy, or another interface may mask Delta connectivity.'
            network_check.retryable = False
            return False
        return True
    if mode == 'http':
        return HttpPortalClient(probe_internet, credentials, network_check, lambda: association()[1], verify_timeout)
    return PortalClient(PORTAL, trusted, probe_internet, credentials, network_check, headless, browser_name=browser_name, verify_seconds=verify_timeout)


def event(message, json_mode=False, **details):
    if json_mode:
        print(json.dumps(dict(event='recovery', message=message, **details)), flush=True)
    else:
        print('  Recovery: ' + message, flush=True)


def login(account, assume_delta=False, max_attempts=3, retry_delay=30, headless=False, browser_name="chrome", force=False, verify_timeout=90, mode="http"):
    policy = RetryPolicy(max_attempts, retry_delay)
    client = portal_client(assume_delta, headless, browser_name, verify_timeout, mode)
    try:
        while True:
            storage.record('recovery-attempt', attempt=policy.attempts + 1)
            started = time.monotonic()
            result = client.recover(account, force=force)
            if result.submitted:
                storage.record('login-attempt', attempt=policy.attempts + 1)
            storage.record('login-result', state=result.state, submitted=result.submitted,
                           duration_seconds=round(time.monotonic() - started))
            event(result.message)
            policy.record(result, time.monotonic())
            if result.state in ('online', 'session-active'):
                return
            if policy.paused:
                raise RuntimeError('Login paused (' + policy.paused + '). Resolve the issue before restarting.')
            delay = max(0, policy.next_at - time.monotonic())
            event(f'Retrying in {delay:.0f}s; {policy.attempts}/{max_attempts} attempts used.')
            # Short waits keep Ctrl+C responsive and avoid a long uninterruptible delay.
            while time.monotonic() < policy.next_at:
                time.sleep(min(1, policy.next_at - time.monotonic()))
    finally:
        client.close()


def watch(args):
    policy = RetryPolicy(args.max_attempts, args.retry_delay)
    client = portal_client(args.assume_delta, args.headless, args.browser, getattr(args, 'verify_timeout', 90), getattr(args, 'mode', 'http'), honor_pause=True)
    notifier = notifications.Notifier(getattr(args, 'notify', False))
    timeline = Timeline(notify=notifier.send, threshold=getattr(args, 'notify_after', 60),
                        last_online=storage.last_access(), save_online=storage.set_last_access)
    panel = Panel(enabled=not args.json and getattr(args, 'display', 'auto') != 'log')
    failures = 0
    last_result = None
    storage.record('watch-start')
    try:
        while True:
            state = inspect(args.assume_delta)
            now = time.monotonic()
            timeline.observe(state, now)
            was_paused = policy.paused
            policy.observe(state.internet)
            if was_paused and not policy.paused:
                timeline.action_notified = False
            suspended = storage.recovery_paused()
            runtime = timeline.runtime(state, policy, now, args.auto_login, last_result)
            if suspended:
                runtime['action'] = 'Automatic recovery paused by logout. Run login or restart watch --auto-login to resume.'
            if not panel.enabled:
                display(state, args.json, runtime)
            else:
                panel.render(state, runtime)
            if state.internet == 'online':
                failures = 0
            elif state.wifi in ('connected', 'user-confirmed') and state.local_ip:
                failures = min(failures + 1, 5)
                evidence = state.session == 'login-required' or failures >= 2
                safe_route = state.route_status not in ('vpn-route', 'other-interface') and state.proxy != 'configured'
                if args.auto_login and not suspended and state.session != 'premium-access' and evidence and safe_route and (state.dns != 'failed' or state.portal == 'reachable') and policy.ready(time.monotonic()):
                    attempt_number = policy.attempts + 1
                    storage.record('recovery-attempt', attempt=attempt_number)
                    if not panel.enabled:
                        event(f'Checking login flow; attempt {policy.attempts + 1}/{args.max_attempts}.', args.json)
                    else:
                        panel.render(state, {**runtime, 'action': 'Inspecting the portal and checking login; this may take a minute.'})
                    started = time.monotonic()
                    last_result = client.recover(args.auto_login)
                    policy.record(last_result, time.monotonic())
                    if last_result.submitted:
                        storage.record('login-attempt', attempt=attempt_number)
                    storage.record('login-result', state=last_result.state, paused=policy.paused,
                                   submitted=last_result.submitted, duration_seconds=round(time.monotonic() - started))
                    if not panel.enabled:
                        event(last_result.message, args.json, state=last_result.state, attempts=policy.attempts,
                              paused=policy.paused,
                              retry_in_seconds=round(max(0, policy.next_at - time.monotonic())) if not policy.paused else None)
                    if policy.paused:
                        timeline.attention()
                    if last_result.state == 'online':
                        failures = 0
            else:
                failures = 0
                client.close()
            delay = min(120, args.interval * 2 ** max(0, failures - 1)) * random.uniform(0.9, 1.1)
            if args.auto_login and not policy.paused and failures and policy.next_at > time.monotonic():
                delay = min(delay, max(1, policy.next_at - time.monotonic()))
            runtime = timeline.runtime(state, policy, time.monotonic(), args.auto_login, last_result)
            if storage.recovery_paused():
                runtime['action'] = 'Automatic recovery paused by logout. Run login or restart watch --auto-login to resume.'
            if notifier.problem:
                runtime['notification_note'] = notifier.problem
            if not panel.enabled:
                if args.json:
                    print(json.dumps(dict(event='next-check', seconds=round(delay), **runtime)), flush=True)
                else:
                    event(runtime['action'] + f' Next check in {delay:.0f}s.')
                    if notifier.problem:
                        print('  ' + notifier.problem, flush=True)
                time.sleep(delay)
            else:
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    runtime = timeline.runtime(state, policy, time.monotonic(), args.auto_login, last_result)
                    if storage.recovery_paused():
                        runtime['action'] = 'Automatic recovery paused by logout. Run login or restart watch --auto-login to resume.'
                    if notifier.problem:
                        runtime['action'] += ' ' + notifier.problem
                    panel.render(state, runtime, remaining)
                    time.sleep(min(1, remaining))
    finally:
        panel.close()
        client.close()
        timeline.stop(time.monotonic())
        storage.record('watch-stop')


def display(status, json_mode=False, runtime=None):
    if json_mode:
        print(json.dumps(dict(asdict(status), **(runtime or {}))), flush=True)
    else:
        print(f'{status.timestamp}  Wi-Fi: {status.wifi} ({status.ssid or "SSID unavailable"})\n'
              f'  Session: {status.session} | Internet: {status.internet}\n'
              f'  Local IP: {status.local_ip or "unknown"} | DNS: {status.dns} | Portal: {status.portal}\n'
              f'  Gateway: {status.gateway_status} | Route: {status.route_status} ({status.route_interface or "unknown"})\n'
              f'  VPN: {status.vpn} | Proxy: {status.proxy} | Provider policy: {status.provider_policy or "unknown"}', flush=True)
        if status.last_online:
            print('  Last observed access: ' + status.last_online, flush=True)
        if status.note:
            print('  ' + status.note, flush=True)
        if runtime:
            outage = runtime['outage_seconds']
            print(f'  Last access: {runtime["last_online"] or "not observed yet"} | Outage: {outage if outage is not None else 0}s', flush=True)


class CommandHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_argument(self, action):
        super().add_argument(action)
        # Reserve enough room for nested command names even when the subparser
        # metavar is the short word COMMAND.
        self._action_max_length = max(self._action_max_length, 20)


def build_parser():
    parser = argparse.ArgumentParser(
        prog='delta-wifi',
        description='Monitor Delta Wi-Fi, check internet access, and recover your SkyMiles login.',
        formatter_class=CommandHelpFormatter,
        epilog="""Quick start:
  delta-wifi wifi-perms                       Enable native Wi-Fi name detection
  delta-wifi status                           Check the current connection
  delta-wifi credentials YOUR_ACCOUNT         Save your password in Keychain
  delta-wifi watch --auto-login               Use the saved account to recover

Use 'delta-wifi COMMAND --help' for options and examples.
Join DeltaWiFi.com in the macOS Wi-Fi menu before checking or logging in.
Press Ctrl+C to stop monitoring. Network settings are never changed.""")
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    sub = parser.add_subparsers(dest='command', required=True, title='commands', metavar='COMMAND')

    def command(name, summary, description, examples):
        return sub.add_parser(name, help=summary, description=textwrap.fill(description, width=78),
                              formatter_class=argparse.RawDescriptionHelpFormatter,
                              epilog=examples)

    def network_option(cmd):
        cmd.add_argument('--assume-delta', action='store_true', help=(
            'Confirm you joined DeltaWiFi.com when macOS hides the SSID. Requires a '
            'Wi-Fi IP; cannot override a known different network. Stop watch before changing networks.'))

    def browser_options(cmd):
        cmd.add_argument('--mode', choices=('http', 'browser'), default='http', help=(
            'http performs OAuth, access activation, and checks without launching a browser '
            '(default). browser uses the optional browser adapter; it is never launched as an automatic fallback.'))
        cmd.add_argument('--browser', choices=('chrome', 'canary'), default='chrome', help=(
            'Browser used only with --mode browser, in a separate temporary profile. '
            'Use canary for development (default: chrome).'))
        cmd.add_argument('--headless', action='store_true', help=(
            'With --mode browser, run without a visible window. '
            'Omit while troubleshooting (default: show window).'))

    def retry_options(cmd):
        cmd.add_argument('--verify-timeout', type=float, default=90, metavar='SECONDS', help=(
            'Verification window after login submission; browser mode includes video/Skip steps '
            '(5–300; default: 90). Individual network checks can add time. '
            'Exits early when general-web access is verified.'))
        cmd.add_argument('--max-attempts', type=int, default=3, metavar='N', help=(
            'Maximum failed recovery attempts per outage, including the first attempt '
            '(1–10; default: 3). Rejections and unknown outcomes pause sooner.'))
        cmd.add_argument('--retry-delay', type=float, default=30, metavar='SECONDS', help=(
            'Initial delay after a retryable failure (5–300; default: 30). '
            'Doubles between attempts, capped at 300 seconds before jitter.'))

    cmd = command('status', 'Show one snapshot of Wi-Fi, session, and internet status',
                  'Check the Wi-Fi network, local address, gateway response, DNS resolution, VPN/proxy and route evidence, portal, and independent HTTPS evidence. '
                  'Working internet does not by itself prove SkyMiles authentication. '
                  'Other networks are not probed. This command never submits credentials.',
                  'Examples:\n  delta-wifi status\n  delta-wifi status --json')
    network_option(cmd)
    cmd.add_argument('--json', action='store_true', help='Print one JSON status object instead of readable text.')

    cmd = command('diagnose', 'Print a shareable JSON connection diagnostic',
                  'Print the same connection checks as status, always as JSON. Includes the '
                  'SSID, local IP, timestamp, and status labels; excludes account identifiers, '
                  'passwords, cookies, and portal HTML. Review before sharing. Never submits credentials.',
                  'Example:\n  delta-wifi diagnose > diagnostic.json')
    network_option(cmd)
    cmd.add_argument('--json', action='store_true', help='Accepted for consistency; diagnose always prints JSON.')

    cmd = command('watch', 'Monitor continuously, with optional automatic login recovery',
                  'Wait for DeltaWiFi.com, then repeat connection checks until Ctrl+C. Leaving the network returns to waiting. Without --auto-login, only monitor. '
                  'With --auto-login, use the HTTP OAuth/activation flow during access failures by default. Browser mode is explicit. Existing premium sessions are preserved during upstream outages.',
                  """Examples:
  delta-wifi watch
  delta-wifi watch --auto-login YOUR_ACCOUNT
  delta-wifi watch --auto-login YOUR_ACCOUNT --mode browser --browser canary
  delta-wifi watch --auto-login YOUR_ACCOUNT --json

Recovery behavior:
  Temporary load/server errors retry with backoff and jitter. Rejected credentials,
  rate limits, verification challenges, and uncertain results after submission
  pause login attempts; monitoring continues. Three fully online observations
  reset a temporary attempt limit. User-action errors require resolving the issue
  and restarting watch. Save credentials with 'credentials YOUR_ACCOUNT' first.

JSON output is one object per line: connection snapshots and recovery events.
Ctrl+C ends monitoring and clears identity state; browser mode also closes its temporary browser.""")
    network_option(cmd)
    cmd.add_argument('--json', action='store_true', help='Print newline-delimited JSON status snapshots and recovery events.')
    cmd.add_argument('--interval', type=float, default=20, metavar='SECONDS', help=(
        'Base monitoring interval (5–300; default: 20). Outages increase the wait '
        'with jitter; recovery deadlines may shorten it. Checks take additional time.'))
    cmd.add_argument('--auto-login', nargs='?', const=True, metavar='ACCOUNT', help=(
        'Enable login recovery. Optionally supply an account; otherwise use the preferred '
        'account saved by credentials. Omit the option to monitor without logging in.'))
    cmd.add_argument('--display', choices=('auto', 'panel', 'log'), default='auto', help=(
        'auto uses a compact updating panel in a terminal and scrolling text when piped. '
        'Use log to retain scrolling output (default: auto). --json always disables the panel.'))
    cmd.add_argument('--notify', action='store_true', help=(
        'Notify once when login needs attention or a sustained outage ends. '
        'Run notify-perms first; routine retries stay quiet.'))
    cmd.add_argument('--notify-after', type=float, default=60, metavar='SECONDS', help=(
        'Minimum outage duration for a recovery notification (10–3600; default: 60).'))
    retry_options(cmd)
    browser_options(cmd)

    cmd = command('credentials', 'Save or update a SkyMiles password in macOS Keychain',
                  'Privately prompt for your existing SkyMiles password and save it in '
                  'macOS Keychain for the given account. Updates an existing saved entry. '
                  'Remembers this account for future login and watch commands. Does not log in or change your Delta password. Do not put a password on the command line.',
                  'Examples:\n  delta-wifi credentials YOUR_ACCOUNT\n  delta-wifi credentials')
    cmd.add_argument('account', nargs='?', metavar='ACCOUNT', help='SkyMiles number or username. Uses the preferred account, or prompts if none is saved.')

    cmd = command('login', 'Log in now, retrying temporary failures within a fixed limit',
                  'Use the preferred account (or an explicit ACCOUNT) and its Keychain password for '
                  'browser-free OAuth/PKCE login, then activate this device and verify web access. '
                  'Use --mode browser for the optional UI adapter. Premium access is preserved unless --force is requested. Exit after recovery or when attempts are paused/exhausted; '
                  'use watch --auto-login for ongoing recovery.',
                  """Examples:
  delta-wifi login YOUR_ACCOUNT
  delta-wifi login YOUR_ACCOUNT --mode browser --browser canary

Save credentials first with 'delta-wifi credentials YOUR_ACCOUNT'.
Rejected credentials, rate limits, challenges, and unknown outcomes after submission
stop retries. Resolve the reported issue before running login again.
HTTP mode never launches a browser. With --mode browser, the temporary browser closes on exit.""")
    cmd.add_argument('account', nargs='?', metavar='ACCOUNT', help='Account with a password already in Keychain; defaults to the preferred saved account.')
    cmd.add_argument('--force', action='store_true', help=(
        'Request login/activation even when this device already has access. '
        'For ambiguous access; origin, network, rejection, and retry safeguards still apply.'))
    network_option(cmd)
    retry_options(cmd)
    browser_options(cmd)

    cmd = command('portal-info', 'Inspect provider access or the optional browser login layout',
                  'In default HTTP mode, read the current device policy and verify it matches '
                  'the laptop Wi-Fi IP. With --mode browser, inspect the login dialog and field counts. '
                  'Never read Keychain or submit credentials.',
                  'Examples:\n  delta-wifi portal-info\n  delta-wifi portal-info --mode browser --browser canary')
    network_option(cmd)
    browser_options(cmd)

    cmd = command('open-portal', 'Open Delta Wi-Fi in your system default browser',
                  'Open https://wifi.delta.com/ in the macOS default browser for manual login. '
                  'Requires a confirmed Delta Wi-Fi connection. Does not fill credentials or '
                  'control the browser afterward.',
                  'Example:\n  delta-wifi open-portal')
    network_option(cmd)

    command('wifi-perms', 'Enable native Wi-Fi name detection on macOS',
            'Build the native Wi-Fi helper if needed, then request Location Services '
            'permission so CoreWLAN can read the connected SSID. No geographic '
            'coordinates are requested. Prints the permission and Wi-Fi detection result. '
            'Building requires Apple Command Line Tools.',
            """Example:
  delta-wifi wifi-perms

Allow 'Delta WiFi Helper' when prompted. If previously denied, enable it under
System Settings > Privacy & Security > Location Services, then run status again.""")
    cmd = command('logout', 'Reset this device to basic/default onboard access for testing',
                  'Use the portal reset API to end this laptop device premium Wi-Fi access. '
                  'Does not log out your global Delta account, remove Keychain credentials, '
                  'or operate on the phone hotspot. Requires DeltaWiFi.com and a portal device '
                  'IP matching the laptop. Stop any in-progress login first. Existing watch '
                  'loops are paused; run login or restart watch --auto-login to resume.',
                  'Examples:\n  delta-wifi logout --dry-run\n  delta-wifi logout')
    network_option(cmd)
    cmd.add_argument('--dry-run', action='store_true', help='Read-only preflight; do not reset access or pause recovery.')
    cmd.add_argument('--verify-timeout', type=float, default=30, metavar='SECONDS', help='Wait for provider confirmation after reset (5–300; default: 30).')
    cmd.add_argument('--json', action='store_true', help='Print the logout result as JSON without device or flight identifiers.')

    cmd = command('connectivity', 'Explain which HTTPS checks pass on the current network',
                  'Run fresh, independent HTTPS checks on any SSID, including a phone hotspot. '
                  'Report endpoint status, timing, redirect hostname, and safe error categories. '
                  'Never read Keychain, submit credentials, or change the network. No response '
                  'bodies, query strings, public IPs, or cookies are included. A network change '
                  'during a sample invalidates its result.',
                  'Examples:\n  delta-wifi connectivity\n  delta-wifi connectivity --json > before-login.json\n  delta-wifi connectivity --samples 3 --json > repeated-checks.json')
    cmd.add_argument('--json', action='store_true', help='Print a JSON object containing the sample list, for comparison or sharing.')
    cmd.add_argument('--samples', type=int, default=1, metavar='N', help='Number of fresh samples (1–10; default: 1).')
    cmd.add_argument('--interval', type=float, default=5, metavar='SECONDS', help='Wait between samples (2–60; default: 5).')

    cmd = command('account', 'Inspect or remove saved account credentials',
                  'Show the preferred account and whether its Keychain entry exists, without '
                  'reading the password. Select an existing Keychain account with use, or remove it with forget and clear '
                  'that preference. This never changes your Delta account or password.',
                  'Examples:\n  delta-wifi account\n  delta-wifi account use YOUR_ACCOUNT\n  delta-wifi account forget\n  delta-wifi account forget OTHER_ACCOUNT')
    cmd.add_argument('action', nargs='?', choices=('show', 'use', 'forget'), default='show', help='Show saved account information, select an existing entry with use, or forget an entry (default: show).')
    cmd.add_argument('account', nargs='?', metavar='ACCOUNT', help='Account to inspect, select, or forget; defaults to the preferred account.')

    cmd = command('history', 'Review local connection changes, outages, and login attempts',
                  'Show recent local events in chronological order. History is limited to '
                  '5,000 events and excludes usernames, passwords, cookies, SSIDs, and IP addresses. '
                  'Only watch and login operations add events. Preferences and history live '
                  'under ~/Library/Application Support/delta-wifi with private permissions.',
                  'Examples:\n  delta-wifi history\n  delta-wifi history --limit 100 --json\n  delta-wifi history --clear')
    cmd.add_argument('--limit', type=int, default=50, metavar='N', help='Show the latest N events (1–5000; default: 50).')
    cmd.add_argument('--json', action='store_true', help='Print events as a JSON array.')
    cmd.add_argument('--clear', action='store_true', help='Remove saved event history; keep the account preference and Keychain credentials.')

    command('notify-perms', 'Enable optional macOS notifications for connection monitoring',
            'Build a separate notification helper if needed and request notification '
            'permission. This does not rebuild the Wi-Fi helper or change its Location '
            'Services authorization. After allowing notifications, use watch --notify.',
            'Examples:\n  delta-wifi notify-perms\n  delta-wifi watch --auto-login --notify')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command in ('watch', 'login', 'logout'):
        if not math.isfinite(args.verify_timeout) or not 5 <= args.verify_timeout <= 300:
            parser.error('--verify-timeout must be between 5 and 300 seconds')
    if args.command in ('watch', 'login'):
        if not 1 <= args.max_attempts <= 10:
            parser.error('--max-attempts must be between 1 and 10')
        if not math.isfinite(args.retry_delay) or args.retry_delay < 5 or args.retry_delay > 300:
            parser.error('--retry-delay must be between 5 and 300 seconds')
    if args.command == 'watch':
        if not math.isfinite(args.notify_after) or not 10 <= args.notify_after <= 3600:
            parser.error('--notify-after must be between 10 and 3600 seconds')
    try:
        if args.command == 'logout':
            client = portal_client(args.assume_delta, verify_timeout=args.verify_timeout, mode='http')
            try:
                result = client.logout(dry_run=args.dry_run, before_reset=lambda: storage.set_recovery_paused(True))
            finally:
                client.close()
            if not args.dry_run:
                storage.record('logout-result', state=result.state)
            print(json.dumps(dict(state=result.state, message=result.message)) if args.json else 'Logout: ' + result.message)
            if result.state not in ('ready', 'logged-out'):
                parser.exit(1)
        elif args.command == 'connectivity':
            if not 1 <= args.samples <= 10:
                parser.error('--samples must be between 1 and 10')
            if not math.isfinite(args.interval) or not 2 <= args.interval <= 60:
                parser.error('--interval must be between 2 and 60 seconds')
            reports = []
            for index in range(args.samples):
                report = connectivity_report()
                reports.append(report)
                if not args.json:
                    print(f'{report["timestamp"]}  Network: {report["network_before"]["ssid"] or "unknown"} -> {report["network_after"]["ssid"] or "unknown"}')
                    print(f'  Internet: {report["internet"]} | Route: {report["route_status"]} | VPN: {report["vpn"]} | Proxy: {report["proxy"]}')
                    for probe in report['probes']:
                        print(f'  {probe["name"]:<17} {probe["result"]:<20} HTTP {probe["status_code"] or "-"}  {probe["elapsed_ms"]}ms')
                    print('  Online requires two general-web checks and no Delta portal redirect. ICMP is not evidence of login.')
                if index + 1 < args.samples:
                    time.sleep(args.interval)
            if args.json:
                print(json.dumps(dict(samples=reports), indent=2))
        elif args.command == 'history':
            if not 1 <= args.limit <= storage.MAX_EVENTS:
                parser.error('--limit must be between 1 and 5000')
            if args.clear:
                storage.clear_history()
                print('[]' if args.json else 'Connection history cleared.')
            else:
                rows = storage.history(args.limit)
                if args.json:
                    print(json.dumps(rows, indent=2))
                elif not rows:
                    print('No history yet. Run watch to record connection changes.')
                else:
                    for row in rows:
                        fields = ', '.join(f'{key}={value}' for key, value in row.items() if key not in ('timestamp', 'event'))
                        print(f'{row["timestamp"]}  {row["event"]:<19} {fields}')
        elif args.command == 'account':
            account = args.account or storage.preferred_account()
            if not account:
                print('No preferred account saved. Run delta-wifi credentials ACCOUNT.')
            elif args.action == 'use':
                result = run(['security', 'find-generic-password', '-s', SERVICE, '-a', account])
                if result.returncode:
                    raise RuntimeError('No accessible Keychain entry for this account. Run credentials ACCOUNT first.')
                storage.set_account(account)
                print('Preferred account selected; existing Keychain password kept.')
            elif args.action == 'forget':
                result = run(['security', 'delete-generic-password', '-s', SERVICE, '-a', account])
                if result.returncode not in (0, 44):
                    raise RuntimeError('Keychain entry could not be removed; saved preference kept.')
                if account == storage.preferred_account():
                    storage.set_account(None)
                print('Saved Keychain entry and matching account preference removed.')
            else:
                result = run(['security', 'find-generic-password', '-s', SERVICE, '-a', account])
                print('Account: ' + account)
                print('Keychain entry: ' + ('present' if result.returncode == 0 else 'not accessible or absent'))
        elif args.command == 'notify-perms':
            if not notifications.BINARY.exists():
                print('Building the notification helper...', flush=True)
                notifications.build()
            result = notifications.invoke('authorize')
            print('Notifications: ' + result.get('state', 'unknown'))
            if result.get('state') != 'authorized':
                print('Check System Settings > Notifications > Delta WiFi Alerts.')
        elif args.command == 'portal-info':
            ssid, ip, _ = association()
            if not network_allowed(ssid, ip, args.assume_delta):
                raise RuntimeError('Join DeltaWiFi.com before inspecting the portal')
            client = portal_client(args.assume_delta, args.headless, args.browser, mode=args.mode)
            try:
                print(json.dumps(client.info(), indent=2))
            finally:
                client.close()
        elif args.command == 'wifi-perms':
            if not (native_helper.APP / 'Contents' / 'MacOS' / 'DeltaWiFiHelper').exists():
                print('Building the native Wi-Fi helper...', flush=True)
                native_helper.build()
            result = native_helper.read(request_permission=True)
            print(json.dumps(result, indent=2))
            if result and not result.get('ssid'):
                print('Check System Settings > Privacy & Security > Location Services > Delta WiFi Helper.')
        elif args.command == 'credentials':
            account = args.account or storage.preferred_account() or input('SkyMiles number or username: ').strip()
            if not account or any(ord(c) < 32 for c in account):
                raise RuntimeError('Enter a valid SkyMiles number or username.')
            credentials(account, getpass.getpass('SkyMiles password: '))
            storage.set_account(account)
            print('Credential saved in macOS Keychain; preferred account remembered.')
        elif args.command == 'open-portal':
            ssid, ip, _ = association()
            if not network_allowed(ssid, ip, args.assume_delta):
                raise RuntimeError('Join DeltaWiFi.com; if SSID is hidden, confirm with --assume-delta')
            result = run(['open', PORTAL])
            if result.returncode:
                raise RuntimeError('Could not open the portal')
        elif args.command == 'login':
            args.account = args.account or storage.preferred_account()
            if not args.account:
                raise RuntimeError('No preferred account. Run delta-wifi credentials ACCOUNT first.')
            ssid, ip, _ = association()
            if not network_allowed(ssid, ip, args.assume_delta):
                raise RuntimeError('Join DeltaWiFi.com; if SSID is hidden, confirm with --assume-delta')
            storage.set_recovery_paused(False)
            login(args.account, args.assume_delta, args.max_attempts, args.retry_delay, args.headless, args.browser, args.force, args.verify_timeout, args.mode)
            state = inspect(args.assume_delta)
            if state.internet == 'online':
                storage.set_last_access(state.timestamp)
                state.last_online = state.timestamp
            display(state)
        elif args.command in ('status', 'diagnose'):
            state = inspect(args.assume_delta)
            if state.internet == 'online':
                storage.set_last_access(state.timestamp)
                state.last_online = state.timestamp
            else:
                state.last_online = storage.last_access()
            # Diagnostics deliberately exclude account, cookies, portal HTML, and public IP.
            display(state, args.json or args.command == 'diagnose')
        else:
            if not math.isfinite(args.interval) or args.interval < 5 or args.interval > 300:
                parser.error('--interval must be between 5 and 300 seconds')
            if args.auto_login is True:
                args.auto_login = storage.preferred_account()
                if not args.auto_login:
                    raise RuntimeError('No preferred account. Run delta-wifi credentials ACCOUNT first.')
            if args.auto_login:
                storage.set_recovery_paused(False)
            watch(args)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        parser.exit(1, f'delta-wifi: {exc}\n')


if __name__ == '__main__':
    main()
