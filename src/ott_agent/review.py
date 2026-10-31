"""Read-only projections for recorded runs and API workspaces."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

from .audit import decision_summaries
from .domain import resolve_facts
from .util import available, jsonl, ts


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def page_view(pack, actions, *, now, dialect, withdrawn=None, invalidated=False):
    """Serve no stale/dialect-mismatched/withdrawn page, including old exports."""
    pages = [a for a in actions if a["type"] == "home_page" and ts(a["published_at"]) <= now]
    page = max(pages, key=lambda a: a["published_at"], default=None)
    reason = "no_approved_page"
    if page:
        ids = [page.get("hero_title_id")] + [t for r in page.get("rows", []) for t in r.get("title_ids", [])]
        if invalidated:
            reason = "viewer_state_changed"
        elif now >= ts(page["published_at"]) + timedelta(minutes=120):
            reason = "page_expired"
        elif not dialect or dialect != page.get("dialect"):
            reason = "dialect_changed_or_unknown"
        elif not all(available(pack.catalogue.get(t), now, withdrawn) for t in ids if t):
            reason = "content_unavailable_or_withdrawn"
        else:
            return {"status": "APPROVED", "as_of": now.isoformat(), "page": page, "reason": None}
    return {"status": "DEFAULT", "as_of": now.isoformat(), "page": None, "reason": reason}


class RunReader:
    def __init__(self, path: Path, pack):
        self.path, self.pack = path, pack
        self._lock = threading.Lock()
        self._stamp, self._summaries, self._offsets = None, [], {}
        self._db_stamp, self._db_summaries = None, []

    def _db(self):
        path = self.path / "state.sqlite3"
        if path.is_file():
            return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        return None

    def _index(self):
        summary = self.path / "decision_summaries.jsonl"
        decisions = self.path / "decisions.jsonl"
        stamp = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in (summary, decisions))
        with self._lock:
            if stamp == self._stamp:
                return
            self._summaries = list(jsonl(summary)) if summary.exists() else []
            self._offsets = {}
            if decisions.exists():
                with decisions.open("rb") as stream:
                    while True:
                        offset = stream.tell()
                        line = stream.readline()
                        if not line:
                            break
                        d = json.loads(line)
                        self._offsets[d["decision_id"]] = offset
                        if not summary.exists():
                            self._summaries.extend(decision_summaries([d], self.actions(d["user_id"]), self.pack))
            self._stamp = stamp

    def summaries(self):
        db = self._db()
        if db:
            try:
                stamp = (tuple(db.execute("SELECT COUNT(*),MAX(rowid) FROM decisions").fetchone()),
                         tuple(db.execute("SELECT COUNT(*),MAX(rowid) FROM actions").fetchone()))
                with self._lock:
                    if stamp != self._db_stamp:
                        fields = ("decision_id", "viewer_id", "user_id", "trigger", "goal", "dialect", "confidence", "evidence", "assumptions", "uncertainties", "checks", "decided_in_ms", "status", "action_ids", "repair", "fallback_reason", "no_action_reason")
                        pairs = [f"'{field}',json_extract(payload,'$.{field}')" for field in fields]
                        pairs.append("'provider_result',json_object('provider',json_extract(payload,'$.provider_result.provider'),'model',json_extract(payload,'$.provider_result.model'))")
                        sql = "SELECT json_object(" + ",".join(pairs) + ") FROM decisions ORDER BY published_at DESC"
                        decisions = [json.loads(r[0]) for r in db.execute(sql)]
                        actions = [json.loads(r[0]) for r in db.execute("SELECT payload FROM actions")]
                        self._db_summaries = list(decision_summaries(decisions, actions, self.pack))
                        self._db_stamp = stamp
                    return list(self._db_summaries)
            finally:
                db.close()
        self._index()
        return list(reversed(self._summaries))

    def decision(self, did):
        db = self._db()
        if db:
            try:
                row = db.execute("SELECT payload FROM decisions WHERE decision_id=?", (did,)).fetchone()
                return json.loads(row[0]) if row else None
            finally:
                db.close()
        self._index()
        offset = self._offsets.get(did)
        if offset is None:
            return None
        with (self.path / "decisions.jsonl").open("rb") as stream:
            stream.seek(offset)
            return json.loads(stream.readline())

    def actions(self, uid):
        db = self._db()
        if db:
            try:
                return [json.loads(r[0]) for r in db.execute("SELECT payload FROM actions WHERE user_id=? ORDER BY rowid", (uid,))]
            finally:
                db.close()
        path = self.path / "actions.jsonl"
        return [a for a in jsonl(path) if a["user_id"] == uid] if path.exists() else []

    def viewer(self, uid):
        db = self._db()
        if db:
            try:
                row = db.execute("SELECT payload FROM viewers WHERE user_id=?", (uid,)).fetchone()
                if not row:
                    return None
                state = json.loads(row[0])
                state["goal_history"] = [json.loads(r[0]) for r in db.execute("SELECT payload FROM goals WHERE user_id=?", (uid,))]
                return state
            finally:
                db.close()
        return read_json(self.path / "viewer_state" / f"{uid}.json")

    def controls(self):
        db = self._db()
        if db:
            try:
                return [json.loads(r[0]) for r in db.execute("SELECT payload FROM controls")]
            finally:
                db.close()
        path = self.path / "control_log.jsonl"
        return list(jsonl(path)) if path.exists() else []

    def home(self, uid, now, extra_withdrawn=None):
        actions = self.actions(uid)
        state = self.viewer(uid) or {}
        withdrawn = {c["title_id"]: c["at"] for c in self.controls() if c["type"] == "title_withdrawn" and ts(c["at"]) <= now}
        withdrawn.update(extra_withdrawn or {})
        db = self._db()
        if db:
            try:
                events = [e for r in db.execute("SELECT payload FROM events WHERE user_id=? ORDER BY received_at,event_id", (uid,))
                          if ts((e := json.loads(r[0]))["received_at"]) <= now]
            finally:
                db.close()
        else:
            decisions = [self.decision(s["decision_id"]) for s in self.summaries() if s["viewer_id"] == uid and ts(self.decision(s["decision_id"])["published_at"]) <= now]
            latest = max(decisions, key=lambda d: d["published_at"], default=None)
            events = []
            if latest:
                facts = (latest.get("validation_context") or latest.get("context") or {}).get("facts", {})
                return page_view(self.pack, actions, now=now, dialect=facts.get("dialect"), withdrawn=withdrawn)
        latest = max(events, key=lambda e: e["received_at"], default=None)
        facts = resolve_facts(self.pack, events, latest, now) if latest else {}
        pages = [a for a in actions if a["type"] == "home_page" and ts(a["published_at"]) <= now]
        latest_page = max(pages, key=lambda a: a["published_at"], default=None)
        changes = {"purchase_completed", "subscription_renewed", "subscription_lapsed", "notification_opt_out", "play_started", "episode_completed", "app_dialect_changed"}
        # Millisecond timestamps can tie across commit/acceptance. Refuse the
        # page conservatively rather than lose an intervening change in a tie.
        changed = bool(latest_page and any(e["event"] in changes and ts(e["received_at"]) >= ts(latest_page["published_at"]) for e in events))
        invalidation = state.get("plan_invalidated_by") or {}
        changed = changed or bool(invalidation.get("at") and ts(invalidation["at"]) <= now and latest_page and ts(invalidation["at"]) >= ts(latest_page["published_at"]))
        return page_view(self.pack, actions, now=now, dialect=facts.get("dialect"), withdrawn=withdrawn, invalidated=changed)

    def detail(self, did):
        d = self.decision(did)
        if not d:
            return None
        actions = [a for a in self.actions(d["user_id"]) if a["decision_id"] == did]
        summary = next(decision_summaries([d], actions, self.pack))
        ids = [t[6:] for t in summary["content"]]
        titles = {tid: {k: self.pack.catalogue[tid].get(k) for k in ("title_id", "title", "genres", "dialect", "content_type", "is_premium")} for tid in ids if tid in self.pack.catalogue}
        return {"summary": summary, "audit": d, "actions": actions, "titles": titles,
                "viewer": self.viewer(d["user_id"]), "home": self.home(d["user_id"], ts(d["published_at"])),
                "preview_note": "Historical snapshot at publication; not a claim that this page is currently servable."}
