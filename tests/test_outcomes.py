from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ott_agent.outcomes import canonical_events, judge_goal
from ott_agent.reports import _latency_report, build_reports, common_outcomes

UTC = timezone.utc
START = datetime(2026, 10, 1, 6, tzinfo=UTC)


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def event(name, when, *, user="U-1", event_id=None, session="session-1", props=None, received=None, buffering=0):
    received = received or when
    # For buffered fixtures, both phone timestamps share their clock offset, and
    # their delta expresses buffering. Server receipt remains knowledge time.
    occurred = received - timedelta(seconds=buffering)
    return {
        "event_id": event_id or f"{user}-{name}-{stamp(when)}",
        "user_id": user, "event": name, "session_id": session,
        "occurred_at": stamp(occurred), "sent_at": stamp(received),
        "received_at": stamp(received), "properties": props or {},
    }


def goal(goal_id="G02", **overrides):
    config = {
        "G01": ("play_started", 24), "G02": ("episode_completed", 72),
        "G03": ("app_open", 48), "G04": ("purchase_completed", 168),
        "G05": ("purchase_completed", 168), "G06": ("purchase_completed", 336),
        "G07": ("play_started", 72), "G08": ("play_started", 168),
        "G09": ("play_started", 336), "G10": (None, 168),
        "G11": ("tv_app_open", 336), "G12": ("play_started", 168),
    }
    signal, hours = config[goal_id]
    row = {
        "goal_instance_id": "goal-1", "id": goal_id, "user_id": "U-1",
        "started_at": stamp(START), "deadline": stamp(START + timedelta(hours=hours)),
        "success": {"event": signal, "window_hours": hours}, "version": "v1",
        "status": "open", "action_ids": [],
    }
    row.update(overrides)
    return row


@pytest.fixture
def pack():
    return SimpleNamespace(
        catalogue={
            "T1": {"content_slug": "series", "release_date": "2026-09-20"},
            "T1-en": {"content_slug": "series", "release_date": "2026-09-20"},
            "T2": {"content_slug": "other", "release_date": "2026-10-02"},
            "T3": {"content_slug": "old", "release_date": "2026-07-01"},
        },
        goals={}, arms={"U-1": "agent", "U-2": "agent", "U-3": "holdout", "U-4": "holdout"},
        pilot_outcomes=[
            {"arm": "treatment", "succeeded": "false", "served": "false"},
            {"arm": "treatment", "succeeded": "true", "served": "true"},
            {"arm": "holdout", "succeeded": "true", "served": "false"},
            {"arm": "holdout", "succeeded": "true", "served": "false"},
        ],
    )


def test_duplicate_delivery_and_reconstructed_open_keep_first_receipt():
    original = event("app_open", START, event_id="original")
    duplicate = {**original, "received_at": stamp(START + timedelta(seconds=10))}
    reconstructed = event("app_open", START + timedelta(seconds=12), event_id="sim-open")
    play = event("play_started", START + timedelta(seconds=30))
    rows = canonical_events([reconstructed, duplicate, play, original])
    assert [e["event_id"] for e in rows] == ["original", play["event_id"]]
    assert rows[0]["received_at"] == stamp(START)


def test_corrected_rule_uses_play_on_later_ist_day(pack):
    opened = event("app_open", START + timedelta(days=1))
    original = judge_goal(goal("G03"), [opened], pack, START + timedelta(days=3))
    assert original["status"] == "succeeded"
    corrected = goal("G03", success={"event": "play_started", "window_hours": 48}, version="corrected")
    result = judge_goal(corrected, [opened], pack, START + timedelta(days=3))
    assert result["status"] == "failed"
    assert result["rule_version"] == "corrected"
    played = event("play_started", START + timedelta(days=1, minutes=1))
    assert judge_goal(corrected, [opened, played], pack, START + timedelta(days=3))["status"] == "succeeded"


def test_buffered_purchase_received_after_goal_is_not_new_conversion(pack):
    purchase = event(
        "purchase_completed", START + timedelta(minutes=2), buffering=86400,
        props={"is_trial": False, "plan_id": "P_MONTH", "price_inr": 149},
    )
    result = judge_goal(goal("G05"), [purchase], pack, START + timedelta(days=8))
    assert result["status"] == "inconclusive"
    assert result["entry_condition_invalidated"] is True
    assert result["evidence"] == ["event:" + purchase["event_id"]]


