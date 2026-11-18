"""Small additive SQLite storage; no visitor ownership or historical data changes."""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash
from services.runtime_config import no_links

COOKIE = 'pc_admin_session'
SESSION_SECONDS = 8 * 3600


def digest(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


class Store:
    def __init__(self, path):
        self.path = no_links(path)

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self):
        # Idempotent and additive. Caller backs up a real instance before upgrading.
        with self.transaction() as c:
            for sql in (
                'CREATE TABLE IF NOT EXISTS pc_admin_identity (id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0)',
                'CREATE TABLE IF NOT EXISTS pc_admin_sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, authenticated INTEGER NOT NULL, expires REAL NOT NULL, created REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS pc_admin_control (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0, draft_revision INTEGER NOT NULL DEFAULT 0, draft TEXT, active TEXT, paused INTEGER NOT NULL DEFAULT 0)',
                'CREATE TABLE IF NOT EXISTS pc_admin_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, summary TEXT NOT NULL)',
            ):
                c.execute(sql)
            c.execute('INSERT OR IGNORE INTO pc_admin_control(id) VALUES (1)')

    @staticmethod
    def audit(c, action, summary, actor='developer'):
        c.execute('INSERT INTO pc_admin_audit(at,actor,action,summary) VALUES(?,?,?,?)',
                  (time.time(), actor, action, json.dumps(summary, ensure_ascii=False)))

    def initialize(self, password, reset=False):
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise ValueError('密码需为12至128个字符。')
        hashed = generate_password_hash(password, method='scrypt')
        with self.transaction() as c:
            exists = c.execute('SELECT id FROM pc_admin_identity').fetchone()
            if bool(exists) != reset:
                raise ValueError('管理员已存在，请使用明确的本地重置命令。' if exists else '管理员尚未初始化。')
            c.execute('INSERT OR REPLACE INTO pc_admin_identity(id,password_hash) VALUES(1,?)', (hashed,))
            c.execute('DELETE FROM pc_admin_sessions')
            self.audit(c, 'password_reset' if reset else 'initialize', {}, 'local-cli')

    def session(self, token):
        with self.transaction() as c:
            return c.execute('SELECT * FROM pc_admin_sessions WHERE token_hash=? AND expires>?',
                             (digest(token or ''), time.time())).fetchone()

    def create_session(self):
        now = time.time()
        with self.transaction() as c:
            c.execute('DELETE FROM pc_admin_sessions WHERE expires<?', (now,))
            if c.execute('SELECT count(*) FROM pc_admin_sessions WHERE created>?', (now-300,)).fetchone()[0] >= 30:
                raise ValueError('登录会话创建过于频繁，请5分钟后再试。')
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            c.execute('INSERT INTO pc_admin_sessions VALUES(?,?,?,?,?)', (digest(token), csrf, 0, now+900, now))
            initialized = c.execute('SELECT id FROM pc_admin_identity').fetchone() is not None
        return token, csrf, initialized

    def login(self, token, password):
        now = time.time()
        with self.transaction() as c:
            row = c.execute('SELECT * FROM pc_admin_identity WHERE id=1').fetchone()
            session = c.execute('SELECT * FROM pc_admin_sessions WHERE token_hash=? AND expires>?', (digest(token), now)).fetchone()
            if not row or not session:
                return None, '请先在本地初始化管理员并刷新登录页。'
            if row['blocked_until'] > now:
                return None, '登录暂时锁定，请5分钟后再试。'
            valid = isinstance(password, str) and len(password) <= 128 and check_password_hash(row['password_hash'], password)
            if not valid:
                failures = row['failures'] + 1 if row['blocked_until'] == 0 else 1
                c.execute('UPDATE pc_admin_identity SET failures=?,blocked_until=? WHERE id=1', (failures, now+300 if failures >= 5 else 0))
                self.audit(c, 'login_failed', {}, 'unauthenticated')
                return None, '密码错误或登录暂时锁定。'
            c.execute('UPDATE pc_admin_identity SET failures=0,blocked_until=0 WHERE id=1')
            c.execute('DELETE FROM pc_admin_sessions WHERE token_hash=?', (digest(token),))
            new, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            c.execute('INSERT INTO pc_admin_sessions VALUES(?,?,?,?,?)', (digest(new), csrf, 1, now+SESSION_SECONDS, now))
            self.audit(c, 'login', {})
        return (new, csrf), None

    def logout(self, token):
        with self.transaction() as c:
            c.execute('DELETE FROM pc_admin_sessions WHERE token_hash=?', (digest(token),))
            self.audit(c, 'logout', {})

    def control(self):
        with self.transaction() as c:
            row = dict(c.execute('SELECT * FROM pc_admin_control WHERE id=1').fetchone())
        for key in ('draft', 'active'):
            row[key] = json.loads(row[key]) if row[key] else None
        return row


def persisted_control(data_dir):
    """Read-only startup/CLI overlay. No migration, credentials or initialization."""
    path = no_links(Path(data_dir) / 'data/database.db')
    if not path.exists():
        return None
    c = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='pc_admin_control'").fetchone():
            return None
        row = dict(c.execute('SELECT * FROM pc_admin_control WHERE id=1').fetchone())
        row['active'] = json.loads(row['active']) if row['active'] else None
        return row
    finally:
        c.close()
