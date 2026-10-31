"""Reproducible replay reports with assignment-level intention-to-treat metrics."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .outcomes import _clock_uncertain, _paid_purchase, canonical_events
from .util import event_time, iso, ist_day, ts, write_json


def _pct(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.2f} pp" if signed else f"{value * 100:.2f}%"


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo), 3)


def _arm_comparison(values: dict[str, list[bool | None]]) -> dict[str, Any]:
    """Unknown followup stays in the randomized denominator as an explicit bound."""
    result: dict[str, Any] = {"arms": {}}
    for arm in ("agent", "holdout"):
        observations = values.get(arm, [])
        n = len(observations)
        successes = sum(value is True for value in observations)
        unknown = sum(value is None for value in observations)
        result["arms"][arm] = {
            "users": n, "successes": successes, "unknown": unknown,
            "observed_failures": n - successes - unknown,
            "rate": successes / n if n and not unknown else None,
            "rate_bounds": [successes / n, (successes + unknown) / n] if n else None,
        }
    treatment, holdout = result["arms"]["agent"], result["arms"]["holdout"]
    if treatment["users"] and holdout["users"]:
        result["difference_bounds"] = [
            treatment["rate_bounds"][0] - holdout["rate_bounds"][1],
            treatment["rate_bounds"][1] - holdout["rate_bounds"][0],
        ]
    else:
        result["difference_bounds"] = None
    p1, p0 = treatment["rate"], holdout["rate"]
    if p1 is not None and p0 is not None:
        delta = p1 - p0
        standard_error = math.sqrt(
            p1 * (1 - p1) / treatment["users"] + p0 * (1 - p0) / holdout["users"]
        )
        result.update(
            difference=delta,
            difference_ci95=[max(-1.0, delta - 1.96 * standard_error), min(1.0, delta + 1.96 * standard_error)],
            ci_method="Unadjusted independent-binomial normal approximation; descriptive, no multiplicity correction.",
        )
    else:
        result.update(difference=None, difference_ci95=None, ci_method="Not estimated: one arm is absent or followup is incomplete.")
    return result


def _baseline_paid(row: dict[str, Any]) -> bool | None:
    if "paid_at_baseline" in row:
        return row["paid_at_baseline"]
    if row.get("subscription_status") == "active" or row.get("ever_paid") is True:
        return True
    if row.get("ever_paid") is False:
        return False
    return None


def common_outcomes(
    baseline: dict[str, dict[str, Any]], outcome_events: list[dict[str, Any]],
    coverage_end: datetime, arms: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fixed outcomes, cohorts, and first-trigger anchors for both randomized arms.

    The first-paid metric uses only viewers supported as never-paid at baseline.
    All triggered users enter the other metrics, including failures and abstentions.
    No denominator conditions on serving, exposure, or the model-selected goal.
    """
    coverage_end = ts(coverage_end)
    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in canonical_events(outcome_events):
        by_user[event.get("user_id")].append(event)
    metric_values: dict[str, dict[str, list[bool | None]]] = {
        key: {"agent": [], "holdout": []}
        for key in ("any_play_7d", "episode_completion_7d", "first_paid_purchase_7d", "three_play_days_14d")
    }
    baseline_counts = Counter()
    user_results = {}
    for user_id, row in sorted(baseline.items()):
        arm = row.get("arm") or (arms or {}).get(user_id)
        if arm == "treatment":
            arm = "agent"
        if arm not in {"agent", "holdout"}:
            baseline_counts["excluded_unknown_arm"] += 1
            continue
        anchor_value = row.get("anchor_at") or row.get("first_trigger_at")
        if not anchor_value:
            baseline_counts["excluded_missing_anchor"] += 1
            continue
        anchor = ts(anchor_value)
        windows = {7: anchor + timedelta(days=7), 14: anchor + timedelta(days=14)}
        events = [
            e for e in by_user.get(user_id, [])
            if anchor < event_time(e) <= windows[14] and ts(e.get("received_at") or e["occurred_at"]) <= coverage_end
        ]
        uncertain = [e for e in events if _clock_uncertain(e)]
        definite = [e for e in events if not _clock_uncertain(e)]
        plays7 = [e for e in definite if e.get("event") == "play_started" and event_time(e) <= windows[7]]
        completions7 = [e for e in definite if e.get("event") == "episode_completed" and event_time(e) <= windows[7]]
        paid7 = [e for e in definite if _paid_purchase(e) and event_time(e) <= windows[7]]
        days14 = {ist_day(event_time(e)) for e in definite if e.get("event") == "play_started"}
        observed = {
            "any_play_7d": bool(plays7), "episode_completion_7d": bool(completions7),
            "first_paid_purchase_7d": bool(paid7), "three_play_days_14d": len(days14) >= 3,
        }
        at_risk = _baseline_paid(row)
        baseline_counts["users_" + arm] += 1
        if at_risk is None:
            baseline_counts["paid_baseline_unknown_" + arm] += 1
        elif at_risk:
            baseline_counts["ever_paid_at_baseline_" + arm] += 1
        else:
            baseline_counts["never_paid_at_baseline_" + arm] += 1
        classified = {}
        for metric, succeeded in observed.items():
            if metric == "first_paid_purchase_7d" and at_risk is not False:
                classified[metric] = "excluded_baseline_paid_or_unknown"
                continue
            days = 14 if metric == "three_play_days_14d" else 7
            relevant_uncertain = any(
                (e.get("event") == "play_started" if metric in {"any_play_7d", "three_play_days_14d"}
                 else e.get("event") == "episode_completed" if metric == "episode_completion_7d"
                 else _paid_purchase(e))
                and event_time(e) <= windows[days]
                for e in uncertain
            )
            value: bool | None = True if succeeded else (
                None if coverage_end < windows[days] or relevant_uncertain else False
            )
            metric_values[metric][arm].append(value)
            classified[metric] = value
        user_results[user_id] = {"arm": arm, "anchor_at": iso(anchor), "outcomes": classified}
    return {
        "cohort": "All randomized users with a supplied replay trigger and an observed first-trigger anchor; assignment-level analysis.",
        "anchor": "Each viewer's first trigger delivery, recorded before agent action; identical outcome windows in both arms.",
        "paid_cohort": "First-paid purchase uses only supported never-paid viewers at baseline (paid_at_baseline=false); known paid and unknown baseline histories are excluded before treatment.",
        "coverage_end": iso(coverage_end),
        "baseline_counts": dict(sorted(baseline_counts.items())),
        "metrics": {key: _arm_comparison(value) for key, value in metric_values.items()},
        "user_outcomes": user_results,
        "interpretation": "Randomized intention-to-treat estimates in this synthetic simulator, not evidence of production effectiveness or individual causality.",
    }


