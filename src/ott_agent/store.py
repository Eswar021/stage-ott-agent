from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
          received_at TEXT NOT NULL,event TEXT NOT NULL,source TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_by_user ON events(user_id,received_at);
        CREATE TABLE IF NOT EXISTS deliveries(delivery_key TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS viewers(user_id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS goals(goal_instance_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
          goal_id TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_goal ON goals(user_id) WHERE status='open';
        CREATE INDEX IF NOT EXISTS goals_by_rule ON goals(goal_id,status);
        CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
          trigger_event_id TEXT NOT NULL,published_at TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS actions(action_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL,
          user_id TEXT NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS actions_by_user ON actions(user_id,type);
        CREATE TABLE IF NOT EXISTS reservations(user_id TEXT NOT NULL,day TEXT NOT NULL,
          channel_group TEXT NOT NULL,action_id TEXT NOT NULL,PRIMARY KEY(user_id,day,channel_group));
        CREATE TABLE IF NOT EXISTS pending(decision_id TEXT PRIMARY KEY,complete_at TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS controls(control_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS baselines(user_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        """)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.db:
            yield

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT payload FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, dump(value)))

    def ingest_many(self, events, source):
        self.db.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", [
            (e["event_id"], e["user_id"], e["received_at"], e["event"], source, dump(e)) for e in events])

    def ingest(self, event, source):
        cursor = self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", (
            event["event_id"], event["user_id"], event["received_at"], event["event"], source, dump(event)))
        return bool(cursor.rowcount)

    def all_events(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM events ORDER BY received_at,event_id")]

    def delivery_seen(self, key):
        return self.db.execute("SELECT 1 FROM deliveries WHERE delivery_key=?", (key,)).fetchone() is not None

    def mark_delivery(self, key, data):
        self.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?)", (key, dump(data)))

    def revision(self, uid):
        row = self.db.execute("SELECT revision FROM viewers WHERE user_id=?", (uid,)).fetchone()
        return row[0] if row else 0

    def put_viewer(self, uid, state, bump=False):
        revision = self.revision(uid) + int(bump)
        self.db.execute("INSERT OR REPLACE INTO viewers VALUES (?,?,?)", (uid, revision, dump(state)))
        return revision

    def viewer(self, uid):
        row = self.db.execute("SELECT payload FROM viewers WHERE user_id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def put_goal(self, goal):
        self.db.execute("INSERT OR REPLACE INTO goals VALUES (?,?,?,?,?)", (
            goal["goal_instance_id"], goal["user_id"], goal.get("goal_id", goal["id"]), goal["status"], dump(goal)))

    def open_goal(self, uid):
        row = self.db.execute("SELECT payload FROM goals WHERE user_id=? AND status='open'", (uid,)).fetchone()
        return json.loads(row[0]) if row else None

    def open_goals_for_rule(self, gid):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM goals WHERE goal_id=? AND status='open'", (gid,))]

    def goals(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM goals ORDER BY rowid")]

    def actions(self, uid=None):
        sql = "SELECT payload FROM actions" + (" WHERE user_id=?" if uid else "") + " ORDER BY rowid"
        return [json.loads(r[0]) for r in self.db.execute(sql, (uid,) if uid else ())]

    def decisions(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM decisions ORDER BY published_at,rowid")]

    def decision(self, did):
        row = self.db.execute("SELECT payload FROM decisions WHERE decision_id=?", (did,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_decision(self, decision):
        self.db.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?)", (
            decision["decision_id"], decision["user_id"], decision["trigger_event_id"], decision["published_at"], dump(decision)))

    def put_action(self, action):
        self.db.execute("INSERT OR IGNORE INTO actions VALUES (?,?,?,?,?)", (
            action["action_id"], action["decision_id"], action["user_id"], action["type"], dump(action)))

    def reserve_message(self, uid, day, group, action_id):
        self.db.execute("INSERT INTO reservations VALUES (?,?,?,?)", (uid, str(day), group, action_id))

    def observe_message(self, uid, day, group, event_id):
        self.db.execute("INSERT OR IGNORE INTO reservations VALUES (?,?,?,?)", (uid, str(day), group, event_id))

    def message_reserved(self, uid, day, group="outbound"):
        return self.db.execute("SELECT 1 FROM reservations WHERE user_id=? AND day=? AND channel_group=?", (uid, str(day), group)).fetchone() is not None

    def queue(self, job):
        self.db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?)", (job["decision_id"], job["complete_at"], dump(job)))

    def pending(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM pending ORDER BY complete_at")]

    def remove_pending(self, did):
        self.db.execute("DELETE FROM pending WHERE decision_id=?", (did,))

    def put_control(self, cid, record):
        self.db.execute("INSERT OR REPLACE INTO controls VALUES (?,?)", (cid, dump(record)))

    def controls(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM controls ORDER BY rowid")]

    def put_baseline(self, uid, data):
        self.db.execute("INSERT OR IGNORE INTO baselines VALUES (?,?)", (uid, dump(data)))

    def baselines(self):
        return {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT user_id,payload FROM baselines")}

    def has_baseline(self, uid):
        return self.db.execute("SELECT 1 FROM baselines WHERE user_id=?", (uid,)).fetchone() is not None

    def count(self, table):
        if table not in {"events", "goals", "actions", "decisions", "baselines", "pending", "viewers"}:
            raise ValueError("Unsupported ledger table")
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def close(self):
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()
