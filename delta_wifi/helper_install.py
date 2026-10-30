"""Stable per-user native helper installation for packaged distributions.

Editable/source installs retain their existing helper location and permissions.
Homebrew's wrapper supplies DELTA_WIFI_BUNDLED_HELPERS for prebuilt helpers.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from .storage import directory

NAMES = ('Delta WiFi Helper.app', 'Delta WiFi Alerts.app')


def managed():
    return bool(os.environ.get('DELTA_WIFI_BUNDLED_HELPERS'))


def app_path(name):
    if name not in NAMES:
        raise ValueError('Unknown native helper')
    root = directory() / 'helpers' if managed() else Path(__file__).parent / 'native'
    return root / name


def digest(app):
    value = hashlib.sha256()
    for path in sorted(app.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('Unexpected symbolic link in a bundled helper')
        if path.is_file():
            value.update(path.relative_to(app).as_posix().encode() + b'\0')
            with path.open('rb') as content:
                for block in iter(lambda: content.read(65536), b''):
                    value.update(block)
    return value.hexdigest()


def ensure(name):
    target = app_path(name)
    if not managed():
        return target
    source = Path(os.environ['DELTA_WIFI_BUNDLED_HELPERS']) / name
    if not (source / 'Contents' / 'Info.plist').is_file() or not (source / 'Contents' / 'MacOS').is_dir():
        raise RuntimeError('Prebuilt helper is missing; reinstall delta-wifi with the package manager.')
    expected = digest(source)
    root = target.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = root / (name + '.sha256')
    with (root / '.install.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if target.exists() and marker.exists() and marker.read_text().strip() == expected:
            return target
        # Keep the app's public path constant and roll back if replacement fails.
        with tempfile.TemporaryDirectory(prefix='.install-', dir=root) as stage:
            incoming = Path(stage) / name
            backup = Path(stage) / 'previous.app'
            shutil.copytree(source, incoming)
            if digest(incoming) != expected:
                raise RuntimeError('Native helper copy failed its integrity check')
            had_previous = target.exists()
            if had_previous:
                target.rename(backup)
            try:
                incoming.rename(target)
            except Exception:
                if had_previous:
                    backup.rename(target)
                raise
            temporary_marker = Path(stage) / 'digest'
            temporary_marker.write_text(expected + '\n')
            temporary_marker.replace(marker)
    return target