def _pilot_report(pack: Any) -> dict[str, Any]:
    rows = getattr(pack, "pilot_outcomes", [])
    values = {"agent": [], "holdout": []}
    served = Counter()
    for row in rows:
        arm = "agent" if row.get("arm") == "treatment" else row.get("arm")
        if arm not in values:
            continue
        values[arm].append(str(row.get("succeeded")).lower() == "true")
        if str(row.get("served")).lower() == "true":
            served[arm] += 1
    return {
        **_arm_comparison(values), "served_users": dict(served),
        "note": "The internal served-vs-everyone-else headline conditions on post-randomization exposure and is selection biased; it does not justify rollout.",
    }


def _latency_report(decisions: list[dict[str, Any]], run_meta: dict[str, Any]) -> dict[str, Any]:
    times = [float(d.get("decided_in_ms", d.get("latency_ms", 0))) for d in decisions]
    unique = [d for d in decisions
              if d.get("status") != "DUPLICATE" and not d.get("duplicate")
              and d.get("no_action_reason") != "duplicate_trigger"]
    calls = []
    tool_calls = []
    for decision in decisions:
        usage = decision.get("budget") or decision.get("usage") or {}
        calls.append(int(decision.get("model_calls", usage.get("model_calls", 0))))
        tool_calls.append(int(decision.get("tool_calls", usage.get("tool_calls", 0))))
    live = [d for d in decisions if d.get("provider_result", {}).get("provider") == "openai"]
    live_times = [float(d.get("decided_in_ms", 0)) for d in live]
    return {
        "provider": run_meta.get("provider"), "model": run_meta.get("model"),
        "scope": "All trigger deliveries including holdout/duplicate gates and deterministic fallback. See live_openai for live-only timings.",
        "decision_deliveries": len(decisions), "unique_decisions": len(unique),
        "milliseconds": {
            "p50": _quantile(times, .5), "p95": _quantile(times, .95), "p99": _quantile(times, .99),
            "max": max(times) if times else None, "mean": round(sum(times) / len(times), 3) if times else None,
        },
        "over_60_seconds": sum(t > 60_000 for t in times),
        "over_300_seconds": sum(t > 300_000 for t in times),
        "model_calls": {"total": sum(calls), "max_per_decision": max(calls, default=0)},
        "tool_calls": {"total": sum(tool_calls), "max_per_decision": max(tool_calls, default=0)},
        "live_openai": {"planning_attempts": len(live),
            "api_requests": sum(d.get("provider_result", {}).get("calls", 0) for d in live),
            "read_only_tool_calls": sum(d.get("provider_result", {}).get("tool_calls", 0) for d in live),
            "provider_errors": sum(bool(d.get("provider_result", {}).get("error")) for d in live),
            "fallbacks": sum(bool(d.get("fallback_reason")) for d in live),
            "layout_repairs": sum(bool(d.get("repair")) for d in live),
            "no_action_decisions": sum(not d.get("action_ids") for d in live),
            "milliseconds": {"p50": _quantile(live_times, .5), "p95": _quantile(live_times, .95),
                             "max": max(live_times) if live_times else None},
            "conservative_cost_usd": round(sum(d.get("provider_result", {}).get("estimated_cost_usd", 0) for d in live), 9)},
        "provider_summary": run_meta.get("provider_summary", {}),
        "note": "Includes measured decision handling and injected dependency waits. Offline provider results are not OpenAI latency measurements. No production-scale throughput claim is made.",
    }


