"""Goal achievement from observable events, separate from causal attribution.

The supplied simulator repeats live events and reconstructs app opens with new IDs.
Both delivery-ID and user/session deduplication are therefore necessary. Phone time
is never used as the knowledge cutoff; a buffered historical purchase cannot become
a conversion just because it was received after a goal commitment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .util import event_time, iso, ist_day, ts

IST = timezone(timedelta(hours=5, minutes=30))


def _received(event: dict[str, Any]) -> datetime:
    return ts(event.get("received_at") or event.get("deliver_at") or event["occurred_at"])


def canonical_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep first server receipt and dedupe reconstructed app-open sessions.

    Do not dedupe all events by session: several genuine plays or completions may
    belong to a session. Missing IDs get a payload-derived key rather than all
    collapsing into one absent-ID bucket.
    """
    ordered = sorted(events, key=lambda e: (_received(e), str(e.get("event_id", ""))))
    seen_ids: set[Any] = set()
    seen_opens: set[tuple[Any, Any]] = set()
    result = []
    for event in ordered:
        event_id = event.get("event_id")
        key = event_id or (
            event.get("user_id"), event.get("event"), event.get("session_id"),
            event.get("received_at"), repr(sorted((event.get("properties") or {}).items())),
        )
        if key in seen_ids:
            continue
        seen_ids.add(key)
        if event.get("event") == "app_open" and event.get("session_id"):
            session_key = (event.get("user_id"), event["session_id"])
            if session_key in seen_opens:
                continue
            seen_opens.add(session_key)
        result.append(event)
    return result


def _clock_uncertain(event: dict[str, Any]) -> bool:
    """A negative phone send/occurrence delta cannot locate the activity safely."""
    if event.get("time_quality") in {"unknown", "uncertain", "invalid"}:
        return True
    try:
        return ts(event["sent_at"]) < ts(event["occurred_at"])
    except (KeyError, TypeError, ValueError):
        return not bool(event.get("received_at"))


def _paid_purchase(event: dict[str, Any]) -> bool:
    props = event.get("properties") or {}
    trial = props.get("is_trial")
    if isinstance(trial, str):
        trial = trial.lower() == "true"
    # The documented paid success requires an explicit non-trial purchase.
    return event.get("event") == "purchase_completed" and trial is False


def _catalogue(pack: Any) -> dict[str, dict[str, Any]]:
    return getattr(pack, "catalogue", {})


def _goal_config(pack: Any, goal_id: str) -> dict[str, Any]:
    goals = getattr(pack, "goals", {})
    if isinstance(goals, dict):
        return goals.get(goal_id, {})
    return next((g for g in goals if g.get("goal_id") == goal_id), {})


def _matches(goal: dict[str, Any], event: dict[str, Any], pack: Any, start: datetime) -> bool:
    goal_id = goal.get("id") or goal.get("goal_id")
    success = goal.get("success") or _goal_config(pack, goal_id).get("success", {})
    required_event = success.get("event")
    if goal_id in {"G04", "G05", "G06"}:
        return _paid_purchase(event)
    if required_event and event.get("event") != required_event:
        return False
    if goal_id == "G03":
        # Both original app_open and corrected play_started are supported, solely
        # according to the goal's recorded rule version.
        return bool(required_event) and ist_day(event_time(event)) > ist_day(start)
    if goal_id == "G07":
        target = goal.get("target_slug")
        title = _catalogue(pack).get((event.get("properties") or {}).get("title_id"), {})
        return bool(target) and title.get("content_slug", title.get("slug")) == target
    if goal_id == "G08":
        title = _catalogue(pack).get((event.get("properties") or {}).get("title_id"), {})
        release = title.get("release_date")
        if not release:
            return False
        try:
            release_day = datetime.fromisoformat(str(release)[:10]).date()
        except ValueError:
            return False
        start_day = start.astimezone(IST).date()
        return start_day - timedelta(days=30) <= release_day <= start_day
    return bool(required_event)


