"""Integration tests of the real event scheduler, ledger and publication checks.

All providers in this file are local. Real sleeps measure short service times so
intervening controls/events genuinely occur before logical publication; no API
request or fabricated provider latency is used.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import time

import pytest

from ott_agent.engine import Engine
from ott_agent.providers import ReplayProvider
from ott_agent.store import Store
from ott_agent.util import ts

from test_domain import NOW, event, iso, pack as domain_pack, proposal


def mini_pack(triggers=None, live=None, controls=None, users=None):
    p = domain_pack()
    triggers = triggers if triggers is not None else [event()]
    users = users or {e["user_id"]: "agent" for e in triggers}
    original_profile = p.profiles["U-test"]
    p.profiles = {uid: {**deepcopy(original_profile), "user_id": uid} for uid in users}
    p.history = []
    p.triggers = triggers
    p.live_events = live or []
    p.control_events = controls or []
    p.model_faults = {}
    p.arms = users
    p.goal_version = "fixture-v1"
    boundaries = [ts(e["deliver_at"]) for e in p.triggers + p.live_events]
    boundaries.extend(ts(c["at"]) for c in p.control_events)
    p.replay_start = min(boundaries)
    p.popularity_cutoff = p.replay_start - timedelta(seconds=1)
    return p


class CountingProvider:
    name = "test-replay"
    model = "local-test-policy"

    def __init__(self, delay=0.0, selected_goal=None):
        self.calls = 0
        self.delay = delay
        self.selected_goal = selected_goal
        self.contexts = []

    def plan(self, context, **kwargs):
        self.calls += 1
        self.contexts.append(deepcopy(context))
        if self.delay:
            time.sleep(self.delay)
        result = ReplayProvider().plan(context)
        if self.selected_goal:
            active = context.get("active_goal") or {}
            gid = self.selected_goal
            transition = "keep" if (active.get("goal_id") or active.get("id")) == gid else "commit"
            chosen = proposal(context, result["proposal"]["actions"], gid=gid, transition=transition)
            chosen["evidence_refs"] = [f"goal_rule:{gid}"]
            result.update(proposal=chosen, raw_response=deepcopy(chosen))
        result.update(provider=self.name, model=self.model)
        return result


def run(tmp_path, p, provider=None, **options):
    store = Store(tmp_path / "ledger.sqlite3")
    engine = Engine(p, store, provider, **options)
    engine.run()
    return store, engine


def test_duplicate_delivery_is_idempotent_and_durable_reopen_preserves_results(tmp_path):
    original = event("same-event")
    duplicate = {**original, "received_at": iso(NOW + timedelta(minutes=1)), "deliver_at": iso(NOW + timedelta(minutes=1))}
    p = mini_pack(triggers=[original, duplicate])
    provider = CountingProvider()
    store, _ = run(tmp_path, p, provider)
    decisions, actions = store.decisions(), store.actions()
    assert provider.calls == 1
    assert len(decisions) == 2
    assert len(actions) == 1
    assert sum(d["no_action_reason"] == "duplicate_trigger" for d in decisions) == 1
    duplicate_decision = next(d for d in decisions if d["no_action_reason"] == "duplicate_trigger")
    assert duplicate_decision["duplicate_of_decision_id"] == next(d["decision_id"] for d in decisions if d["no_action_reason"] != "duplicate_trigger")
    assert len({e["event_id"] for e in store.all_events()}) == 1
    active = store.open_goal("U-test")
    assert active is not None
    snapshot = (deepcopy(decisions), deepcopy(actions), deepcopy(active), deepcopy(store.viewer("U-test")))
    store.close()

    reopened = Store(tmp_path / "ledger.sqlite3")
    unused_provider = CountingProvider()
    Engine(p, reopened, unused_provider).run()
    assert unused_provider.calls == 0
    assert (reopened.decisions(), reopened.actions(), reopened.open_goal("U-test"), reopened.viewer("U-test")) == snapshot
    assert reopened.pending() == []
    reopened.close()


def test_pending_work_resumes_after_process_loss_without_republishing(tmp_path):
    class ProcessLoss(BaseException):
        pass

    class CrashingProvider(CountingProvider):
        def plan(self, context, **kwargs):
            raise ProcessLoss("Simulate process loss before a provider result is committed")

    p = mini_pack()
    store = Store(tmp_path / "ledger.sqlite3")
    with pytest.raises(ProcessLoss):
        Engine(p, store, CrashingProvider()).run()
    assert store.delivery_seen("trigger:0")
    assert len(store.pending()) == 1
    assert store.count("actions") == store.count("decisions") == 0
    store.close()

    reopened = Store(tmp_path / "ledger.sqlite3")
    provider = CountingProvider()
    Engine(p, reopened, provider).run()
    assert provider.calls == 1
    assert reopened.count("actions") == reopened.count("decisions") == 1
    assert not reopened.pending()
    assert reopened.get_meta("replay_complete") is True
    reopened.close()


def test_buffered_trigger_updates_evidence_without_model_or_live_action(tmp_path):
    buffered = event("offline-open", buffered=86400 * 3)
    p = mini_pack(triggers=[buffered])
    provider = CountingProvider()
    store, _ = run(tmp_path, p, provider)
    decision = store.decisions()[0]
    assert provider.calls == decision["model_calls"] == 0
    assert decision["no_action_reason"] == "buffered_trigger_not_live_visit"
    assert decision["status"] == "NO_ACTION"
    assert not store.actions()
    assert any(e["event_id"] == "offline-open" for e in store.all_events())
    store.close()


def test_holdout_never_calls_model_even_when_matching_a_timeout_fixture(tmp_path):
    trigger = event("holdout-trigger")
    p = mini_pack(triggers=[trigger], users={"U-test": "holdout"})
    p.model_faults = {trigger["event_id"]: {"fault": "timeout", "seconds": 45}}
    provider = CountingProvider()
    store, _ = run(tmp_path, p, provider, model_timeout_s=0.001)
    decision = store.decisions()[0]
    assert provider.calls == decision["model_calls"] == 0
    assert decision["no_action_reason"] == "holdout"
    assert "assignment:U-test" in decision["evidence"]
    assert decision["gate_evidence"]["assignment:U-test"]["arm"] == "holdout"
    assert decision["fixture"]["fault"] == "timeout"
    assert store.actions() == []
    store.close()


def test_late_historical_purchase_invalidates_first_paid_entry_instead_of_succeeding(tmp_path):
    trigger = event("before-buffered-billing")
    late = event("historical-paid", "purchase_completed", NOW + timedelta(minutes=2),
                 props={"is_trial": False, "plan_id": "P_MONTH"}, buffered=4 * 86400)
    p = mini_pack(triggers=[trigger], live=[late])
    store, _ = run(tmp_path, p, CountingProvider(selected_goal="G05"))
    goals = store.goals()
    assert len(goals) == 1
    assert goals[0]["id"] == "G05"
    assert goals[0]["status"] == "invalidated"
    assert goals[0]["judgment"]["status"] == "inconclusive"
    assert "event:historical-paid" in goals[0]["invalidation_evidence"]
    assert store.open_goal("U-test") is None
    assert store.viewer("U-test")["active_plan"] is None
    store.close()


def test_series_tool_returns_relevant_bounded_progress_not_entire_inventory(tmp_path):
    p = mini_pack()
    slug = p.catalogue["T01"]["content_slug"]
    p.episodes[slug] = [{"episode_id": f"{slug}-e{i}", "season": "1", "episode": str(i), "minutes": "10"} for i in range(1, 201)]
    store = Store(tmp_path / "tools.sqlite3")
    engine = Engine(p, store)
    context = engine._context(p.triggers[0], NOW)
    result = engine._tool_handler(context, p.triggers[0], NOW)("read_series_progress", {"content_slug": slug})
    assert result["episode_count"] == result["remaining_count"] == 200
    assert len(result["next_episodes"]) == 5
    assert result["bounded"] is True
    assert result["next_episodes"][0]["episode_id"] == f"{slug}-e1"
    store.close()


def test_layout_repair_keeps_model_goal_instead_of_replacing_it_with_policy_goal(tmp_path):
    class DuplicateLayout(CountingProvider):
        def plan(self, context, **kwargs):
            result = super().plan(context, **kwargs)
            action = result["proposal"]["actions"][0]
            action["rows"][0]["title_ids"].append(action["hero_title_id"])
            return result

    p = mini_pack()
    store, _ = run(tmp_path, p, DuplicateLayout(selected_goal="G05"))
    decision = store.decisions()[0]
    assert decision["goal"]["id"] == "G05"
    assert decision["repair"]
    assert decision["fallback_reason"] is None
    assert "duplicate_content_slug_on_page" in decision["checks"]["rejected_proposal_checks"]
    assert decision["checks"]["failed"] == []
    assert store.actions()
    store.close()


def test_same_viewer_waits_until_commitment_publication_not_just_provider_completion(tmp_path, monkeypatch):
    import ott_agent.engine as module
    original_verify = module.verify_proposal

    def slow_verify(*args, **kwargs):
        time.sleep(.006)
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(module, "verify_proposal", slow_verify)
    first = event("first-concurrent")
    second = event("second-concurrent", at=NOW + timedelta(milliseconds=1))
    p = mini_pack(triggers=[first, second])
    store, _ = run(tmp_path, p, CountingProvider(delay=.02), workers=1)
    decisions = store.decisions()
    later = next(d for d in decisions if d["trigger_event_id"] == second["event_id"])
    context = later["context"]
    assert ts(context["active_goal"]["started_at"]) <= ts(context["as_of"])
    assert later["queue_wait_ms"] >= 20
    store.close()


def test_existing_plan_is_in_next_decision_context_not_only_stored_in_database(tmp_path):
    first = event("first-plan")
    later = event("later-plan", at=NOW + timedelta(hours=3))
    p = mini_pack(triggers=[first, later])
    provider = CountingProvider()
    store, _ = run(tmp_path, p, provider)
    assert len(provider.contexts) == 2
    first_plan = store.decisions()[0]["proposal"]["plan"]
    assert provider.contexts[1]["active_goal"]["plan"] == first_plan
    assert provider.contexts[1]["active_goal"]["reason_summary"]
    assert len(store.goals()) == 1
    store.close()


def test_past_paid_plan_does_not_invalidate_a_legitimate_repeat_trial(tmp_path):
    trigger = event("repeat-trial-trigger")
    old_paid = event("old-paid-before-trial", "purchase_completed", NOW + timedelta(minutes=2),
                     props={"is_trial": False, "plan_id": "P_MONTH"}, buffered=4 * 86400)
    p = mini_pack(triggers=[trigger], live=[old_paid])
    p.profiles["U-test"].update(subscription_status="trial_active", plan_started_at=iso(NOW - timedelta(hours=6)),
       plan_ends_at=iso(NOW + timedelta(hours=66)), updated_at=iso(NOW - timedelta(hours=2)), snapshot_at=iso(NOW - timedelta(hours=1)))
    store, _ = run(tmp_path, p, CountingProvider(selected_goal="G04"))
    assert store.open_goal("U-test")["id"] == "G04"
    assert store.goals()[0]["status"] == "open"
    store.close()


def test_corrected_rule_arriving_during_inference_is_applied_before_publication(tmp_path):
    correction_at = NOW + timedelta(seconds=0.005)
    correction = {"type": "goal_rule_corrected", "at": iso(correction_at), "goal_id": "G03",
                  "new_success": {"event": "play_started", "condition": "later IST calendar day"},
                  "applies_to": "every G03 goal that is still open"}
    p = mini_pack(controls=[correction])
    p.goals["G03"]["success"]["condition"] = "on a later IST calendar day"
    store = Store(tmp_path / "ledger.sqlite3")
    started = NOW - timedelta(seconds=10)
    deadline = started + timedelta(hours=48)
    active = {"goal_instance_id": "existing-G03", "user_id": "U-test", "goal_id": "G03", "id": "G03",
              "started_at": iso(started), "deadline": iso(deadline), "status": "open", "target_slug": None,
              "success": deepcopy(p.goals["G03"]["success"]), "version": "G03@original",
              "action_ids": [], "evidence_refs": ["goal_rule:G03"]}
    with store.transaction():
        store.put_goal(active)
    provider = CountingProvider(delay=0.02, selected_goal="G03")
    Engine(p, store, provider).run()
    decision = store.decisions()[0]
    goal = store.open_goal("U-test")
    assert provider.calls == 1
    assert provider.contexts[0]["active_goal"]["success"]["event"] == "app_open"
    assert ts(decision["published_at"]) > correction_at
    assert decision["state_changed_during_inference"] is True
    assert goal["success"]["event"] == "play_started"
    assert goal["version"] == f"G03@{correction['at']}"
    assert goal["goal_instance_id"] == "existing-G03"
    assert goal["started_at"] == iso(started)
    assert goal["deadline"] == iso(deadline)
    assert goal["rule_history"][0]["success"]["event"] == "app_open"
    assert store.controls()[0]["affected_goal_instances"] == ["existing-G03"]
    store.close()


def test_purchase_arriving_during_inference_invalidates_proposed_trial_goal(tmp_path):
    purchase_at = NOW + timedelta(seconds=0.005)
    paid = event("organic-paid", "purchase_completed", at=purchase_at,
                 props={"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})
    p = mini_pack(live=[paid])
    p.profiles["U-test"].update(subscription_status="trial_active", plan_started_at=iso(NOW - timedelta(days=1)), plan_ends_at=iso(NOW + timedelta(days=2)))
    provider = CountingProvider(delay=0.02, selected_goal="G04")
    store, _ = run(tmp_path, p, provider)
    decision = store.decisions()[0]
    assert provider.contexts[0]["facts"]["subscription_status"] == "trial_active"
    assert decision["provider_result"]["proposal"]["goal_id"] == "G04"
    assert decision["validation_context"]["facts"]["subscription_status"] == "active"
    assert decision["state_changed_during_inference"] is True
    assert "goal_not_proven_eligible" in decision["checks"]["rejected_proposal_checks"]
    assert decision["fallback_reason"] == "independent_verifier_rejected_proposal"
    assert all(g["goal_id"] != "G04" for g in store.goals())
    assert decision["goal"]["goal_id"] != "G04"
    assert all(a["goal_instance_id"] == decision["goal"]["goal_instance_id"] for a in store.actions())
    assert ts(decision["published_at"]) > purchase_at
    store.close()


def test_injected_store_timeout_abstains_without_provider_call(tmp_path):
    control = {"type": "dependency_degraded", "dependency": "user_data_store",
               "at": iso(NOW - timedelta(seconds=1)), "until": iso(NOW + timedelta(minutes=1)),
               "detail": "p95 read latency 8 s and 20% of reads time out"}
    # Exercise the real deterministic failure branch, rather than monkeypatching
    # the store reader or bypassing its control-window handling.
    eid = next(f"store-read-{i}" for i in range(1000)
               if int(hashlib.sha256((control["at"] + f"store-read-{i}").encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < 0.20)
    p = mini_pack(triggers=[event(eid)], controls=[control])
    provider = CountingProvider()
    store, _ = run(tmp_path, p, provider, store_timeout_s=0.001)
    decision = store.decisions()[0]
    assert provider.calls == decision["model_calls"] == 0
    assert decision["no_action_reason"] == "user_data_store_timeout"
    assert decision["store_injection"]["injected"] == "20_percent_timeout"
    assert not store.actions()
    store.close()


def test_simultaneous_viewers_use_worker_pool_instead_of_one_serial_queue(tmp_path):
    triggers = [{**event(f"trigger-{i}"), "user_id": f"U-{i}"} for i in range(4)]
    users = {t["user_id"]: "agent" for t in triggers}
    p = mini_pack(triggers=triggers, users=users)
    serial_store, _ = run(tmp_path / "serial", p, CountingProvider(delay=0.02), workers=1)
    pooled_store, _ = run(tmp_path / "pooled", p, CountingProvider(delay=0.02), workers=4)
    serial = serial_store.decisions()
    pooled = pooled_store.decisions()
    assert len(serial) == len(pooled) == 4
    assert max(d["queue_wait_ms"] for d in serial) >= 45
    assert max(d["queue_wait_ms"] for d in pooled) < 5
    assert len({d["context"]["as_of"] for d in serial}) == 4
    assert len({d["context"]["as_of"] for d in pooled}) == 1
    assert all(d["model_calls"] == 1 for d in pooled)
    serial_store.close()
    pooled_store.close()