def _write_text(path: Path, text: str) -> None:
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _rate_cell(arm: dict[str, Any]) -> str:
    if arm["rate"] is not None:
        return f"{arm['successes']}/{arm['users']} ({_pct(arm['rate'])})"
    if not arm["users"]:
        return "0/0 (n/a)"
    lo, hi = arm["rate_bounds"]
    return f"{arm['successes']}/{arm['users']}; {arm['unknown']} unknown [{_pct(lo)}, {_pct(hi)}]"


def build_reports(
    pack: Any, output_dir: Path, decisions: list[dict[str, Any]], actions: list[dict[str, Any]],
    goals: list[dict[str, Any]], outcome_events: list[dict[str, Any]],
    baseline: dict[str, dict[str, Any]], run_meta: dict[str, Any],
) -> dict[str, Any]:
    """Write evaluator-facing reports; return all machine-readable summaries."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not run_meta.get("coverage_end"):
        raise ValueError("Reports require an explicit coverage_end; last observed event is not a coverage watermark.")
    coverage_end = ts(run_meta["coverage_end"])
    impact = common_outcomes(baseline, outcome_events, coverage_end, getattr(pack, "arms", {}))
    impact["pilot"] = _pilot_report(pack)
    impact["partial_run"] = bool(run_meta.get("partial_run") or not run_meta.get("replay_complete", True))
    impact["triggered_users_total"] = run_meta.get("triggered_users_total", len(baseline))
    impact["provider"] = run_meta.get("provider")
    impact["model"] = run_meta.get("model")
    impact["provider_mode"] = run_meta.get("live_mode_note", "")
    impact["live_provider_sampled"] = run_meta.get("provider") == "openai" and run_meta.get("live_sample_limit") is not None
    latency = _latency_report(decisions, run_meta)
    canonical = canonical_events(outcome_events)
    event_counts = Counter(e.get("event") for e in canonical)
    action_counts = Counter(a.get("type") for a in actions)
    decision_counts = Counter(d.get("status", "UNKNOWN") for d in decisions)
    goal_counts = Counter(g.get("status", "unknown") for g in goals)
    by_goal: dict[str, Counter[str]] = defaultdict(Counter)
    for goal in goals:
        by_goal[goal.get("id") or goal.get("goal_id") or "unknown"][goal.get("status", "unknown")] += 1
    published_ids = {a.get("action_id") for a in actions}
    home_exposures = {
        (e.get("properties") or {}).get("page_action_id")
        for e in canonical if e.get("event") == "home_page_shown" and (e.get("properties") or {}).get("personalised")
    } & published_ids
    delivered_ids = {
        (e.get("properties") or {}).get("action_id") for e in canonical if e.get("event") == "message_delivered"
    } & published_ids
    clicked_ids = {
        (e.get("properties") or {}).get("action_id") for e in canonical if e.get("event") == "message_clicked"
    } & published_ids
    holdout_actions = [a for a in actions if getattr(pack, "arms", {}).get(a.get("user_id")) == "holdout"]
    impact["diagnostics"] = {
        "actions_by_type": dict(action_counts), "decisions_by_status": dict(decision_counts),
        "goals_by_status": dict(goal_counts), "goals_by_id_and_status": {k: dict(v) for k, v in by_goal.items()},
        "outcome_events_raw": len(outcome_events), "outcome_events_canonical": len(canonical),
        "outcome_event_counts": dict(event_counts), "published_pages_exposed": len(home_exposures),
        "published_messages_delivered": len(delivered_ids), "published_messages_clicked": len(clicked_ids),
        "holdout_action_count": len(holdout_actions),
    }
    write_json(output_dir / "impact_report.json", impact)
    write_json(output_dir / "latency_report.json", latency)
    table = ["| Fixed outcome | Agent assigned | Holdout assigned | Absolute difference | Approx. 95% interval |",
             "|---|---:|---:|---:|---:|"]
    for name, metric in impact["metrics"].items():
        interval = metric["difference_ci95"]
        ci = f"[{_pct(interval[0], signed=True)}, {_pct(interval[1], signed=True)}]" if interval else "n/a; see unknown bounds"
        table.append(f"| {name} | {_rate_cell(metric['arms']['agent'])} | {_rate_cell(metric['arms']['holdout'])} | {_pct(metric['difference'], signed=True)} | {ci} |")
    pilot = impact["pilot"]
    pdelta = pilot.get("difference")
    pci = pilot.get("difference_ci95")
    pilot_text = (
        f"Historical pilot ITT: treatment {_rate_cell(pilot['arms']['agent'])}; "
        f"holdout {_rate_cell(pilot['arms']['holdout'])}; difference {_pct(pdelta, signed=True)}"
        + (f", approximate 95% interval [{_pct(pci[0], signed=True)}, {_pct(pci[1], signed=True)}]." if pci else ".")
    )
    partial_warning = (
        "**Partial/demo replay:** these results demonstrate integration only. The full assigned-agent policy was not exercised; do not interpret this sample as a complete policy evaluation.\n\n"
        if impact["partial_run"] else ""
    )
    if impact["live_provider_sampled"]:
        partial_warning += "**Mixed provider policy:** OpenAI is used for a bounded sample of planning attempts; the remaining stream uses explicitly recorded deterministic fallback/gates. These aggregate outcomes describe that hybrid policy, not a complete OpenAI-policy experiment. Live-only latency and request counts are in `latency_report.json`.\n\n"
    _write_text(output_dir / "impact_report.md", f"""# Randomized impact report