def test_late_historical_evidence_can_invalidate_an_already_closed_first_paid_claim(pack):
    paid = event("purchase_completed", START + timedelta(hours=1), props={"is_trial": False})
    older = event("purchase_completed", START + timedelta(days=2), buffering=3 * 86400, props={"is_trial": False})
    result = judge_goal(goal("G05", status="succeeded"), [paid, older], pack, START + timedelta(days=8))
    assert result["status"] == "inconclusive"
    assert result["entry_condition_invalidated"]
    assert result["evidence"] == ["event:" + older["event_id"]]


def test_late_delivery_within_goal_window_can_correct_an_earlier_failure(pack):
    played = event("play_started", START + timedelta(days=2), buffering=47 * 3600)
    result = judge_goal(goal("G01", status="failed"), [played], pack, START + timedelta(days=3))
    assert result["status"] == "succeeded"
    assert result["success_at"] == stamp(START + timedelta(hours=1)).replace("+00:00", "Z").replace("Z", ".000Z")


def test_trial_purchase_does_not_satisfy_paid_goal(pack):
    trial = event("purchase_completed", START + timedelta(hours=2), props={"is_trial": True})
    assert judge_goal(goal("G04"), [trial], pack, START + timedelta(days=8))["status"] == "failed"


def test_goal_window_is_strictly_after_commitment(pack):
    completion = event("episode_completed", START)
    result = judge_goal(goal(), [completion], pack, START + timedelta(days=4))
    assert result["status"] == "failed"


def test_other_users_success_does_not_satisfy_viewer_goal(pack):
    completion = event("episode_completed", START + timedelta(hours=1), user="U-2")
    assert judge_goal(goal(), [completion], pack, START + timedelta(days=4))["status"] == "failed"


def test_resume_series_joins_language_variant_by_slug(pack):
    played = event("play_started", START + timedelta(hours=1), props={"title_id": "T1-en"})
    assert judge_goal(goal("G07", target_slug="series"), [played], pack, START + timedelta(days=4))["status"] == "succeeded"
    assert judge_goal(goal("G07", target_slug="other"), [played], pack, START + timedelta(days=4))["status"] == "failed"


def test_recent_release_window_is_anchored_to_goal_start(pack):
    future_release = event("play_started", START + timedelta(days=3), props={"title_id": "T2"})
    old_release = event("play_started", START + timedelta(days=2), props={"title_id": "T3"}, event_id="old")
    recent = event("play_started", START + timedelta(days=1), props={"title_id": "T1"}, event_id="recent")
    assert judge_goal(goal("G08"), [future_release, old_release], pack, START + timedelta(days=8))["status"] == "failed"
    assert judge_goal(goal("G08"), [recent], pack, START + timedelta(days=8))["status"] == "succeeded"


def test_habit_requires_three_distinct_ist_days(pack):
    first = START.replace(hour=18, minute=0)  # 23:30 IST, 1 October.
    second = first + timedelta(hours=1)  # 00:30 IST, 2 October.
    events = [event("play_started", first), event("play_started", second), event("play_started", second + timedelta(hours=1))]
    assert judge_goal(goal("G09"), events, pack, START + timedelta(days=15))["status"] == "failed"
    events.append(event("play_started", second + timedelta(days=1)))
    result = judge_goal(goal("G09"), events, pack, START + timedelta(days=15))
    assert result["status"] == "succeeded"
    assert result["distinct_ist_play_days"] == ["2026-10-01", "2026-10-02", "2026-10-03"]


def test_incomplete_window_and_unsupported_goal_are_inconclusive(pack):
    assert judge_goal(goal(), [], pack, START + timedelta(hours=1))["status"] == "inconclusive"
    assert judge_goal(goal("G11"), [], pack, START + timedelta(days=20))["status"] == "inconclusive"
    assert judge_goal(goal(status="superseded"), [], pack, START + timedelta(days=20))["status"] == "inconclusive"


def test_exposure_link_is_descriptive_and_delivery_alone_is_not_exposure(pack):
    exposure = event("home_page_shown", START + timedelta(seconds=10), props={"personalised": True, "page_action_id": "A1"})
    success = event("episode_completed", START + timedelta(minutes=10))
    g = goal(action_ids=["A1"])
    result = judge_goal(g, [exposure, success], pack, START + timedelta(days=4))
    assert result["attribution"]["classification"] == "exposure_linked"
    assert result["attribution"]["causal_claim"] is False
    delivered = event("message_delivered", START + timedelta(seconds=5), props={"action_id": "A1"})
    result = judge_goal(g, [delivered, success], pack, START + timedelta(days=4))
    assert result["attribution"]["classification"] == "success_without_observed_action_link"


