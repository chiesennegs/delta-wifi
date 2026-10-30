#!/usr/bin/env python3
"""Build a reviewed, allowlisted release archive; never include the working venv or user state."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
HELPERS = (
    ('Delta WiFi Helper', 'DeltaWiFiHelper', 'WiFiHelper.swift', 'local.delta-wifi.network-helper', ['AppKit','CoreWLAN','CoreLocation']),
    ('Delta WiFi Alerts', 'DeltaWiFiAlerts', 'Alerts.swift', 'local.delta-wifi.alerts', ['AppKit','UserNotifications']),
)


def run(args):
    subprocess.run([str(arg) for arg in args], check=True)


def build_helper(destination, item, version, architectures, identity, cache):
    name, executable, source, bundle_id, frameworks = item
    app = destination / (name + '.app')
    binary = app / 'Contents' / 'MacOS' / executable
    binary.parent.mkdir(parents=True)
    info = dict(CFBundleIdentifier=bundle_id, CFBundleName=name, CFBundleDisplayName=name,
                CFBundleExecutable=executable, CFBundlePackageType='APPL', CFBundleVersion=version,
                CFBundleShortVersionString=version, LSMinimumSystemVersion='11.0', LSUIElement=True)
    if 'CoreLocation' in frameworks:
        description = 'Read the Wi-Fi network name to recognize Delta Wi-Fi. No geographic coordinates are requested.'
        info.update(NSLocationUsageDescription=description, NSLocationWhenInUseUsageDescription=description)
    (app / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps(info))
    slices = []
    for architecture in architectures:
        target = cache / (executable + '-' + architecture)
        args = ['xcrun','swiftc', ROOT/'delta_wifi'/'native'/source, '-O',
                '-module-cache-path', cache/'modules', '-target', architecture+'-apple-macos11.0', '-o', target]
        for framework in frameworks:
            args.extend(['-framework', framework])
        run(args)
        slices.append(target)
    if len(slices) == 1:
        shutil.copy2(slices[0], binary)
    else:
        run(['xcrun','lipo','-create', *slices, '-output', binary])
    signing = ['codesign','--force','--sign', identity]
    if identity != '-':
        signing += ['--options','runtime','--timestamp']
    run(signing + [app])
    run(['codesign','--verify','--deep','--strict', app])
    run(['xcrun','lipo','-info', binary])
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'dist')
    parser.add_argument('--arch', choices=('universal','arm64','x86_64'), default='universal')
    parser.add_argument('--signing-identity', default='-', help='Developer ID identity; - means ad-hoc development signing')
    args = parser.parse_args()
    version = tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='delta-wifi-release-') as temporary:
        temp = Path(temporary)
        source = temp/'source'
        source.mkdir()
        for filename in ('pyproject.toml','README.md','LICENSE'):
            shutil.copy2(ROOT/filename, source/filename)
        shutil.copytree(ROOT/'delta_wifi', source/'delta_wifi',
                        ignore=shutil.ignore_patterns('*.app','__pycache__','*.pyc'))
        wheels = temp/'wheels'
        run([sys.executable,'-m','build','--wheel','--no-isolation','--outdir',wheels,source])
        stage = temp/('delta-wifi-'+version)
        stage.mkdir()
        for wheel in wheels.glob('*.whl'):
            shutil.copy2(wheel, stage/wheel.name)
        for filename in ('README.md','LICENSE'):
            shutil.copy2(ROOT/filename, stage/filename)
        architectures = ('arm64','x86_64') if args.arch == 'universal' else (args.arch,)
        helpers = stage/'helpers'
        helpers.mkdir()
        for item in HELPERS:
            build_helper(helpers, item, version, architectures, args.signing_identity, temp)
        manifest = dict(version=version, architectures=architectures,
                        signing='developer-id' if args.signing_identity != '-' else 'ad-hoc',
                        notarized=False)
        (stage/'release-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        archive = args.output/f'delta-wifi-{version}-macos-{args.arch}.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            bundle.add(stage, arcname=stage.name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (args.output/(archive.name+'.sha256')).write_text(checksum+'  '+archive.name+'\n')
    print(f'Release archive: {archive}\nSHA256: {checksum}')
    if args.signing_identity == '-':
        print('Development signing only. Stable Developer ID signing/notarization is not configured.')


if __name__ == '__main__':
    main()
