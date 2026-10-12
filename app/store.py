import json
import sqlite3
import time
import uuid


class Store:
    def __init__(self, path, recover=True):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, update_id INTEGER UNIQUE, owner INTEGER, chat INTEGER,
          url TEXT, requested_name TEXT, state TEXT, bytes INTEGER DEFAULT 0,
          total INTEGER, destination TEXT DEFAULT '', error TEXT DEFAULT '', created REAL);
        CREATE TABLE IF NOT EXISTS updates (
          id INTEGER PRIMARY KEY, payload TEXT, state TEXT DEFAULT 'pending', created REAL);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for name, declaration in {
            "completed": "REAL", "expires": "REAL", "file_id": "TEXT DEFAULT ''",
            "etag": "TEXT DEFAULT ''", "file_url": "TEXT DEFAULT ''",
            "retention": "TEXT DEFAULT ''", "cleanup_error": "TEXT DEFAULT ''",
            "manifest": "INTEGER DEFAULT 0", "links_error": "TEXT DEFAULT ''",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        self.db.commit()
        if recover:
            self.recover()

    def recover(self):
        with self.db:
            self.db.execute("UPDATE jobs SET state='interrupted', url='', error='Host đã khởi động lại. Kiểm tra thư mục đích trước khi gửi lại link.' WHERE state='running'")
            self.db.execute("UPDATE updates SET state='pending' WHERE state='running'")

    def ingest(self, update):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO updates(id,payload,created) VALUES(?,?,?)",
                            (update["update_id"], json.dumps(update), time.time()))

    def next_update(self):
        row = self.db.execute("SELECT * FROM updates WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
        if row:
            with self.db:
                self.db.execute("UPDATE updates SET state='running' WHERE id=?", (row["id"],))
            return json.loads(row["payload"])

    def finish_update(self, update_id):
        with self.db:
            # Retain ID for retry deduplication, discard signed URLs and message content.
            self.db.execute("UPDATE updates SET state='done',payload='' WHERE id=?", (update_id,))

    def add(self, update_id, owner, chat, url, requested_name=""):
        row = self.db.execute("SELECT * FROM jobs WHERE update_id=?", (update_id,)).fetchone()
        if row:
            return row
        if self.db.execute("SELECT COUNT(*) FROM jobs WHERE owner=? AND state IN ('queued','running')", (owner,)).fetchone()[0] >= 3:
            raise ValueError("Mỗi người tối đa 3 tác vụ đang chờ / chạy.")
        if self.db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 20:
            raise ValueError("Hàng đợi đã đầy, thử lại sau.")
        job_id = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO jobs(id,update_id,owner,chat,url,requested_name,state,created) VALUES(?,?,?,?,?,?,'queued',?)",
                            (job_id, update_id, owner, chat, url, requested_name, time.time()))
        return self.get(job_id, owner)

    def claim(self):
        row = self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
        if row:
            self.change(row["id"], state="running")
            return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def change(self, job_id, **fields):
        if not set(fields).issubset({"state", "bytes", "total", "destination", "error", "url",
                "completed", "expires", "file_id", "etag", "file_url", "retention", "cleanup_error", "manifest", "links_error"}):
            raise ValueError("Invalid job fields")
        with self.db:
            self.db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?", (*fields.values(), job_id))

    def get(self, job_id, owner):
        row = self.db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        return dict(row) if row else None

    def recent(self, owner):
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 5", (owner,))]

    def find(self, owner, query, limit=10):
        # Match literally; '%' and '_' are filename characters, not SQL wildcards.
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE owner=? AND state='done' AND (expires IS NULL OR expires>?) AND instr(lower(destination),lower(?))>0 ORDER BY completed DESC LIMIT ?",
            (owner, time.time(), query, limit))]

    def managed(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE state='done' AND expires IS NOT NULL AND retention IN ('pending','armed') ORDER BY expires")]

    def import_manifest(self, record):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(id,owner,chat,url,requested_name,state,bytes,total,destination,created,completed,expires,file_id,etag,retention,manifest) VALUES(?,?,?,'','','done',?,?,?,?,?,?,?,?,'armed',1)",
                (record["id"], record["owner"], record["chat"], record["size"], record["size"], record["destination"],
                 record["completed"], record["completed"], record["expires"], record["file_id"], record["etag"]))

    def clean(self):
        with self.db:
            cutoff = time.time() - 7 * 86400
            self.db.execute("DELETE FROM jobs WHERE created<? AND state NOT IN ('queued','running') AND retention NOT IN ('pending','armed','conflict')", (cutoff,))
            self.db.execute("DELETE FROM updates WHERE created<? AND state='done'", (cutoff,))

    def close(self):
        self.db.close()
