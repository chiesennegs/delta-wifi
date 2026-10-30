"""Private local preferences and bounded history. Passwords stay in Keychain."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
from .connectivity import POLICY

# Only fixed labels, durations, and counters may reach persistent event history.
EVENT_KEYS = frozenset(('wifi', 'session', 'internet', 'dns', 'gateway_status', 'route_status',
                        'vpn', 'proxy', 'state', 'attempt', 'paused', 'duration_seconds', 'reason', 'submitted'))
MAX_EVENTS = 5000


def directory():
    return Path(os.environ.get('DELTA_WIFI_STATE_DIR',
                str(Path.home() / 'Library' / 'Application Support' / 'delta-wifi')))


@contextlib.contextmanager
def database():
    root = directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = root / 'state.sqlite3'
    # Create with private permissions before SQLite opens it.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)')
        yield connection
        connection.commit()
    finally:
        connection.close()


def preferred_account():
    with database() as db:
        result = db.execute("SELECT value FROM preferences WHERE key='account'").fetchone()
        return result[0] if result else None


def set_account(account):
    with database() as db:
        if account is None:
            db.execute("DELETE FROM preferences WHERE key='account'")
        else:
            db.execute("INSERT OR REPLACE INTO preferences VALUES ('account', ?)", (account,))


def record(kind, **fields):
    if kind not in ('connection', 'outage-start', 'outage-end', 'outage-interrupted', 'login-attempt', 'recovery-attempt',
                    'login-result', 'logout-result', 'watch-start', 'watch-stop', 'history-cleared'):
        raise ValueError('Unknown event kind')
    data = {key: value for key, value in fields.items() if key in EVENT_KEYS}
    with database() as db:
        db.execute('INSERT INTO events(timestamp, kind, data) VALUES (?, ?, ?)',
                   (dt.datetime.now().astimezone().isoformat(timespec='seconds'), kind, json.dumps(data)))
        db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)', (MAX_EVENTS,))


def history(limit=50):
    with database() as db:
        rows = db.execute('SELECT timestamp, kind, data FROM events ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(timestamp=stamp, event=kind, **json.loads(data)) for stamp, kind, data in reversed(rows)]


def clear_history():
    with database() as db:
        db.execute('DELETE FROM events')


def last_access():
    with database() as db:
        rows = dict(db.execute("SELECT key, value FROM preferences WHERE key IN ('last_access', 'last_access_policy')").fetchall())
        return rows.get('last_access') if rows.get('last_access_policy') == POLICY else None


def set_last_access(timestamp):
    with database() as db:
        db.execute("INSERT OR REPLACE INTO preferences VALUES ('last_access', ?)", (timestamp,))
        db.execute("INSERT OR REPLACE INTO preferences VALUES ('last_access_policy', ?)", (POLICY,))


def recovery_paused():
    with database() as db:
        result = db.execute("SELECT value FROM preferences WHERE key='recovery_paused'").fetchone()
        return bool(result and result[0] == '1')


def set_recovery_paused(paused):
    with database() as db:
        db.execute("INSERT OR REPLACE INTO preferences VALUES ('recovery_paused', ?)", ('1' if paused else '0',))