{partial_warning}Provider: `{run_meta.get('provider')}`; model: `{run_meta.get('model') or 'keyless replay'}`. Synthetic simulator seed: `{run_meta.get('seed', 'unspecified')}`.

## Cohort and estimand

{impact['cohort']} Baseline includes {len(baseline)} users out of {impact['triggered_users_total']} triggered users. Serving, exposure, model success, and goal selection do not remove a viewer from their assigned arm. {impact['anchor']}

{impact['paid_cohort']} Baseline exclusions and unknowns are recorded in `impact_report.json`.

Coverage watermark: `{impact['coverage_end']}`. Success within an incomplete window is observable; absence of success in an incomplete window remains unknown, with rate bounds. Unknowns are not silently coded as failures or removed from the randomized denominator. The 14-day metric can be censored for late triggers.

## Prespecified common outcomes

{chr(10).join(table)}

Differences and intervals are in percentage points. Intervals use an unadjusted independent-binomial normal approximation; they are descriptive, with no multiplicity correction. No interval is produced where an arm is empty or followup is incomplete. A positive point estimate alone is not proof of improvement. These are outcomes in one synthetic simulator, not production effect estimates.

## Exposure and selected-goal diagnostics

Published actions: {len(actions)}. Confirmed exposed pages: {len(home_exposures)}; confirmed delivered messages: {len(delivered_ids)}; confirmed clicked messages: {len(clicked_ids)}. Holdout actions: {len(holdout_actions)}.

