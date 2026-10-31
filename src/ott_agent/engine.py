"""Delivery-ordered replay with durable jobs and publication-time revalidation.

Provider service times are measured in wall time. A bounded logical worker pool
uses those measurements to account for replay queue wait; this is a replay
scheduler, not a claim of production throughput benchmarking.
"""
from __future__ import annotations

import copy
import hashlib
import heapq
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import datetime, timedelta

from .domain import build_context, content_candidates, resolve_facts, verify_proposal
from .outcomes import judge_goal
from .providers import ReplayProvider
from .util import UTC, available, buffering_seconds, event_is_buffered, event_time, iso, ist_day, stable_id, ts


class Engine:
    def __init__(self, pack, store, provider=None, *, workers=4, model_timeout_s=12.0,
                 store_timeout_s=1.0, max_model_calls=8, max_tool_calls=20,
                 live_decisions=20):
        self.pack, self.store = pack, store
        self.provider = provider or ReplayProvider()
        self.fallback = ReplayProvider()
        self.workers = max(1, int(workers))
        self.model_timeout_s, self.store_timeout_s = model_timeout_s, store_timeout_s
        self.max_model_calls, self.max_tool_calls = max_model_calls, max_tool_calls
        self.live_decisions = max(0, live_decisions)
        self.live_attempts = store.get_meta("live_attempts", 0)
        self.queue, self.sequence = [], 0
        self.worker_available = [pack.replay_start] * self.workers
        self.viewer_available = defaultdict(lambda: pack.replay_start)
        self.rules = store.get_meta("rule_overrides", {})
        self.withdrawn = store.get_meta("withdrawn", {})
        self.dependencies = store.get_meta("dependencies", {})
        self.history = defaultdict(list)
        if not store.get_meta("history_imported"):
            with store.transaction():
                store.ingest_many(pack.history, "history")
                store.set_meta("history_imported", True)
        for event in store.all_events():
            self.history[event["user_id"]].append(event)
        self.seen_ids = {e["event_id"] for rows in self.history.values() for e in rows}
        for pending in store.pending():
            if pending.get("phase") == "complete":
                end = ts(pending["complete_at"])
                self.worker_available[pending["worker"]] = max(self.worker_available[pending["worker"]], end)
                self.viewer_available[pending["trigger"]["user_id"]] = end
            self._push(ts(pending["complete_at"]), 2 if pending.get("phase") == "complete" else 4, pending["phase"], pending)

    def _push(self, at, priority, kind, payload):
        self.sequence += 1
        heapq.heappush(self.queue, (at, priority, self.sequence, kind, payload))

    def _ingest(self, event, source):
        if event["event_id"] in self.seen_ids:
            return False
        self.seen_ids.add(event["event_id"])
        self.history[event["user_id"]].append(event)
        self.store.ingest(event, source)
        state = self.store.viewer(event["user_id"])
        state["last_event_id"] = event["event_id"]
        state["last_known_at"] = event.get("deliver_at", event["received_at"])
        self.store.put_viewer(event["user_id"], state, bump=True)
        if event["event"] == "message_delivered":
            channel = event.get("properties", {}).get("channel")
            if channel in {"push", "whatsapp"}:
                self.store.observe_message(event["user_id"], ist_day(event_time(event)), "outbound", event["event_id"])
        return True

    def _reconcile(self, uid, now):
        goal = self.store.open_goal(uid)
        if not goal:
            return
        result = judge_goal(goal, self.history[uid], self.pack, now)
        if result["status"] == "failed" and getattr(self.pack, "incomplete_live_coverage", False):
            result.update(status="inconclusive", reason="Live ingestion has no complete-observation watermark; absence cannot prove failure.")
        # Late evidence can reveal that the entry condition was false, rather
        # than representing a new success. Do not let receipt manufacture a
        # first play/purchase or keep an invalid baseline commitment alive.
        contradictory = []
        for event in self.history[uid]:
            when = event_time(event)
            if ts(event["received_at"]) <= ts(goal["started_at"]) or when > ts(goal["started_at"]) or buffering_seconds(event) < 0:
                continue
            kind, props = event["event"], event.get("properties", {})
            gid = goal["id"]
            paid = kind == "subscription_renewed" or (kind == "purchase_completed" and props.get("is_trial") is False)
            if (gid == "G01" and kind == "play_started" or gid == "G02" and kind == "episode_completed"
                or gid == "G05" and paid
                or gid == "G12" and kind == "app_open" and when >= ts(goal["started_at"]) - timedelta(days=14)):
                contradictory.append(f"event:{event['event_id']}")
        if contradictory:
            goal["status"] = "invalidated"
            goal["invalidated_at"] = iso(now)
            goal["invalidation_reason"] = "Late historical evidence contradicts the goal's entry condition."
            goal["invalidation_evidence"] = contradictory
            result = judge_goal(goal, self.history[uid], self.pack, now)
            result["evidence"] = contradictory
            goal["judgment"] = result
            self.store.put_goal(goal)
            state = self.store.viewer(uid)
            state["active_goal"], state["active_plan"] = None, None
            state["plan_invalidated_by"] = {"reason": goal["invalidation_reason"], "evidence": contradictory, "at": iso(now)}
            self.store.put_viewer(uid, state, bump=True)
            return
        if result["status"] in {"succeeded", "failed", "inconclusive"} and (result["status"] != "inconclusive" or now >= ts(goal["deadline"])):
            goal["status"] = result["status"]
            goal["judgment"] = result
            goal["closed_at"] = iso(now)
            self.store.put_goal(goal)
            state = self.store.viewer(uid)
            state["active_goal"] = None
            state["active_plan"] = None
            state.setdefault("learning", []).append({"goal_instance_id": goal["goal_instance_id"],
                "status": result["status"], "evidence": result.get("evidence", result.get("evidence_refs", [])),
                "note": "Observed goal result; no individual causal claim."})
            self.store.put_viewer(uid, state, bump=True)

    def _control(self, control, now):
        kind = control["type"]
        record = dict(control, affected_goal_instances=[], affected_action_ids=[])
        if kind == "title_withdrawn":
            tid = control["title_id"]
            self.withdrawn[tid] = control["at"]
            for action in self.store.actions():
                ids = [action.get("hero_title_id"), action.get("title_id")]
                ids.extend(t for row in action.get("rows", []) for t in row.get("title_ids", []))
                if tid in ids and ts(action["published_at"]) + timedelta(minutes=120) > now:
                    record["affected_action_ids"].append(action["action_id"])
                    state = self.store.viewer(action["user_id"])
                    state["plan_invalidated_by"] = {"reason": "title_withdrawn", "title_id": tid, "at": iso(now)}
                    self.store.put_viewer(action["user_id"], state, bump=True)
            record["handling"] = "Candidate withdrawal effective immediately; affected live pages marked invalid; replacement at next safe trigger. Simulator has no retraction action."
            self.store.set_meta("withdrawn", self.withdrawn)
        elif kind == "goal_rule_corrected":
            gid = control["goal_id"]
            success = {**self.pack.goals[gid]["success"], **control["new_success"]}
            version = f"{gid}@{control['at']}"
            self.rules[gid] = {"success": success, "version": version, "effective_at": control["at"]}
            for goal in self.store.open_goals_for_rule(gid):
                goal.setdefault("rule_history", []).append({"version": goal["version"], "success": goal["success"], "changed_at": iso(now)})
                goal["version"], goal["success"] = version, success
                goal["rule_corrected_at"] = iso(now)
                self.store.put_goal(goal)
                state = self.store.viewer(goal["user_id"])
                state["active_goal"] = goal
                state["plan_invalidated_by"] = {"reason": "goal_rule_corrected", "at": iso(now)}
                self.store.put_viewer(goal["user_id"], state, bump=True)
                record["affected_goal_instances"].append(goal["goal_instance_id"])
            self.store.set_meta("rule_overrides", self.rules)
        elif kind in {"dependency_unavailable", "dependency_degraded"}:
            self.dependencies[control["dependency"]] = control
            self.store.set_meta("dependencies", self.dependencies)
        self.store.put_control(stable_id("C", control), record)

    def _dependency(self, name, now):
        control = self.dependencies.get(name)
        return control if control and ts(control["at"]) <= now < ts(control["until"]) else None

    def _store_read(self, trigger, now):
        control = self._dependency("user_data_store", now)
        if not control:
            return None
        # The hash chooses a reproducible request fault, not a preferred viewer.
        h = int(hashlib.sha256((control["at"] + trigger["event_id"]).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        if h < 0.20:
            time.sleep(min(0.03, self.store_timeout_s))
            return {"error": "user_data_store_timeout", "injected": "20_percent_timeout", "waited_s": min(0.03, self.store_timeout_s)}
        latency = 8.0 if h > 0.90 else 0.02
        time.sleep(min(latency, self.store_timeout_s))
        if latency > self.store_timeout_s:
            return {"error": "user_data_store_timeout", "injected": "p95_8_second_read", "waited_s": self.store_timeout_s}
        return {"error": None, "injected": "degraded_read", "waited_s": latency}

    def _context(self, trigger, now):
        uid = trigger["user_id"]
        active = self.store.open_goal(uid)
        if active:
            active = copy.deepcopy(active)
            active["plan"] = self.store.viewer(uid).get("active_plan") or active.get("plan")
        context = build_context(self.pack, self.history[uid], trigger, now,
            active, self.store.actions(uid), self.rules, self.withdrawn)
        context["state_revision"] = self.store.revision(uid)
        context["as_of"] = iso(now)
        context["budget"] = {"max_model_calls": self.max_model_calls, "max_tool_calls": self.max_tool_calls,
                             "remaining_model_calls": self.max_model_calls, "remaining_tool_calls": max(0, self.max_tool_calls - 4)}
        return context

    def _tool_handler(self, context, trigger, now):
        uid = trigger["user_id"]

        def read(name, args):
            if name == "read_goal_rule":
                return next((g for g in context["goal_assessments"] if g["goal_id"] == args["goal_id"]), {"error": "unknown_goal"})
            if name == "read_series_progress":
                slug = args["content_slug"]
                progress = next((s for s in context["facts"]["behavior"].get("unfinished_series", []) if s.get("content_slug") == slug), None)
                inventory = self.pack.episodes.get(slug, [])
                completed = {e.get("properties", {}).get("episode_id") for e in self.history[uid]
                    if e["event"] == "episode_completed" and ts(e["received_at"]) <= now
                    and self.pack.catalogue.get(e.get("properties", {}).get("title_id"), {}).get("content_slug") == slug}
                remaining = [ep for ep in inventory if ep["episode_id"] not in completed]
                return {"content_slug": slug, "progress": progress, "episode_count": len(inventory),
                        "observed_completed_count": len(completed), "remaining_count": len(remaining),
                        "next_episodes": remaining[:5], "evidence_ref": f"episodes:{slug}", "as_of": iso(now),
                        "bounded": len(remaining) > 5}
            if name == "read_viewer_evidence":
                minimum = now - timedelta(days=min(30, max(1, args["window_days"])))
                events = [e for e in self.history[uid] if minimum <= ts(e["received_at"]) <= now
                          and (not args["event_types"] or e["event"] in args["event_types"])]
                chosen = events[-min(6, args["limit"]):]
                for e in chosen:
                    context["evidence_index"].setdefault(f"event:{e['event_id']}", {
                        "source": "event", "known_at": e["received_at"], "event": e["event"],
                        "user_id": uid, "estimated_at": iso(event_time(e))})
                facts = {k: context["facts"].get(k) for k in args["fact_keys"][:6] if k in context["facts"] and k not in {"provenance", "observation_coverage"}}
                if "behavior" in facts:
                    behavior = facts["behavior"]
                    facts["behavior"] = {k: behavior.get(k) for k in ("play_count", "completion_count", "play_days_14d", "top_genres", "last_play_at")}
                properties = {"title_id", "episode_id", "plan_id", "is_trial", "channel", "percent"}
                return {"facts": facts,
                        "events": [{"event_id": e["event_id"], "event": e["event"], "received_at": e["received_at"],
                                    "estimated_at": iso(event_time(e)), "properties": {k: v for k, v in e.get("properties", {}).items() if k in properties}} for e in chosen],
                        "coverage": context["facts"].get("observation_coverage"), "as_of": iso(now),
                        "bounded": len(events) > len(chosen), "returned_count": len(chosen)}
            if name == "read_content_candidates":
                candidates = content_candidates(self.pack, context["facts"], now, self.withdrawn,
                    limit=min(8, args["limit"]), genre=args.get("genre"), exclude_slugs=args["exclude_slugs"])
                known = {c["title_id"] for c in context["candidates"]}
                for candidate in candidates:
                    if candidate["title_id"] not in known:
                        context["candidates"].append(candidate)
                        known.add(candidate["title_id"])
                    context["evidence_index"].setdefault(f"catalogue:{candidate['title_id']}", {
                        "source": "catalogue", "title_id": candidate["title_id"], "known_at": iso(now)})
                keys = ("title_id", "content_slug", "title", "content_type", "genres", "release_date", "is_premium", "evidence_ref")
                return {"candidates": [{k: c[k] for k in keys if k in c} for c in candidates], "as_of": iso(now),
                        "dialect": context["facts"].get("dialect"), "returned_count": len(candidates),
                        "batch_cap": 8, "scope": "Available current-dialect variants; additional batches may exclude returned slugs."}
            raise ValueError("Unsupported tool")

        return read

    def _abstain(self, reason):
        return {"goal_transition": "none", "goal_id": None, "target_slug": None,
            "reason_summary": reason, "evidence_refs": [], "assumptions": [], "uncertainties": [],
            "confidence": {"level": "low", "reason": reason},
            "plan": {"intended_behavior_change": "Wait for supported evidence", "review_on": ["next_fresh_trigger"]},
            "actions": [], "no_action_reason": reason}

    def _existing_page_suffices(self, context, trigger, now):
        active = context.get("active_goal")
        if not active or context.get("active_goal_viable") is False:
            return False
        pages = [a for a in self.store.actions(trigger["user_id"]) if a["type"] == "home_page"
                 and a.get("goal_instance_id") == active["goal_instance_id"]
                 and a.get("dialect") == context["facts"].get("dialect")
                 and ts(a["published_at"]) <= now < ts(a["published_at"]) + timedelta(minutes=120)]
        if not pages:
            return False
        page = pages[-1]
        ids = [page.get("hero_title_id")]
        ids.extend(t for r in page.get("rows", []) for t in r.get("title_ids", []))
        if not all(available(self.pack.catalogue.get(t), now, self.withdrawn) for t in ids if t):
            return False
        changes = {"purchase_completed", "subscription_renewed", "subscription_lapsed", "notification_opt_out",
                   "play_started", "episode_completed", "app_dialect_changed"}
        if any(e["event"] in changes and ts(e["received_at"]) > ts(page["published_at"]) for e in self.history[trigger["user_id"]]):
            return False
        if self.store.viewer(trigger["user_id"]).get("plan_invalidated_by"):
            return False
        return True

    def _smaller_layout(self, proposal):
        """Remove repeated content, without inventing titles or changing the goal."""
        smaller = copy.deepcopy(proposal)
        for action in smaller.get("actions", []):
            if action.get("type") != "home_page":
                continue
            hero = self.pack.catalogue.get(action.get("hero_title_id"))
            seen = {hero["content_slug"]} if hero else set()
            rows = []
            for row in action.get("rows", []):
                ids = []
                for tid in row["title_ids"]:
                    slug = self.pack.catalogue[tid]["content_slug"]
                    if slug not in seen:
                        seen.add(slug)
                        ids.append(tid)
                if len(ids) >= 3:
                    rows.append({**row, "title_ids": ids})
            action["rows"] = rows
        return smaller

    def _trigger(self, trigger, now, delivery_key):
        gate_started = time.perf_counter()
        uid = trigger["user_id"]
        new = self._ingest(trigger, "trigger")
        self._reconcile(uid, now)
        if not self.store.has_baseline(uid):
            facts = resolve_facts(self.pack, self.history[uid], trigger, now)
            paid = True if facts.get("ever_paid") is True or facts.get("subscription_status") == "active" else facts.get("ever_paid")
            self.store.put_baseline(uid, {"arm": self.pack.arms.get(uid), "first_trigger_at": trigger["deliver_at"],
                "anchor_at": trigger["deliver_at"], "facts": facts, "paid_at_baseline": paid,
                "subscription_status": facts.get("subscription_status"), "ever_paid": facts.get("ever_paid"),
                "fresh_trigger": not event_is_buffered(trigger)})
        original = stable_id("D", trigger["event_id"])
        if not new:
            job = {"decision_id": stable_id("D-duplicate", delivery_key), "trigger": trigger,
                   "complete_at": iso(now), "started_at": iso(now), "phase": "complete", "worker": 0,
                   "result": {"proposal": self._abstain("duplicate_trigger"), "provider": "gate", "calls": 0, "tool_calls": 0},
                   "service_ms": 0.0, "queue_wait_ms": 0.0, "initial_context": None,
                   "gate_reason": "duplicate_trigger", "original_decision_id": original}
            elapsed = (time.perf_counter() - gate_started) * 1000
            job["service_ms"], job["complete_at"] = elapsed, iso(now + timedelta(milliseconds=elapsed))
            self.store.queue(job)
            self._push(ts(job["complete_at"]), 2, "complete", job)
            return
        if self.pack.arms.get(uid) == "holdout" or event_is_buffered(trigger):
            reason = "holdout" if self.pack.arms.get(uid) == "holdout" else "buffered_trigger_not_live_visit"
            job = {"decision_id": original, "trigger": trigger, "complete_at": iso(now), "started_at": iso(now),
                   "phase": "complete", "worker": 0, "result": {"proposal": self._abstain(reason), "provider": "gate", "calls": 0, "tool_calls": 0},
                   "service_ms": 0.0, "queue_wait_ms": 0.0, "initial_context": None, "gate_reason": reason}
            elapsed = (time.perf_counter() - gate_started) * 1000
            job["service_ms"], job["complete_at"] = elapsed, iso(now + timedelta(milliseconds=elapsed))
            self.store.queue(job)
            self._push(ts(job["complete_at"]), 2, "complete", job)
            return
        worker = min(range(self.workers), key=lambda w: self.worker_available[w])
        start = max(now, self.worker_available[worker], self.viewer_available[uid])
        job = {"decision_id": original, "trigger": trigger, "complete_at": iso(start), "phase": "start", "worker": worker}
        self.store.queue(job)
        self._push(start, 4, "start", job)

    def _start(self, job, now):
        uid = job["trigger"]["user_id"]
        worker = min(range(self.workers), key=lambda w: self.worker_available[w])
        job["worker"] = worker
        start = max(now, self.worker_available[worker], self.viewer_available[uid])
        if start > now:
            job["complete_at"] = iso(start)
            self.store.queue(job)
            self._push(start, 4, "start", job)
            return
        wall_start = time.perf_counter()
        fault = self._store_read(job["trigger"], now)
        context = self._context(job["trigger"], now)
        reason = None
        result = None
        if fault and fault.get("error"):
            reason = fault["error"]
        elif not context["facts"].get("dialect"):
            reason = "unknown_or_unsupported_current_dialect"
        elif not context["eligible_goals"] and job["trigger"]["event_id"] not in self.pack.model_faults:
            reason = "no_supported_eligible_goal"
        elif job["trigger"]["event_id"] not in self.pack.model_faults and self._existing_page_suffices(context, job["trigger"], now):
            reason = "existing_valid_page_and_plan_retained"
        if reason:
            result = {"proposal": self._abstain(reason), "provider": "gate", "calls": 0, "tool_calls": 0}
        else:
            outage = self._dependency("model_provider", now)
            if outage:
                result = {"proposal": None, "provider": "replay_fault", "model": getattr(self.provider, "model", None),
                          "error": "provider_http_503", "calls": 1, "tool_calls": 0,
                          "raw_response": {"injected_outage": outage}}
            elif getattr(self.provider, "name", "") == "openai" and self.live_attempts >= self.live_decisions:
                result = {"proposal": None, "provider": "gate", "error": "live_sample_limit_reached", "calls": 0, "tool_calls": 0}
            else:
                result = self.provider.plan(context, tools=self._tool_handler(context, job["trigger"], now),
                    fault=self.pack.model_faults.get(job["trigger"]["event_id"]), deadline_s=self.model_timeout_s)
                if getattr(self.provider, "name", "") == "openai" and result.get("provider") == "openai":
                    self.live_attempts += 1
                    self.store.set_meta("live_attempts", self.live_attempts)
        elapsed = (time.perf_counter() - wall_start) * 1000
        complete = now + timedelta(milliseconds=elapsed)
        self.worker_available[worker], self.viewer_available[uid] = complete, complete
        job.update({"complete_at": iso(complete), "started_at": iso(now), "phase": "complete",
                    "service_ms": elapsed, "queue_wait_ms": (now - ts(job["trigger"]["deliver_at"])).total_seconds() * 1000,
                    "initial_context": context, "result": result, "gate_reason": reason, "store_injection": fault})
        self.store.queue(job)
        self._push(complete, 2, "complete", job)

    def _finish(self, job, now):
        trigger = job["trigger"]
        uid, did = trigger["user_id"], job["decision_id"]
        if self.store.decision(did):
            self.store.remove_pending(did)
            return
        initial = job.get("initial_context")
        result = job["result"]
        check_start = time.perf_counter()
        self._reconcile(uid, now)
        current = self._context(trigger, now) if initial else None
        if current and initial:
            # Preserve scoped retrievals while rechecking their validity against
            # current catalogue/settings. Evidence still uses original known-at.
            known = {c["title_id"] for c in current["candidates"]}
            for candidate in initial["candidates"]:
                if candidate["title_id"] not in known and candidate.get("dialect") == current["facts"].get("dialect") and available(candidate, now, self.withdrawn):
                    current["candidates"].append(candidate)
            for ref, evidence in initial["evidence_index"].items():
                if ref not in current["evidence_index"]:
                    current["evidence_index"][ref] = evidence
        proposal = result.get("proposal") or self._abstain(result.get("error") or job.get("gate_reason") or "no_proposal")
        checks = {"passed": [], "failed": [], "actions": [], "goal": None}
        rejected_checks = []
        fallback_used = None
        repair = None
        if current and not job.get("gate_reason"):
            checks = verify_proposal(self.pack, current, proposal, now, self.store.actions(uid), self.withdrawn, self.pack.arms.get(uid, "unknown"))
            if not result.get("error") and checks["failed"] and set(checks["failed"]) <= {"duplicate_content_slug_on_page", "row_title_count"}:
                rejected_checks = list(checks["failed"])
                smaller = self._smaller_layout(proposal)
                smaller_checks = verify_proposal(self.pack, current, smaller, now, self.store.actions(uid), self.withdrawn, self.pack.arms.get(uid, "unknown"))
                if not smaller_checks["failed"]:
                    proposal, checks = smaller, smaller_checks
                    repair = "Removed duplicate page content and dropped undersized rows; model-selected goal retained."
            if result.get("error") or checks["failed"]:
                rejected_checks = list(checks["failed"])
                # The fallback is local code, not a second hidden model/tool call.
                fallback = self.fallback.plan(current)
                proposal = fallback["proposal"]
                checks = verify_proposal(self.pack, current, proposal, now, self.store.actions(uid), self.withdrawn, self.pack.arms.get(uid, "unknown"))
                fallback_used = result.get("error") or "independent_verifier_rejected_proposal"
        elif job.get("gate_reason"):
            checks["passed"] = ["safe_abstention", job["gate_reason"]]
        calls, tools = int(result.get("calls", 0)), int(result.get("tool_calls", 0)) + (4 if initial else 0)
        if calls > self.max_model_calls or tools > self.max_tool_calls:
            checks["failed"].append("call_budget_exceeded")
        actions = checks.get("actions", []) if not checks["failed"] else []
        validation_ms = (time.perf_counter() - check_start) * 1000
        publication = now + timedelta(milliseconds=validation_ms)
        # If the check crosses a delivery/control boundary, requeue publication
        # so those observations are applied before checks are committed.
        if self.queue and self.queue[0][0] <= publication and self.queue[0][0] > now:
            job["complete_at"] = iso(publication)
            job["service_ms"] += validation_ms
            job["phase"] = "complete"
            self.store.queue(job)
            self._push(publication, 2, "complete", job)
            return
        duration = max(0.0, (publication - ts(trigger["deliver_at"])).total_seconds() * 1000)
        if duration > 60_000:
            actions = []
            checks["failed"].append("decision_deadline_exceeded")
        selected = checks.get("goal") if not checks["failed"] else None
        active = self.store.open_goal(uid)
        goal = active
        if selected:
            gid = selected.get("goal_id") or selected["id"]
            if not active or active["id"] != gid:
                if active:
                    active["status"], active["closed_at"] = "superseded", iso(publication)
                    active["superseded_reason"] = proposal.get("reason_summary")
                    active["superseded_by_decision_id"] = did
                    self.store.put_goal(active)
                goal = {"goal_instance_id": stable_id("G", uid, did, gid), "id": gid, "goal_id": gid,
                        "user_id": uid, "started_at": iso(publication),
                        "deadline": iso(publication + timedelta(hours=selected["success"]["window_hours"])),
                        "success": selected["success"], "version": selected.get("version", self.pack.goal_version),
                        "target_slug": proposal.get("target_slug") or selected.get("target_slug"),
                        "status": "open", "evidence": proposal.get("evidence_refs", []), "action_ids": [], "rule_history": []}
        published = []
        for n, payload in enumerate(actions):
            action = {**payload, "action_id": stable_id("A", did, n), "decision_id": did, "user_id": uid,
                      "trigger_event_id": trigger["event_id"], "published_at": iso(publication),
                      "goal_instance_id": goal["goal_instance_id"] if goal else None}
            if action["type"] in {"push", "whatsapp", "in_app_message"}:
                action["send_at"] = iso(publication)
                group = "outbound" if action["type"] in {"push", "whatsapp"} else "in_app"
                if self.store.message_reserved(uid, ist_day(publication), group):
                    checks["failed"].append("atomic_daily_message_reservation")
                    continue
                self.store.reserve_message(uid, ist_day(publication), group, action["action_id"])
            self.store.put_action(action)
            published.append(action)
            if goal:
                goal.setdefault("action_ids", []).append(action["action_id"])
        if goal:
            if selected:
                goal["plan"] = proposal.get("plan")
                goal["reason_summary"] = proposal.get("reason_summary")
            self.store.put_goal(goal)
        no_action = job.get("gate_reason") or proposal.get("no_action_reason") or result.get("error")
        if not published and not no_action:
            no_action = ";".join(checks["failed"]) or "valid_goal_without_intervention"
        source_refs = list(proposal.get("evidence_refs") or [f"event:{trigger['event_id']}"])
        gate_evidence = {f"event:{trigger['event_id']}": {"source": "trigger_stream", "known_at": trigger["deliver_at"],
            "received_at": trigger["received_at"], "estimated_event_at": iso(event_time(trigger)),
            "buffered": event_is_buffered(trigger)}}
        if job.get("gate_reason") == "holdout":
            ref = f"assignment:{uid}"
            source_refs.append(ref)
            gate_evidence[ref] = {"source": "replay/arms.csv", "user_id": uid, "arm": "holdout",
                                  "known_at": iso(self.pack.replay_start)}
        decision = {"decision_id": did, "user_id": uid, "viewer_id": uid, "trigger_event_id": trigger["event_id"],
            "trigger": {"event": trigger["event"], "received_at": trigger["received_at"], "deliver_at": trigger["deliver_at"],
                        "event_age_s": max(0, (ts(trigger["received_at"]) - event_time(trigger)).total_seconds()), "event_id": trigger["event_id"]},
            "goal": copy.deepcopy(goal), "action": [a["type"] for a in published] or "NO_ACTION",
            "action_ids": [a["action_id"] for a in published], "dialect": (current or {}).get("facts", {}).get("provenance", {}).get("dialect"),
            "confidence": proposal.get("confidence"), "evidence": source_refs, "gate_evidence": gate_evidence,
            "assumptions": proposal.get("assumptions", []), "uncertainties": proposal.get("uncertainties", []),
            "proposal": proposal, "provider_result": result, "fallback_reason": fallback_used, "repair": repair,
            "checks": {"passed": checks["passed"], "failed": checks["failed"], "rejected_proposal_checks": rejected_checks},
            "context": initial, "validation_context": current if initial and current and initial != current else None,
            "validation_state_revision": (current or {}).get("state_revision"),
            "state_changed_during_inference": bool(initial and current and initial["state_revision"] != current["state_revision"]),
            "published_at": iso(publication), "decided_in_ms": round(duration, 3),
            "service_ms": round(job.get("service_ms", 0) + validation_ms, 3), "queue_wait_ms": round(job.get("queue_wait_ms", 0), 3),
            "model_calls": calls, "tool_calls": tools, "no_action_reason": None if published else no_action,
            "status": "PUBLISHED" if published else "NO_ACTION", "store_injection": job.get("store_injection"),
            "fixture": self.pack.model_faults.get(trigger["event_id"])}
        original = job.get("original_decision_id")
        if original and (self.store.decision(original) or self.store.db.execute("SELECT 1 FROM pending WHERE decision_id=?", (original,)).fetchone()):
            decision["duplicate_of_decision_id"] = original
        self.store.put_decision(decision)
        state = self.store.viewer(uid)
        state.update({"user_id": uid, "active_goal": goal if goal and goal["status"] == "open" else None,
                      "last_decision_id": did, "active_plan": proposal.get("plan") if selected else state.get("active_plan"),
                      "facts": (current or {}).get("facts", state.get("facts", {}))})
        if published:
            state.pop("plan_invalidated_by", None)
        self.store.put_viewer(uid, state)
        self.store.remove_pending(did)
        if initial:
            # A same-viewer job must not observe a commitment whose recorded
            # publication is still in its future because verification took time.
            worker = job["worker"]
            self.worker_available[worker] = max(self.worker_available[worker], publication)
            self.viewer_available[uid] = max(self.viewer_available[uid], publication)

    def process_event(self, event, *, before_publish=None, publication_guard=None, clock=None):
        """Process one HTTP event on its owning worker, using actual wall time.

        The adapter owns serialization and receipt timestamps. A publication
        refresh can ingest purchases/opt-outs received while inference ran.
        Pending jobs retain the same durable recovery path as replay.
        """
        clock = clock or (lambda: datetime.now(UTC))
        did = stable_id("D", event["event_id"])
        if self.store.decision(did):
            return self.store.decision(did)
        if event["event"] not in {"app_open", "login_success"}:
            with self.store.transaction():
                self._ingest(event, "api")
                self._reconcile(event["user_id"], clock())
            return None
        if not any(j["trigger"]["event_id"] == event["event_id"] for j in self.store.pending()):
            with self.store.transaction():
                self._trigger(event, clock(), "api:" + event["event_id"])
        while self.queue:
            _, _, _, kind, job = heapq.heappop(self.queue)
            now = clock()
            # The adapter briefly serializes inbox acceptance with refresh and
            # commit, closing the final verification-to-publication race. It
            # never holds this guard across external inference.
            guard = publication_guard if kind == "complete" and publication_guard is not None else nullcontext()
            with guard:
                with self.store.transaction():
                    if kind == "start":
                        self._start(job, now)
                    elif kind == "complete":
                        if before_publish:
                            before_publish(self, clock())
                        self._finish(job, clock())
        return self.store.decision(did) or self.store.decision(stable_id("D-duplicate", "api:" + event["event_id"]))

    def run(self, *, progress=None):
        if self.store.get_meta("replay_complete"):
            return
        for kind, rows, priority in [("control", self.pack.control_events, 0), ("live", self.pack.live_events, 1), ("trigger", self.pack.triggers, 3)]:
            for index, row in enumerate(rows):
                key = f"{kind}:{index}"
                if not self.store.delivery_seen(key):
                    at = ts(row["at"] if kind == "control" else row["deliver_at"])
                    self._push(at, priority, kind, {"delivery_key": key, "record": row})
        count = 0
        while self.queue:
            now, _, _, kind, data = heapq.heappop(self.queue)
            with self.store.transaction():
                if kind == "control":
                    self._control(data["record"], now)
                elif kind == "live":
                    self._ingest(data["record"], "live")
                    self._reconcile(data["record"]["user_id"], now)
                elif kind == "trigger":
                    self._trigger(data["record"], now, data["delivery_key"])
                elif kind == "start":
                    self._start(data, now)
                elif kind == "complete":
                    self._finish(data, now)
                if "delivery_key" in data:
                    self.store.mark_delivery(data["delivery_key"], {"at": iso(now), "kind": kind})
                self.store.set_meta("last_logical_time", iso(now))
            count += 1
            if progress and count % 500 == 0:
                progress({"processed_queue_items": count, "decisions": self.store.count("decisions"), "logical_time": iso(now)})
        with self.store.transaction():
            self.store.set_meta("replay_complete", True)
