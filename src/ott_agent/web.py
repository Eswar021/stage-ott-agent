"""Local API and review console. Start with: python run.py serve."""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audit import decision_summaries
from .pack import Pack
from .review import read_json
from .service import Conflict, Service
from .util import UTC, available, ts

ID = r"^[A-Za-z0-9_-]{1,100}$"
Provider = Literal["replay", "openai"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventInput(Strict):
    event_id: str = Field(pattern=ID)
    user_id: str = Field(pattern=ID)
    event: Literal["app_open", "login_success", "signup_completed", "home_viewed", "title_viewed", "play_started", "play_progress", "episode_completed", "search", "paywall_viewed", "purchase_completed", "subscription_renewed", "subscription_lapsed", "notification_opt_out", "app_dialect_changed", "message_clicked", "message_delivered"]
    session_id: str = Field(default="api-session", max_length=100)
    occurred_at: str | None = None
    sent_at: str | None = None
    app_dialect: str | None = Field(default=None, max_length=10)
    app_language: Literal["hin", "en"] | None = None
    properties: dict = Field(default_factory=dict)
    provider: Provider = "replay"

    @field_validator("occurred_at", "sent_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Phone timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def properties_contract(self):
        for field in ("title_id", "episode_id", "plan_id", "channel"):
            value = self.properties.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                raise ValueError("Reference properties must be short strings")
        if self.event == "purchase_completed" and type(self.properties.get("is_trial")) is not bool:
            raise ValueError("purchase_completed requires a boolean properties.is_trial")
        if self.event == "notification_opt_out" and self.properties.get("channel") not in {"push", "whatsapp"}:
            raise ValueError("notification_opt_out requires properties.channel: push or whatsapp")
        return self


class DemoInput(Strict):
    trigger_event_id: str = Field(pattern=ID)
    provider: Provider = "replay"


class ReplayInput(Strict):
    provider: Provider = "replay"
    decisions: int = Field(default=12, ge=0, le=20)


class WithdrawalInput(Strict):
    title_id: str = Field(pattern=ID)


def create_app(*, project=None, pack=None, runtime=None, provider_factory=None):
    project = Path(project or Path(__file__).resolve().parents[2]).resolve()

    @asynccontextmanager
    async def lifespan(app):
        from dotenv import load_dotenv
        load_dotenv(project / ".env", override=False)
        source = pack or Pack(project.parent / "my_analysis/one_user_one_agent_pack")
        app.state.service = Service(project, source, runtime=runtime, provider_factory=provider_factory)
        try:
            yield
        finally:
            app.state.service.close()

    app = FastAPI(title="One User, One Agent", version="0.2.0", lifespan=lifespan,
        description="Local assignment API. One event per request; durable background decisions. Source-trigger sandboxes and full replay are separate workflows. Defaults to keyless offline replay.")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        # Local-only service: prevent another website driving paid localhost POSTs.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if origin and (urlsplit(origin).netloc != request.headers.get("host") or urlsplit(origin).scheme not in {"http", "https"}):
                return JSONResponse({"detail": "Cross-origin mutations are not permitted"}, status_code=403)
            if "application/json" not in request.headers.get("content-type", ""):
                return JSONResponse({"detail": "Use application/json"}, status_code=415)
            # Read with a hard cap even when Content-Length is absent/chunked.
            chunks, count = [], 0
            async for chunk in request.stream():
                count += len(chunk)
                if count > 65536:
                    return JSONResponse({"detail": "Request exceeds 64 KiB"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def service():
        return app.state.service

    def reader(rid):
        try:
            return service().reader(rid)
        except KeyError:
            raise HTTPException(404, "Unknown run") from None

    def identifier(value):
        if not re.fullmatch(ID, value):
            raise HTTPException(422, "Invalid identifier")
        return value

    def enqueue(kind, payload, key=None):
        if key and (len(key) > 150 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", key)):
            raise HTTPException(422, "Invalid Idempotency-Key")
        try:
            job = service().submit(kind, payload, key=key)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {**job, "status_url": "/jobs/" + job["job_id"]}

    @app.get("/health", tags=["System"])
    def health():
        return {"status": "ok", "mode": "local_assignment_service", "workers": 4}

    @app.get("/config", tags=["System"])
    def config():
        # Return presence/settings only, never credential values.
        return {"openai_available": bool(os.getenv("OPENAI_API_KEY") or provider_factory),
            "default_provider": "replay", "model": os.getenv("OPENAI_MODEL", "gpt-6-luna"),
            "note": "Browsing results is free. Only explicit OpenAI jobs can incur charges. Bind to loopback; authentication is not supplied.",
            "source": "Supplied assignment pack", "live_coverage_note": "New HTTP events use actual server time; the gap after the historical export is unknown."}

    @app.get("/runs", tags=["Review"])
    def runs():
        return [{"run_id": rid, "metadata": service().metadata(rid, path),
                 "report": read_json(path / "report_summary.json", {})} for rid, path in service().run_paths().items()]

    @app.get("/decisions", tags=["Review"])
    def decisions(run: str = "sample_run", viewer: str | None = None, provider: str | None = None,
                  status: str | None = None, search: str = "", offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=100)):
        rows = reader(run).summaries()
        if viewer:
            identifier(viewer)
            rows = [d for d in rows if d["viewer_id"] == viewer]
        if provider:
            rows = [d for d in rows if d["provider"] == provider]
        if status:
            rows = [d for d in rows if d["status"] == status]
        if search:
            query = search.lower()[:100]
            rows = [d for d in rows if query in str((d["viewer_id"], d["decision_id"], d.get("goal"), d["content"])).lower()]
        return {"total": len(rows), "items": rows[offset:offset + limit], "offset": offset, "limit": limit}

    @app.get("/decisions/{decision_id}", tags=["Review"])
    def decision(decision_id: str, run: str = "sample_run"):
        result = reader(run).detail(identifier(decision_id))
        if not result:
            raise HTTPException(404, "Decision not found in this run")
        return result

    @app.get("/viewers/{viewer_id}", tags=["Review"])
    def viewer(viewer_id: str, run: str = "sample_run"):
        state = reader(run).viewer(identifier(viewer_id))
        if state is None:
            raise HTTPException(404, "Viewer not found in this run")
        return state

    @app.get("/viewers/{viewer_id}/home", tags=["Events"])
    def home(viewer_id: str, run: str | None = None, at: datetime | None = None):
        uid = identifier(viewer_id)
        rid = run or "live:" + uid
        if rid.startswith("live:") and at:
            raise HTTPException(422, "Live serving always uses server time; at is only for recorded snapshots")
        if not run and rid not in service().run_paths():
            return {"status": "DEFAULT", "page": None, "reason": "no_approved_page"}
        if at and at.tzinfo is None:
            raise HTTPException(422, "at must include a timezone")
        withdrawals = {c["title_id"]: c["at"] for c in service().live_controls()} if rid.startswith("live:") else {}
        result = reader(rid).home(uid, ts(at) if at else datetime.now(UTC), withdrawals)
        if rid.startswith("live:") and result["page"]:
            # Inbox facts apply immediately, even while a worker owns inference.
            page = result["page"]
            changes = [e for e in service().incoming(uid) if ts(e["received_at"]) >= ts(page["published_at"])
                       and e["event"] in {"purchase_completed", "subscription_renewed", "subscription_lapsed", "notification_opt_out", "app_dialect_changed", "play_started", "episode_completed"}]
            if changes:
                result.update(status="DEFAULT", page=None, reason="new_evidence_pending_or_applied")
        return {**result, "historical_snapshot": at is not None}

    @app.get("/reports", tags=["Review"])
    def reports(run: str = "sample_run"):
        path = reader(run).path
        return {name: read_json(path / (name + ".json")) for name in ("run_meta", "report_summary", "impact_report", "latency_report", "validation_report", "robustness_report")}

    @app.get("/demo/triggers", tags=["Demo"])
    def triggers(viewer: str = "", limit: int = Query(30, ge=1, le=100)):
        unique, seen = [], set()
        for e in service().pack.triggers:
            if e["event_id"] in seen or viewer.lower() not in e["user_id"].lower():
                continue
            seen.add(e["event_id"])
            unique.append({k: e.get(k) for k in ("event_id", "user_id", "event", "deliver_at", "app_dialect") } | {"arm": service().pack.arms.get(e["user_id"], "holdout")})
        return {"total": len(unique), "items": unique[:limit]}

    @app.post("/events", status_code=202, tags=["Events"])
    def event(payload: EventInput):
        if payload.event == "app_dialect_changed" and payload.properties.get("to") is not None and payload.properties["to"] != payload.app_dialect:
            raise HTTPException(422, "properties.to must match the new top-level app_dialect setting")
        for field in ("title_id",):
            if payload.properties.get(field) and payload.properties[field] not in service().pack.catalogue:
                raise HTTPException(422, "Unknown title reference")
        if payload.event in {"play_started", "play_progress", "episode_completed", "title_viewed"} and not payload.properties.get("title_id"):
            raise HTTPException(422, "Content events require properties.title_id")
        episode = payload.properties.get("episode_id")
        title = service().pack.catalogue.get(payload.properties.get("title_id"))
        if episode and (not title or episode not in {e["episode_id"] for e in service().pack.episodes.get(title["content_slug"], [])}):
            raise HTTPException(422, "Episode does not belong to the supplied title")
        if payload.event in {"play_started", "play_progress", "episode_completed"} and not episode:
            raise HTTPException(422, "Playback events require properties.episode_id")
        if payload.event in {"purchase_completed", "subscription_renewed"}:
            plans = {p["plan_id"] for p in service().pack.pricing["plans"]}
            if payload.properties.get("plan_id") not in plans:
                raise HTTPException(422, "Billing events require a known properties.plan_id")
        return enqueue("event", payload.model_dump())

    @app.get("/events/examples", tags=["Events"])
    def event_example(event_type: Literal["app_open", "login_success", "play_started", "episode_completed", "purchase_completed", "notification_opt_out", "app_dialect_changed"],
                      dialect: Literal["d1", "d2", "d3", "d4", "d5", "d6"] = "d3"):
        """Form assistance only; GET never records an event or calls a model."""
        properties, required, title_name = {}, [], None
        if event_type in {"app_open", "login_success"}:
            hint = "No extra properties are required. Viewer ID, session, app settings and generated timestamps are sent outside this JSON."
        elif event_type == "notification_opt_out":
            properties, required = {"channel": "push"}, ["channel"]
            hint = "Report the channel the viewer opted out of: push or whatsapp. This updates consent without a new model call."
        elif event_type == "purchase_completed":
            plans = service().pack.pricing["plans"]
            plan = next((p for p in plans if p["plan_id"] == "P_MONTH"), plans[0])
            properties = {"plan_id": plan["plan_id"], "price_inr": plan["price_inr"], "is_trial": plan["plan_id"] == "P_TRIAL"}
            required = ["plan_id", "is_trial"]
            hint = "Example purchase using a supplied plan and price. Edit it to represent the purchase you intend to report; this updates billing/goal facts without a new model call."
        elif event_type == "app_dialect_changed":
            properties = {"to": dialect}
            hint = "Set Current app dialect above to the new setting. The agent reads that top-level field; to mirrors it here for the audit. Add from only if the previous setting is known."
        else:
            now = datetime.now(UTC)
            controls = service().pack.control_events + service().live_controls()
            withdrawn = {c["title_id"]: c["at"] for c in controls if c["type"] == "title_withdrawn" and ts(c["at"]) <= now}
            titles = [t for t in service().pack.catalogue.values() if t.get("dialect") == dialect
                      and available(t, now, withdrawn) and service().pack.episodes.get(t["content_slug"])]
            titles.sort(key=lambda t: (str(t.get("is_premium")).lower() == "true", t["title_id"]))
            if not titles:
                raise HTTPException(422, "No available title with an episode exists for this dialect; choose another dialect.")
            title = titles[0]
            episode = service().pack.episodes[title["content_slug"]][0]
            properties = {"title_id": title["title_id"], "episode_id": episode["episode_id"]}
            required, title_name = ["title_id", "episode_id"], title["title"]
            hint = "Report the title and episode actually played." if event_type == "play_started" else "Report the completed title and episode (90% or more watched in the source contract)."
            hint += " The prefilled pair is a valid catalogue example, not an observed playback or a recommendation."
        return {"event_type": event_type, "properties": properties, "required_properties": required,
                "hint": hint, "example_title": title_name, "records_event": False, "calls_model": False}

    @app.post("/demo/decisions", status_code=202, tags=["Demo"])
    def demo(payload: DemoInput, idempotency_key: str | None = Header(default=None)):
        return enqueue("demo", payload.model_dump(), idempotency_key)

    @app.post("/replays", status_code=202, tags=["Replay"])
    def replay(payload: ReplayInput, idempotency_key: str | None = Header(default=None)):
        return enqueue("replay", payload.model_dump(), idempotency_key)

    @app.get("/jobs", tags=["Jobs"])
    def jobs():
        return service().jobs()

    @app.get("/jobs/{job_id}", tags=["Jobs"])
    @app.get("/replays/{job_id}", tags=["Replay"])
    def job(job_id: str):
        result = service().job(identifier(job_id))
        if not result:
            raise HTTPException(404, "Unknown job")
        return result

    @app.post("/jobs/{job_id}/retry", status_code=202, tags=["Jobs"])
    def retry(job_id: str):
        if not service().job(identifier(job_id)):
            raise HTTPException(404, "Unknown job")
        try:
            return service().retry(job_id)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/controls/withdrawals", tags=["Events"])
    def withdrawal(payload: WithdrawalInput):
        try:
            return service().control(payload.title_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    assets = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(assets / "index.html")

    return app