Selected-goal statuses: `{dict(goal_counts)}`. These are lifecycle diagnostics, not causal comparisons: the agent can choose goals with different difficulty or revise them. A publication is not delivery, a page publication is not exposure, and an individual success is not proof the intervention caused it.

## Why the prior pilot headline is misleading

{pilot_text}

{pilot['note']} The correct comparison keeps all users in their randomized arms. The pilot estimates a different historical policy and does not establish the new policy's impact.

## Measurement limitations

The supplied simulator omits live-only users, models ordinary title choice independently of page recommendations, does not model in-app display, and simplifies organic purchase timing. Our outcome evaluation deduplicates copied live events and reconstructed app opens, uses buffered-event time reconstruction, and preserves uncertainty. Exact recommendation/episode-continuation quality cannot be inferred from this simulator alone. Neither served-versus-unserved nor exposed-versus-unexposed rates are used as the primary effect estimate.
""")
    simulator = run_meta.get("simulator_report") or {}
    simulator_problems = simulator.get("policy_problem_count", "not run")
    _write_text(output_dir / "final_report.md", f"""# Replay verification report

## Run

- Provider: `{run_meta.get('provider')}`; model: `{run_meta.get('model') or 'keyless replay'}`.
- Replay complete: `{run_meta.get('replay_complete', not impact['partial_run'])}`; incomplete replay/cohort: `{impact['partial_run']}`; live-provider sampled: `{impact['live_provider_sampled']}`.
- Provider mode: {impact['provider_mode']}
- Source pack: `{run_meta.get('source_pack', 'unspecified')}`.
- Trigger deliveries/decisions: {len(decisions)}; baseline users: {len(baseline)}.
- Published actions: {len(actions)}; goal commitments: {len(goals)}.
- Canonical observed events: {len(canonical)} ({len(outcome_events)} raw rows).

## Reliability and safety evidence

- Decision statuses: `{dict(decision_counts)}`.
- Action types: `{dict(action_counts)}`.
- Holdout actions: {len(holdout_actions)}.
- Decisions over 60 seconds: {latency['over_60_seconds']}; over 300 seconds: {latency['over_300_seconds']}.
- Maximum measured decision latency: {latency['milliseconds']['max']} ms.
- Maximum model/tool calls per decision: {latency['model_calls']['max_per_decision']} / {latency['tool_calls']['max_per_decision']}.
- Simulator policy problems: {simulator_problems}.
- Independent validation summary: `{run_meta.get('validation_summary', {})}`.

Live OpenAI subset (separate from offline/gated decisions): `{latency['live_openai']}`.

The simulator's policy acceptance is not complete validation. The runtime independently checks eligibility, evidence, dialect, availability, consent, caps, text, schema and idempotency. The control log and per-decision records document corrected rules, withdrawal, faults and fallbacks. Scope/scale claims are limited to what was actually executed.

## Outcomes and impact

Goal statuses: `{dict(goal_counts)}`. Read `impact_report.md` for the assignment-level common-outcome comparison and uncertainty. Goal achievement and exposure linkage are reported separately from causal impact. See `latency_report.json` for actual timing/call counts and provider usage. Live-model measurements are not replaced with offline-provider estimates.

## Audit path

Inspect `decisions.jsonl`, `actions.jsonl`, `outcomes.jsonl`, the exported viewer state, and the durable SQLite ledger to trace a viewer from evidence through verification, publication and judgment. The sample is reproducible from its documented command/provider/seed; exact wall-clock timing can vary by machine.
""")
    summary = {"impact": impact, "latency": latency, "diagnostics": impact["diagnostics"]}
    write_json(output_dir / "report_summary.json", {
        "diagnostics": impact["diagnostics"], "latency": latency,
        "metrics": impact["metrics"], "partial_run": impact["partial_run"],
    })
    return summary
