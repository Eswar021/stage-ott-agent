"""Local durable HTTP jobs, with real worker threads and per-viewer ownership."""
from __future__ import annotations

import copy
import errno
import fcntl
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path

from .budget import BudgetLedger
from .engine import Engine
from .pack import Pack
from .providers import OpenAIProvider, ReplayProvider
from .review import RunReader, read_json
from .store import Store
from .util import UTC, iso, stable_id, ts, write_json


class Conflict(ValueError):
    pass


def acquire_runtime_lock(runtime, *, record_owner=True):
    """The OS lock, not file existence or a recorded PID, decides ownership."""
    runtime = Path(runtime).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    handle = (runtime / "server.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno not in {errno.EACCES, errno.EAGAIN}:
            handle.close()
            raise
        handle.seek(0)
        try:
            owner = json.loads(handle.read(1024)).get("pid")
        except (ValueError, AttributeError):
            owner = None
        handle.close()
        detail = f" (recorded PID {owner})" if type(owner) is int and owner > 0 else ""
        raise RuntimeError("This API runtime already has an owner" + detail + ". "
            "Use the existing dashboard, or stop its server with Ctrl+C before starting another. "
            "Changing --port does not bypass the shared runtime lock.") from None
    if record_owner:
        handle.seek(0)
        handle.truncate()
        json.dump({"pid": os.getpid()}, handle)
        handle.flush()
    return handle


def check_runtime_available(runtime):
    """Fast CLI preflight; the service still acquires its own lifetime lock."""
    handle = acquire_runtime_lock(runtime, record_owner=False)
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


class Service:
    def __init__(self, project, pack, *, runtime=None, provider_factory=None, workers=4):
        self.project, self.pack = Path(project).resolve(), pack
        self.runtime = Path(runtime or self.project / "runtime/api").resolve()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self._process_lock = acquire_runtime_lock(self.runtime)
        self.provider_factory = provider_factory
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ott-worker")
        self._mutex, self._viewer_locks = threading.Lock(), {}
        self._publication_guard = threading.RLock()
        self._futures, self._readers = {}, {}
        self.started_at = iso(datetime.now(UTC))
        self.history = {}
        for e in pack.history:
            self.history.setdefault(e["user_id"], []).append(e)
        with self.db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,
                request TEXT NOT NULL,result TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,progress TEXT);
              CREATE TABLE IF NOT EXISTS incoming(event_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
                request TEXT NOT NULL,event TEXT NOT NULL,job_id TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS incoming_viewer ON incoming(user_id);
              CREATE TABLE IF NOT EXISTS controls(control_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
              UPDATE jobs SET status='QUEUED' WHERE status='RUNNING';
            """)
            queued = [r[0] for r in db.execute("SELECT job_id FROM jobs WHERE status='QUEUED' ORDER BY created_at")]
        for jid in queued:
            self._schedule(jid)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.runtime / "jobs.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        self.pool.shutdown(wait=True)
        fcntl.flock(self._process_lock, fcntl.LOCK_UN)
        self._process_lock.close()

    def _lock(self, uid):
        with self._mutex:
            return self._viewer_locks.setdefault(uid, threading.Lock())

    def job(self, jid):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ("request", "result", "progress"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def jobs(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 40")]
        return [self.job(j) for j in ids]

    def retry(self, jid):
        with self.db() as db:
            changed = db.execute("UPDATE jobs SET status='QUEUED',error=NULL,updated_at=? WHERE job_id=? AND status='FAILED'", (iso(datetime.now(UTC)), jid)).rowcount
        if not changed:
            raise Conflict("Only a failed job can be retried")
        with self._mutex:
            self._futures.pop(jid, None)
        self._schedule(jid)
        return self.job(jid)

    def _update(self, jid, status=None, **fields):
        allowed = {"result", "error", "progress"}
        values = {k: json.dumps(v) if k != "error" else v for k, v in fields.items() if k in allowed}
        values["updated_at"] = iso(datetime.now(UTC))
        if status:
            values["status"] = status
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in values) + " WHERE job_id=?", (*values.values(), jid))

    def submit(self, kind, request, *, key=None):
        """Commit the inbox and job before returning 202; reject changed retries."""
        if request.get("provider") == "openai" and not self.provider_factory and not os.getenv("OPENAI_API_KEY"):
            raise ValueError("OpenAI is not configured. Choose offline replay or configure OPENAI_API_KEY locally.")
        if kind == "demo" and not any(e["event_id"] == request["trigger_event_id"] for e in self.pack.triggers):
            raise ValueError("Unknown supplied trigger event")
        if kind == "event" and request["user_id"] not in self.pack.profiles:
            raise ValueError("Unknown viewer; this assignment service accepts viewers in the supplied pack.")
        key = request["event_id"] if kind == "event" else key or uuid.uuid4().hex
        jid = stable_id("J", kind, key)
        encoded, now = json.dumps(request, sort_keys=True), iso(datetime.now(UTC))
        with (self._publication_guard if kind == "event" else nullcontext()), self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request FROM jobs WHERE job_id=?", (jid,)).fetchone()
            if old:
                if old[0] != encoded:
                    raise Conflict("This idempotency key/event ID already belongs to a different request.")
            else:
                if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('RUNNING','QUEUED')").fetchone()[0] >= 128:
                    raise Conflict("The local job queue is full; retry later.")
                db.execute("INSERT INTO jobs VALUES(?,?, 'QUEUED', ?, NULL,NULL,?,?,NULL)", (jid, kind, encoded, now, now))
                if kind == "event":
                    event = {k: v for k, v in request.items() if k != "provider"}
                    receipt = iso(datetime.now(UTC))
                    event.update(received_at=receipt, deliver_at=receipt)
                    # Missing phone timestamps remain explicitly uncertain.
                    db.execute("INSERT INTO incoming VALUES(?,?,?,?,?)", (event["event_id"], event["user_id"], encoded, json.dumps(event), jid))
        self._schedule(jid)
        return self.job(jid)

    def _schedule(self, jid):
        with self._mutex:
            if jid not in self._futures:
                self._futures[jid] = self.pool.submit(self._work, jid)

    def _provider(self, name, jid):
        if self.provider_factory:
            return self.provider_factory(name), None
        if name == "replay":
            return ReplayProvider(), None
        from .cli import _setting
        ledger = BudgetLedger(self.project / "runtime/inference_budget.sqlite3", run_id=jid,
            run_limit_usd=_setting("OTT_AGENT_LIVE_RUN_BUDGET_USD", .50, float),
            total_limit_usd=_setting("OTT_AGENT_LIVE_TOTAL_BUDGET_USD", 1.00, float))
        if not 0 <= ledger.run_limit_usd <= ledger.total_limit_usd <= 5:
            ledger.close()
            raise ValueError("Invalid local budget limits")
        try:
            provider = OpenAIProvider(api_key=os.environ["OPENAI_API_KEY"], model=_setting("OPENAI_MODEL", "gpt-6-luna"), ledger=ledger,
                reasoning_effort=_setting("OPENAI_REASONING_EFFORT", "low"),
                max_input_tokens=_setting("OTT_AGENT_MAX_INPUT_TOKENS", 16000, int),
                max_output_tokens=_setting("OTT_AGENT_MAX_OUTPUT_TOKENS", 1800, int), max_tool_calls=16)
        except BaseException:
            ledger.close()
            raise
        return provider, ledger

    def _work(self, jid):
        job = self.job(jid)
        if job["status"] != "QUEUED":
            return
        self._update(jid, "RUNNING")
        try:
            if job["kind"] == "replay":
                result = self._replay(jid, job["request"])
            elif job["kind"] == "demo":
                result = self._demo(jid, job["request"])
            else:
                uid = job["request"]["user_id"]
                with self._lock(uid):
                    result = self._event(jid, job["request"])
            self._update(jid, "SUCCEEDED", result=result)
        except Exception as exc:
            # Never expose raw provider exceptions, credentials or prompts in errors.
            logging.getLogger(__name__).error("Job %s failed: %s", jid, type(exc).__name__)
            self._update(jid, "FAILED", error="Job failed (" + type(exc).__name__ + "). Durable state is retained; see server logs/configuration.")

    def _view_pack(self, uid, *, live=False):
        pack = copy.copy(self.pack)
        pack.history = list(self.history.get(uid, []))
        pack.profiles = {uid: copy.deepcopy(self.pack.profiles.get(uid, {}))}
        pack.arms = {uid: self.pack.arms.get(uid, "holdout")}
        pack.triggers, pack.live_events, pack.control_events = [], [], []
        pack.model_faults = {}  # Interactive calls do not inject replay provider faults.
        if live:
            # A September snapshot cannot prove current October billing or
            # unobserved activity. Preserve history but disable negative claims.
            pack.observation_gaps = [(iso(self.pack.replay_start), self.started_at)]
            pack.incomplete_live_coverage = True
            pack.profiles[uid]["subscription_status"] = "unknown"
            pack.profiles[uid]["plan_started_at"] = ""
            pack.profiles[uid]["plan_ends_at"] = ""
        return pack

    def control(self, title_id):
        if title_id not in self.pack.catalogue:
            raise ValueError("Unknown title")
        c = {"type": "title_withdrawn", "title_id": title_id, "at": iso(datetime.now(UTC)), "source": "local_admin"}
        with self._publication_guard, self.db() as db:
            c["at"] = iso(datetime.now(UTC))
            db.execute("INSERT INTO controls VALUES (?,?)", (uuid.uuid4().hex, json.dumps(c)))
        return c

    def live_controls(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM controls ORDER BY rowid")]

    def incoming(self, uid):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT event FROM incoming WHERE user_id=? ORDER BY rowid", (uid,))]

    def _refresh(self, engine, now):
        uid = next(iter(engine.pack.profiles))
        for c in self.pack.control_events + self.live_controls():
            cid = stable_id("C", c)
            if ts(c["at"]) <= now and not engine.store.db.execute("SELECT 1 FROM controls WHERE control_id=?", (cid,)).fetchone():
                engine._control(c, now)
        for e in self.incoming(uid):
            if e["event"] not in {"app_open", "login_success"} and ts(e["received_at"]) <= now:
                engine._ingest(e, "api")
        engine._reconcile(uid, now)

    def _event(self, jid, request):
        uid = request["user_id"]
        folder = self.runtime / "viewers" / uid
        pack = self._view_pack(uid, live=True)
        store, ledger = Store(folder / "state.sqlite3"), None
        try:
            # Keep the first live coverage gap after server restarts.
            old_gap = store.get_meta("live_coverage_start")
            if old_gap:
                pack.observation_gaps = [(iso(self.pack.replay_start), old_gap)]
            else:
                with store.transaction():
                    store.set_meta("live_coverage_start", self.started_at)
            provider, ledger = self._provider(request["provider"], jid)
            engine = Engine(pack, store, provider, workers=1, live_decisions=1_000_000)
            with store.transaction():
                self._refresh(engine, datetime.now(UTC))
            event = next(e for e in self.incoming(uid) if e["event_id"] == request["event_id"])
            decision = engine.process_event(event, before_publish=self._refresh, publication_guard=self._publication_guard)
            # Persist fresh facts even when a non-trigger updated memory only.
            from .domain import resolve_facts
            with store.transaction():
                state = store.viewer(uid)
                latest = max(engine.history[uid], key=lambda e: e["received_at"])
                state["facts"] = resolve_facts(pack, engine.history[uid], latest, datetime.now(UTC))
                state["facts"].pop("evidence_index", None)
                store.put_viewer(uid, state)
            result = {"run_id": "live:" + uid, "viewer_id": uid, "decision_id": decision["decision_id"] if decision else None,
                "mode": "live_events", "provider_spend": ledger.summary() if ledger else {"run_accounted_usd": 0},
                "note": "Actual server receipt time. Historical coverage gap remains unknown; no outcome simulator runs here."}
            return result
        finally:
            if ledger:
                ledger.close()
            store.close()

    def _demo(self, jid, request):
        trigger = next(e for e in self.pack.triggers if e["event_id"] == request["trigger_event_id"])
        uid, at = trigger["user_id"], ts(trigger["deliver_at"])
        pack = self._view_pack(uid)
        seen = {e["event_id"] for e in pack.history}
        for e in sorted(self.pack.triggers + self.pack.live_events, key=lambda e: e["deliver_at"]):
            if e["user_id"] == uid and ts(e["deliver_at"]) < at and e["event_id"] != trigger["event_id"] and e["event_id"] not in seen:
                pack.history.append(e)
                seen.add(e["event_id"])
        pack.triggers = [trigger]
        # Include later observations/controls that can arrive during inference.
        pack.live_events = [e for e in self.pack.live_events if e["user_id"] == uid and at <= ts(e["deliver_at"]) <= at + timedelta(seconds=60)]
        pack.control_events = [c for c in self.pack.control_events if ts(c["at"]) <= at + timedelta(seconds=60)]
        folder = self.runtime / "demos" / jid
        store, ledger = Store(folder / "state.sqlite3"), None
        try:
            provider, ledger = self._provider(request["provider"], jid)
            Engine(pack, store, provider, workers=1, live_decisions=1).run()
            did = stable_id("D", trigger["event_id"])
            result = {"run_id": "demo:" + jid, "viewer_id": uid, "decision_id": did, "mode": "source_trigger_sandbox",
                "provider_spend": ledger.summary() if ledger else {"run_accounted_usd": 0},
                "note": "Isolated plan at supplied replay time, with prior delivered facts; no prior agent commitments or simulated future outcomes. Excluded from impact reports."}
            decisions = store.decisions()
            write_json(folder / "run_meta.json", {**result, "provider": request["provider"],
                "decision_count": len(decisions), "published_decisions": sum(d["status"] == "PUBLISHED" for d in decisions),
                "provider_attempts": sum(d["provider_result"]["provider"] == "openai" for d in decisions),
                "completed_at": iso(datetime.now(UTC))})
            return result
        finally:
            if ledger:
                ledger.close()
            store.close()

    def _replay(self, jid, request):
        folder = self.project / "runs" / ("api_" + jid)
        cmd = [sys.executable, str(self.project / "run.py"), "replay" if request["provider"] == "replay" else "demo",
               "--out", str(folder), "--pack", str(self.pack.root), "--decisions", str(request["decisions"])]
        if (folder / "state.sqlite3").exists():
            cmd.append("--resume")
        # A separate process isolates the full evaluator from event workers.
        with subprocess.Popen(cmd, cwd=self.project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            for line in proc.stdout:
                try:
                    self._update(jid, progress=json.loads(line))
                except json.JSONDecodeError:
                    pass
            proc.communicate(timeout=180)
            if proc.returncode:
                raise RuntimeError("Replay subprocess failed")
        return {"run_id": "runs/api_" + jid, "mode": "full_replay", "metadata": read_json(folder / "run_meta.json", {})}

    def run_paths(self):
        result = {}
        for path in [self.project / "sample_run", self.project / "live_demo"] + sorted((self.project / "runs").glob("*")):
            if path.is_dir() and not path.is_symlink() and (path / "run_meta.json").is_file():
                result[str(path.relative_to(self.project))] = path
        with self.db() as db:
            demos = [self.job(r[0]) for r in db.execute("SELECT job_id FROM jobs WHERE kind='demo' AND status='SUCCEEDED'")]
        for job in demos:
            info = job.get("result") or {}
            if info.get("run_id", "").startswith("demo:"):
                result[info["run_id"]] = self.runtime / "demos" / job["job_id"]
        for path in (self.runtime / "viewers").glob("*"):
            if path.is_dir():
                result["live:" + path.name] = path
        return result

    def reader(self, rid):
        path = self.run_paths().get(rid)
        if not path:
            raise KeyError("Unknown run")
        with self._mutex:
            return self._readers.setdefault(rid, RunReader(path, self.pack))

    def metadata(self, rid, path):
        meta = read_json(path / "run_meta.json", {})
        if not rid.startswith("live:"):
            return meta
        db = sqlite3.connect((path / "state.sqlite3").as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute("SELECT COUNT(*),COALESCE(SUM(json_extract(payload,'$.status')='PUBLISHED'),0),COALESCE(SUM(json_extract(payload,'$.provider_result.provider')='openai'),0) FROM decisions").fetchone()
        finally:
            db.close()
        with self.db() as db:
            jids = [r[0] for r in db.execute("SELECT job_id FROM incoming WHERE user_id=?", (rid[5:],))]
        cost = 0
        budget = self.project / "runtime/inference_budget.sqlite3"
        if jids and budget.exists():
            db = sqlite3.connect(budget.as_uri() + "?mode=ro", uri=True)
            try:
                # Includes retained reservations of failed/in-flight requests.
                cost = db.execute("SELECT COALESCE(SUM(COALESCE(charged_usd,reserved_usd)),0) FROM inference_budget WHERE run_id IN (" + ",".join("?" for _ in jids) + ")", jids).fetchone()[0]
            finally:
                db.close()
        return {"mode": "live_events", "decision_count": row[0], "published_decisions": row[1], "provider_attempts": row[2],
                "provider_spend": {"run_accounted_usd": cost}, "cost_scope": "All API jobs for this viewer, including retained uncertain reservations."}
