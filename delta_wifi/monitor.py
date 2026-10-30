"""Watch history, actionable status, and a compact terminal panel."""
import shutil
import sys
import textwrap
from . import storage


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours}h {minutes:02d}m {seconds:02d}s' if hours else f'{minutes}m {seconds:02d}s'


class Timeline:
    def __init__(self, record=None, notify=None, threshold=60, last_online=None, save_online=None):
        self.record = record or storage.record
        self.notify = notify or (lambda *args: None)
        self.threshold = threshold
        self.last_online = last_online
        self.save_online = save_online or (lambda timestamp: None)
        self.saved_at = None
        self.outage_since = None
        self.previous = None
        self.action_notified = False

    def observe(self, state, now):
        connected = state.wifi in ('connected', 'user-confirmed')
        identity = tuple(getattr(state, key) for key in ('wifi', 'session', 'internet', 'dns', 'gateway_status', 'route_status', 'vpn', 'proxy'))
        if identity != self.previous:
            self.record('connection', **dict(zip(('wifi', 'session', 'internet', 'dns', 'gateway_status', 'route_status', 'vpn', 'proxy'), identity)))
            self.previous = identity
        if connected and state.internet == 'online':
            self.last_online = state.timestamp
            if self.saved_at is None or now - self.saved_at >= 60:
                self.save_online(self.last_online)
                self.saved_at = now
            if self.outage_since is not None:
                elapsed = now - self.outage_since
                self.record('outage-end', duration_seconds=round(elapsed))
                if elapsed >= self.threshold:
                    self.notify('restored')
            self.outage_since = None
        elif connected and self.outage_since is None:
            self.outage_since = now
            if self.last_online:
                self.save_online(self.last_online)
            self.record('outage-start')
        elif not connected and self.outage_since is not None:
            self.record('outage-interrupted', duration_seconds=round(now - self.outage_since), reason='network-left')
            self.outage_since = None

    def stop(self, now):
        if self.outage_since is not None:
            self.record('outage-interrupted', duration_seconds=round(now - self.outage_since), reason='watch-stopped')
            self.outage_since = None

    def attention(self):
        if not self.action_notified:
            self.notify('attention')
            self.action_notified = True

    def runtime(self, state, policy, now, auto_login, last_result=None):
        waiting = state.wifi not in ('connected', 'user-confirmed')
        if waiting:
            action = 'Waiting for DeltaWiFi.com; checks resume automatically when it appears.'
            if state.wifi == 'connected-unidentified':
                action += ' If already joined, run wifi-perms to enable SSID access.'
        elif state.internet == 'online':
            action = 'Internet access works; preserving session.'
        elif not state.local_ip:
            action = 'Waiting for a local Wi-Fi IP address.'
        elif state.route_status in ('vpn-route', 'other-interface') or state.proxy == 'configured':
            action = 'Route or proxy may mask Delta connectivity; automatic login is suspended.'
        elif state.session == 'premium-access':
            action = 'Delta reports premium access; preserving the session while external connectivity recovers.'
        elif state.dns == 'failed' and state.portal != 'reachable':
            action = 'External DNS and the portal are unavailable; waiting before attempting login.'
        elif not auto_login:
            action = ('Only selected destinations respond; login and general internet access are unconfirmed. ' if state.internet in ('limited', 'degraded') else '') + 'Monitoring only; use --auto-login to enable recovery.'
        elif policy.paused:
            if policy.paused == 'attempt-limit':
                action = 'Login paused: retry limit reached. Monitoring continues; stable access will rearm recovery.'
            else:
                action = 'Login paused: ' + (last_result.message if last_result else policy.paused)
                action += ' Resolve the issue and restart watch.'
        elif policy.next_at > now:
            action = f'Login retry in {max(1, round(policy.next_at - now))}s.'
        else:
            action = 'Checking connectivity; login recovery is ready if needed.'
        return dict(action=action, last_online=self.last_online,
                    outage_seconds=round(now - self.outage_since) if self.outage_since is not None else None,
                    login_attempts=policy.attempts, login_paused=policy.paused,
                    login_state=last_result.state if last_result else 'not-attempted')


class Panel:
    def __init__(self, enabled=False):
        self.enabled = enabled and sys.stdout.isatty()
        self.active = False

    def render(self, state, runtime, next_check=None):
        if not self.enabled:
            return
        if not self.active:
            sys.stdout.write('\033[?1049h\033[?25l')
            self.active = True
        width = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        # Only erase/redraw this private alternate screen. Never print raw network text.
        clean = lambda value: ''.join(c for c in str(value) if c.isprintable())
        lines = ['Delta Wi-Fi  |  Ctrl+C to stop', '',
                 f'Wi-Fi     {state.wifi}  {state.ssid or "SSID unavailable"}',
                 f'Session   {state.session}', f'Internet  {state.internet}  |  DNS {state.dns}',
                 f'Gateway   {state.gateway_status}  |  Route {state.route_status}',
                 f'VPN       {state.vpn}  |  Proxy {state.proxy}', '',
                 f'Last access: {runtime["last_online"] or "not observed yet"}',
                 f'Outage: {duration(runtime["outage_seconds"] or 0)}  |  Failed attempts: {runtime["login_attempts"]}',
                 '', runtime['action']]
        if state.note:
            lines.extend(['', state.note])
        if next_check is not None:
            lines.append(f'Next connection check in {max(0, int(next_check))}s')
        wrapped = [part for line in lines for part in (textwrap.wrap(clean(line), width) or [''])]
        sys.stdout.write('\033[H\033[2J' + '\n'.join(wrapped) + '\n')
        sys.stdout.flush()

    def close(self):
        if self.active:
            sys.stdout.write('\033[?25h\033[?1049l')
            sys.stdout.flush()
            self.active = False
