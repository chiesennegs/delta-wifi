"""Optional macOS alerts from a separate app; Wi-Fi authorization is unaffected."""
import json
from pathlib import Path
import platform
import plistlib
import subprocess
import tempfile
from . import helper_install

APP = helper_install.app_path('Delta WiFi Alerts.app')
BINARY = APP / 'Contents' / 'MacOS' / 'DeltaWiFiAlerts'


def build():
    if helper_install.managed():
        helper_install.ensure('Delta WiFi Alerts.app')
        return
    if platform.system() != 'Darwin':
        raise RuntimeError('Notifications require macOS')
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    info = dict(CFBundleIdentifier='local.delta-wifi.alerts', CFBundleName='Delta WiFi Alerts',
                CFBundleDisplayName='Delta WiFi Alerts', CFBundleExecutable='DeltaWiFiAlerts',
                CFBundlePackageType='APPL', CFBundleVersion='1', LSMinimumSystemVersion='11.0', LSUIElement=True)
    (APP / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps(info))
    with tempfile.TemporaryDirectory(prefix='delta-wifi-alert-build-') as cache:
        subprocess.run(['xcrun', 'swiftc', str(Path(__file__).parent / 'native' / 'Alerts.swift'),
                        '-module-cache-path', cache, '-framework', 'AppKit', '-framework', 'UserNotifications',
                        '-o', str(BINARY)], check=True, timeout=180)
    subprocess.run(['codesign', '--force', '--sign', '-', str(APP)], check=True, timeout=20)


def invoke(kind):
    if helper_install.managed():
        helper_install.ensure('Delta WiFi Alerts.app')
    if kind not in ('authorize', 'attention', 'restored'):
        raise ValueError('Unknown notification')
    if not BINARY.exists():
        return {'state': 'setup-required'}
    with tempfile.TemporaryDirectory(prefix='delta-wifi-alert-') as directory:
        output = Path(directory) / 'result.json'
        subprocess.run(['open', '-W', '-n', str(APP), '--args', '--output', str(output), '--' + kind],
                       check=True, capture_output=True, timeout=55 if kind == 'authorize' else 8)
        return json.loads(output.read_text())


class Notifier:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.problem = None

    def send(self, kind):
        if not self.enabled or self.problem:
            return
        try:
            result = invoke(kind)
            if result.get('state') != 'queued':
                self.problem = 'Notifications unavailable; run delta-wifi notify-perms and check macOS notification settings.'
        except (OSError, subprocess.SubprocessError, ValueError):
            self.problem = 'Notification delivery failed; connection monitoring continues.'
