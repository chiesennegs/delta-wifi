"""Build and launch a CoreWLAN app with its own Location Services identity."""
import json
import platform
import plistlib
import subprocess
import tempfile
from . import helper_install
from pathlib import Path

APP = helper_install.app_path('Delta WiFi Helper.app')


def build():
    if helper_install.managed():
        helper_install.ensure('Delta WiFi Helper.app')
        return
    if platform.system() != 'Darwin':
        raise RuntimeError('The Wi-Fi helper requires macOS')
    contents = APP / 'Contents'
    binary = contents / 'MacOS' / 'DeltaWiFiHelper'
    binary.parent.mkdir(parents=True, exist_ok=True)
    description = 'Read the connected Wi-Fi network name so delta-wifi can recognize Delta Wi-Fi. No geographic coordinates are requested.'
    info = dict(CFBundleIdentifier='local.delta-wifi.network-helper',
                CFBundleName='Delta WiFi Helper', CFBundleDisplayName='Delta WiFi Helper',
                CFBundleExecutable='DeltaWiFiHelper', CFBundlePackageType='APPL',
                CFBundleVersion='1', CFBundleShortVersionString='0.1.0',
                LSMinimumSystemVersion='11.0', LSUIElement=True,
                NSLocationUsageDescription=description,
                NSLocationWhenInUseUsageDescription=description)
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    source = Path(__file__).parent / 'native' / 'WiFiHelper.swift'
    with tempfile.TemporaryDirectory(prefix='delta-wifi-build-') as cache:
        subprocess.run(['xcrun', 'swiftc', str(source), '-o', str(binary),
                        '-module-cache-path', cache,
                        '-framework', 'AppKit', '-framework', 'CoreWLAN',
                        '-framework', 'CoreLocation'], check=True, timeout=120)
    subprocess.run(['codesign', '--force', '--sign', '-', str(APP)], check=True, timeout=20)


def read(request_permission=False):
    if helper_install.managed():
        helper_install.ensure('Delta WiFi Helper.app')
    if not (APP / 'Contents' / 'MacOS' / 'DeltaWiFiHelper').exists():
        return None
    # Per-call private output directory; the helper never writes credentials or coordinates.
    with tempfile.TemporaryDirectory(prefix='delta-wifi-') as directory:
        output = Path(directory) / 'network.json'
        args = ['open', '-W', '-n', str(APP), '--args', '--output', str(output)]
        if request_permission:
            args.append('--request-permission')
        subprocess.run(args, capture_output=True, check=True,
                       timeout=55 if request_permission else 10)
        return json.loads(output.read_text())