def _exposure_link(
    goal: dict[str, Any], successes: list[dict[str, Any]], events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe observed temporal/session links; never claim individual causality."""
    action_ids = set(goal.get("action_ids") or [])
    start = ts(goal["started_at"])
    deadline = ts(goal["deadline"])
    observed: list[dict[str, Any]] = []
    linked_successes: list[str] = []
    delivered = []
    for event in events:
        when = event_time(event)
        if not start <= when <= deadline:
            continue
        props = event.get("properties") or {}
        action_id = props.get("page_action_id") or props.get("action_id")
        if action_id not in action_ids:
            continue
        if event.get("event") == "message_delivered":
            delivered.append(event.get("event_id"))
        if event.get("event") == "home_page_shown" and props.get("personalised"):
            observed.append(event)
        elif event.get("event") == "message_clicked":
            observed.append(event)
    for success in successes:
        s_time = event_time(success)
        sid = success.get("session_id")
        for exposure in observed:
            if event_time(exposure) > s_time:
                continue
            props = exposure.get("properties") or {}
            same_session = bool(sid) and sid == exposure.get("session_id")
            message_session = sid == "msg-" + str(props.get("action_id"))
            if same_session or message_session:
                linked_successes.append(success.get("event_id"))
                break
    return {
        "classification": "exposure_linked" if linked_successes else (
            "success_without_observed_action_link" if successes else "no_success_observed"
        ),
        "linked_success_event_ids": linked_successes,
        "observed_exposure_event_ids": [e.get("event_id") for e in observed],
        "confirmed_delivery_event_ids": delivered,
        "causal_claim": False,
        "note": "Temporal/session linkage is descriptive; causal impact is estimated by randomized assignment.",
    }


def judge_goal(
    goal: dict[str, Any], events: list[dict[str, Any]], pack: Any, coverage_end: datetime,
) -> dict[str, Any]:
    """Return an auditable success/failure/inconclusive judgment for one commitment.

    `coverage_end` must be an explicit observation watermark, not simply the last
    event observed for a particular viewer. No event is evidence of failure only
    when the complete window is observable. Superseded commitments are treated as
    administratively inconclusive rather than as failures.
    """
    start, deadline, coverage_end = ts(goal["started_at"]), ts(goal["deadline"]), ts(coverage_end)
    goal_id = goal.get("id") or goal.get("goal_id")
    user_id = goal.get("user_id")
    user_events = canonical_events([
        e for e in events if (not user_id or e.get("user_id") == user_id) and _received(e) <= coverage_end
    ])
    result: dict[str, Any] = {
        "goal_instance_id": goal.get("goal_instance_id"),
        "goal_id": goal_id,
        "user_id": user_id,
        "rule_version": goal.get("version"),
        "started_at": iso(start),
        "deadline": iso(deadline),
        "judged_at": iso(coverage_end),
        "status": "inconclusive",
        "evidence": [],
        "evidence_event_ids": [],
        "success_at": None,
        "coverage_complete": coverage_end >= deadline,
        "rule_history": goal.get("rule_history") or [],
    }
    if goal_id in {"G10", "G11"}:
        result["reason"] = "Goal has no supported measurable success signal."
        result["attribution"] = _exposure_link(goal, [], user_events)
        return result
    if goal.get("status") in {"superseded", "cancelled", "invalidated"}:
        result["reason"] = "Commitment was superseded/cancelled; it is not counted as a failed full-window goal."
        if goal.get("status") == "invalidated":
            result["reason"] = goal.get("invalidation_reason", "Later evidence invalidated the commitment's entry condition.")
            result["evidence"] = goal.get("invalidation_evidence", [])
            result["entry_condition_invalidated"] = True
        elif goal.get("superseded_by_decision_id"):
            result["evidence"] = ["decision:" + goal["superseded_by_decision_id"]]
        result["attribution"] = _exposure_link(goal, [], user_events)
        return result
    # Entry-negative claims can be contradicted by historical evidence delivered
    # after commitment, even if a goal had already been provisionally closed.
    # Past paid history alone does not invalidate G04: repeat trials are possible.
    contradictions = []
    for event in user_events:
        when = event_time(event)
        if _received(event) <= start or when > start or _clock_uncertain(event):
            continue
        kind = event.get("event")
        if (goal_id == "G01" and kind == "play_started"
            or goal_id == "G02" and kind == "episode_completed"
            or goal_id == "G05" and (_paid_purchase(event) or kind == "subscription_renewed")
            or goal_id == "G12" and kind == "app_open" and when >= start - timedelta(days=14)):
            contradictions.append("event:" + str(event.get("event_id")))
    if contradictions:
        result.update(reason="Late historical evidence contradicts the original goal entry condition.",
                      entry_condition_invalidated=True, evidence=contradictions)
        result["attribution"] = _exposure_link(goal, [], user_events)
        return result
    matches, uncertain = [], []
    for event in user_events:
        when = event_time(event)
        if not start < when <= deadline:
            continue
        if _matches(goal, event, pack, start):
            (uncertain if _clock_uncertain(event) else matches).append(event)
    if goal_id == "G09":
        distinct = {ist_day(event_time(e)) for e in matches}
        success = len(distinct) >= 3
        result["distinct_ist_play_days"] = sorted(str(day) for day in distinct)
        # Evidence for all observed days is useful; the earliest third day is the
        # achievement time, not a later duplicate play on that day.
        if success:
            first_by_day: dict[Any, dict[str, Any]] = {}
            for event in sorted(matches, key=event_time):
                first_by_day.setdefault(ist_day(event_time(event)), event)
            matches = sorted(first_by_day.values(), key=event_time)[:3]
    else:
        success = bool(matches)
        matches = sorted(matches, key=event_time)[:1]
    if success:
        result.update(
            status="succeeded", reason="Required signal observed after commitment and within its recorded window.",
            success_at=iso(max(event_time(e) for e in matches)),
            evidence=["event:" + str(e.get("event_id")) for e in matches],
            evidence_event_ids=[e.get("event_id") for e in matches],
        )
    elif uncertain:
        result["reason"] = "Potential matching evidence has uncertain event time; no definitive success or failure claim."
        result["uncertain_evidence_event_ids"] = [e.get("event_id") for e in uncertain]
    elif coverage_end < deadline:
        result["reason"] = "Observation horizon ends before the goal deadline."
    else:
        result.update(status="failed", reason="The complete observed goal window ended without its required signal.")
    result["attribution"] = _exposure_link(goal, matches if success else [], user_events)
    return result
