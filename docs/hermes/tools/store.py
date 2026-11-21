"""Version-compatible session-store access.

Reads go through plain SQLite (read-only) so they work on every Hermes version.
Writes use the sanctioned API: SessionDB.update_session_model (<=0.22) or
persistence.sessions.update_model (>=0.23).
"""
import sqlite3, warnings
from pathlib import Path

LIVE = 'qwen3.8-flash-next-iq4_xs'
URL = 'http://127.0.0.1:8081/v1'


class Store:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self._p = None
        self._db = None
        try:
            from persistence_assembly import open_persistence, profile_scope
            self._p = open_persistence(profile_scope(home=self.path.parent))
            self._api = 'persistence'
            return
        except Exception:
            self._p = None
        warnings.filterwarnings('ignore')
        from hermes_state import SessionDB
        self._db = SessionDB(self.path)
        self._api = 'sessiondb'

    def read(self, sql, args=()):
        conn = sqlite3.connect('file:%s?mode=ro' % self.path, uri=True)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def write_model(self, sid, model, provider=None, base_url=None, api_mode=None):
        if self._api == 'persistence':
            self._p.sessions.update_model(sid, model, provider,
                                          base_url=base_url, api_mode=api_mode)
            return
        self._db.update_session_model(sid, model, provider=provider,
                                      base_url=base_url, api_mode=api_mode)

    def close(self):
        try:
            if self._p is not None: self._p.close()
            if self._db is not None: self._db.close()
        except Exception:
            pass