def baseline():
    return {
        f"U-{i}": {"anchor_at": stamp(START), "arm": "agent" if i <= 2 else "holdout", "paid_at_baseline": False}
        for i in range(1, 5)
    }


def test_itt_uses_all_assigned_users_not_served_subset():
    events = [event("play_started", START + timedelta(hours=1), user="U-1"), event("play_started", START + timedelta(hours=1), user="U-3")]
    result = common_outcomes(baseline(), events, START + timedelta(days=15))
    metric = result["metrics"]["any_play_7d"]
    assert metric["arms"]["agent"]["users"] == 2
    assert metric["arms"]["holdout"]["users"] == 2
    assert metric["difference"] == 0


def test_unknown_followup_keeps_denominator_and_reports_bounds():
    result = common_outcomes(baseline(), [], START + timedelta(days=8))
    metric = result["metrics"]["three_play_days_14d"]
    assert metric["arms"]["agent"] == {
        "users": 2, "successes": 0, "unknown": 2, "observed_failures": 0,
        "rate": None, "rate_bounds": [0, 1],
    }
    assert metric["difference_ci95"] is None
    assert metric["difference_bounds"] == [-1, 1]
    assert result["metrics"]["any_play_7d"]["arms"]["agent"]["rate"] == 0


def test_paid_conversion_has_fixed_supported_never_paid_baseline():
    rows = baseline()
    rows["U-1"]["paid_at_baseline"] = True
    rows["U-2"]["paid_at_baseline"] = None
    result = common_outcomes(rows, [], START + timedelta(days=15))
    assert result["metrics"]["first_paid_purchase_7d"]["arms"]["agent"]["users"] == 0
    assert result["metrics"]["any_play_7d"]["arms"]["agent"]["users"] == 2


def test_reports_write_auditable_artifacts_and_keep_pilot_separate(pack, tmp_path):
    result = build_reports(
        pack, tmp_path,
        [{"status": "PUBLISHED", "decided_in_ms": 120, "model_calls": 1, "tool_calls": 2}],
        [{"action_id": "A1", "user_id": "U-1", "type": "home_page"}],
        [{"id": "G02", "status": "succeeded"}], [], baseline(),
        {"provider": "replay", "coverage_end": stamp(START + timedelta(days=15)), "replay_complete": True},
    )
    assert result["impact"]["pilot"]["difference"] == -.5
    assert result["latency"]["milliseconds"]["p95"] == 120
    for name in ["impact_report.json", "latency_report.json", "report_summary.json"]:
        assert json.loads((tmp_path / name).read_text())
    for name in ["impact_report.md", "final_report.md"]:
        assert "#" in (tmp_path / name).read_text()
    assert "selection biased" in (tmp_path / "impact_report.md").read_text()


def test_report_never_infers_coverage_from_last_observed_event(pack, tmp_path):
    with pytest.raises(ValueError, match="coverage_end"):
        build_reports(pack, tmp_path, [], [], [], [], baseline(), {})


def test_latency_unique_count_excludes_runtime_duplicate_gate():
    rows = [
        {"status": "PUBLISHED", "decided_in_ms": 10},
        {"status": "NO_ACTION", "no_action_reason": "duplicate_trigger", "decided_in_ms": 0},
    ]
    report = _latency_report(rows, {"provider": "replay"})
    assert report["decision_deliveries"] == 2
    assert report["unique_decisions"] == 1


def test_live_latency_is_separate_from_fast_gates_and_mock_decisions():
    rows = [{"decided_in_ms": .1, "provider_result": {"provider": "gate"}} for _ in range(100)]
    rows.append({"decided_in_ms": 9000, "provider_result": {"provider": "openai", "calls": 2, "tool_calls": 1}})
    report = _latency_report(rows, {"provider": "openai"})
    assert report["milliseconds"]["p50"] == .1
    assert report["live_openai"]["milliseconds"]["p50"] == 9000
    assert report["live_openai"]["api_requests"] == 2
    assert report["live_openai"]["read_only_tool_calls"] == 1


def test_mixed_provider_report_cannot_present_sample_as_full_openai_policy(pack, tmp_path):
    result = build_reports(pack, tmp_path, [], [], [], [], baseline(), {
        "provider": "openai", "live_sample_limit": 12, "coverage_end": stamp(START + timedelta(days=15)),
        "replay_complete": True, "live_mode_note": "Sampled OpenAI plus deterministic fallback",
    })
    assert result["impact"]["live_provider_sampled"] is True
    assert "Mixed provider policy" in (tmp_path / "impact_report.md").read_text()
