"""Evidence reconciliation, goal options and independent publication checks.

This module never calls a model, mutates viewer memory, or publishes an action.
The model proposes a choice; facts and execution authority stay here.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import re
from typing import Any

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
VALID_DIALECTS = {f"d{i}" for i in range(1, 7)}
HISTORY_START = datetime(2026, 9, 1, tzinfo=IST).astimezone(UTC)
UNSUPPORTED = {
    "G06": "recently_lapsed_has_no_defined_threshold",
    "G10": "meaningful_engagement_has_no_measurable_success_condition",
    "G11": "tv_app_open_is_not_an_available_observation",
}


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z") if value else None


def _get(pack: Any, field: str, default: Any = None) -> Any:
    return pack.get(field, default) if isinstance(pack, dict) else getattr(pack, field, default)


def _bool(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _catalogue(pack: Any) -> dict:
    return _get(pack, "catalogue", {})


def _rules(pack: Any) -> dict:
    rules = _get(pack, "goals", {})
    if isinstance(rules, list):
        return {r["goal_id"]: r for r in rules}
    if "goals" in rules and isinstance(rules["goals"], list):
        return {r["goal_id"]: r for r in rules["goals"]}
    return rules


def event_time(event: dict) -> tuple[datetime | None, float, bool]:
    """Server-anchored estimate, buffered age, and usable shared-clock delta.

    It cancels a shared phone-clock offset but is still an estimate. Negative
    phone deltas are explicitly uncertain and never shift an event into future.
    """
    received = _dt(event.get("received_at") or event.get("deliver_at"))
    sent, occurred = _dt(event.get("sent_at")), _dt(event.get("occurred_at"))
    if not received:
        return None, 0.0, False
    if sent is None or occurred is None:
        return received, 0.0, False
    age = (sent - occurred).total_seconds()
    return received - timedelta(seconds=max(0.0, age)), max(0.0, age), age >= 0


def _fact(value: Any, source: str, refs: list[str], known_at: datetime | None,
          now: datetime, status: str | None = None, conflicts: list | None = None,
          resolution: str = "") -> dict:
    return {
        "value": value, "status": status or ("known" if value is not None else "unknown"),
        "source": source, "evidence_refs": list(dict.fromkeys(refs)),
        "known_at": _iso(known_at),
        "freshness_s": max(0.0, (now - known_at).total_seconds()) if known_at else None,
        "conflicts": conflicts or [], "resolution": resolution,
    }


def _events_asof(events: list[dict], trigger: dict, now: datetime) -> list[dict]:
    unique = {}
    for event in [*events, trigger]:
        if not event or not event.get("event_id"):
            continue
        known = _dt(event.get("received_at") or event.get("deliver_at"))
        if known is None or known > now:
            continue
        old = unique.get(event["event_id"])
        if old is None or (_dt(old.get("received_at")) or now) > known:
            unique[event["event_id"]] = event
    return sorted(unique.values(), key=lambda e: (e.get("received_at", ""), e["event_id"]))


def resolve_facts(pack: Any, events: list[dict], trigger: dict, now: datetime) -> dict:
    """Resolve facts with provenance, without treating a missing event as false."""
    now = _dt(now)
    assert now is not None
    uid = trigger.get("user_id") or (events[0].get("user_id") if events else None)
    events = _events_asof([e for e in events if e.get("user_id") == uid], trigger, now)
    profile = _get(pack, "profiles", {}).get(uid) or {}
    # A snapshot not yet exported at this boundary is not knowable.
    if _dt(profile.get("snapshot_at")) and _dt(profile["snapshot_at"]) > now:
        profile = {}
    profile_known = _dt(profile.get("snapshot_at"))
    profile_at = _dt(profile.get("updated_at")) or profile_known
    pref = f"profile:{uid}@{profile.get('updated_at', 'missing')}"
    versions = [r for r in _get(pack, "profile_versions", {}).get(uid, [])
                if not _dt(r.get("snapshot_at")) or _dt(r["snapshot_at"]) <= now]
    evidence: dict[str, dict] = {}
    if profile:
        evidence[pref] = {
            "source": "profiles_snapshot.csv", "user_id": uid,
            "known_at": _iso(profile_known), "fact_updated_at": _iso(profile_at),
        }
    for version in versions:
        ref = f"profile:{uid}@{version.get('updated_at', 'missing')}"
        evidence[ref] = {"source": "profiles_snapshot.csv", "user_id": uid,
                         "known_at": version.get("snapshot_at"), "fact_updated_at": version.get("updated_at")}
    for e in events:
        estimate, age, usable = event_time(e)
        evidence[f"event:{e['event_id']}"] = {
            "source": "event", "event_id": e["event_id"], "event": e.get("event"),
            "known_at": e.get("received_at"), "estimated_at": _iso(estimate),
            "buffered_s": round(age, 3), "event_time_confidence": "estimated" if usable else "uncertain",
        }
    provenance = {}
    fresh_settings = [e for e in events if event_time(e)[1] <= 600 and event_time(e)[2]]
    setting = fresh_settings[-1] if fresh_settings else None
    if setting:
        raw_dialect = setting.get("app_dialect")
        dialect = raw_dialect if raw_dialect in VALID_DIALECTS else None
        language = setting.get("app_language") if setting.get("app_language") in {"hin", "en"} else None
        conflicts = []
        if profile.get("preferred_dialect") and profile["preferred_dialect"] != raw_dialect:
            conflicts.append({"source": pref, "value": profile["preferred_dialect"]})
        link = (trigger.get("properties") or {}).get("deeplink", "")
        match = re.match(r"ott://c/[^/]+/([^/]+)/title/", str(link))
        if match and match.group(1) != raw_dialect:
            conflicts.append({"source": "trigger_campaign_link", "value": match.group(1)})
        provenance["dialect"] = _fact(
            dialect, "app_settings_feed", [f"event:{setting['event_id']}"], _dt(setting.get("received_at")), now,
            status="known" if dialect else "unknown", conflicts=conflicts,
            resolution="Current fresh app setting determines displayed dialect; no profile/link substitution for invalid settings.",
        )
        provenance["dialect"]["raw_value"] = raw_dialect
        provenance["language"] = _fact(language, "app_settings_feed", [f"event:{setting['event_id']}"], _dt(setting.get("received_at")), now)
    else:
        dialect = language = None
        provenance["dialect"] = _fact(None, "no_fresh_settings", [], None, now, resolution="Buffered events cannot establish the app's current dialect.")
        provenance["language"] = _fact(None, "no_fresh_settings", [], None, now)

    signup = _dt(profile.get("signup_at"))
    signup_ref = [pref] if signup else []
    signup_known = profile_known if signup else None
    signup_events = [e for e in events if e.get("event") == "signup_completed"]
    if signup_events:
        e = min(signup_events, key=lambda x: event_time(x)[0] or now)
        estimated, _, usable = event_time(e)
        if signup is None and usable:
            signup, signup_ref, signup_known = estimated, [f"event:{e['event_id']}"], _dt(e.get("received_at"))
    if signup and signup > now:
        signup = None
    provenance["signup_at"] = _fact(_iso(signup), "profile_or_signup_event", signup_ref, signup_known, now)

    status = profile.get("subscription_status") if profile.get("subscription_status") in {"never", "trial_active", "active", "lapsed"} else "unknown"
    plan_start, plan_end = _dt(profile.get("plan_started_at")), _dt(profile.get("plan_ends_at"))
    billing_refs = [pref] if status != "unknown" else []
    billing_known = profile_known
    billing_fact_at = profile_at or datetime.min.replace(tzinfo=UTC)
    conflicts = [{"source": f"profile:{uid}@{v.get('updated_at', 'missing')}", "value": v.get("subscription_status"),
                  "reason": "Older profile version differs; latest version is used only for current warehouse status."}
                 for v in versions if v.get("updated_at") != profile.get("updated_at")
                 and v.get("subscription_status") != profile.get("subscription_status")]
    ever_paid_events = []
    plans = {p["plan_id"]: p for p in _get(pack, "pricing", {}).get("plans", [])}
    for e in events:
        ev, props = e.get("event"), e.get("properties") or {}
        at, _, usable = event_time(e)
        if ev == "subscription_renewed" or (ev == "purchase_completed" and props.get("is_trial") is False):
            ever_paid_events.append(e)
        if ev not in {"purchase_completed", "subscription_renewed", "subscription_lapsed"} or at is None:
            continue
        if not usable:
            conflicts.append({"source": f"event:{e['event_id']}", "reason": "uncertain_billing_event_time"})
            continue
        # Fact-time precedence avoids letting buffered old purchases undo later lapsed state.
        if at >= billing_fact_at:
            prior = status
            if ev == "subscription_lapsed":
                status, plan_end = "lapsed", at
            else:
                status = "trial_active" if ev == "purchase_completed" and props.get("is_trial") is True else "active"
                plan_start = at
                plan = plans.get(props.get("plan_id"))
                plan_end = at + timedelta(days=int(plan["days"])) if plan else None
            if prior not in {"unknown", status}:
                conflicts.append({"source": pref if profile else "prior_billing_event", "value": prior})
            billing_fact_at, billing_known = at, _dt(e.get("received_at"))
            billing_refs = [f"event:{e['event_id']}"]
    if status in {"active", "trial_active"}:
        if plan_end is None:
            conflicts.append({"reason": "missing_plan_end_cannot_prove_current_entitlement"})
            status = "unknown"
        elif plan_end <= now:
            status = "lapsed"
            conflicts.append({"reason": "plan_window_elapsed"})
    if ever_paid_events:
        ever_paid = True
        ever_refs = [f"event:{e['event_id']}" for e in ever_paid_events[-3:]]
    elif profile.get("subscription_status") in {"active", "lapsed"}:
        ever_paid, ever_refs = True, [pref]
    elif profile.get("subscription_status") == "never":
        older_paid = [v for v in versions if v.get("subscription_status") in {"active", "lapsed"}]
        if older_paid:
            # A newer current-state row cannot establish the lifetime-negative
            # fact by erasing a conflicting older paid-status export. Without a
            # billing event, preserve this as unknown rather than choose either.
            ever_paid = None
            ever_refs = [pref] + [f"profile:{uid}@{v.get('updated_at', 'missing')}" for v in older_paid]
        else:
            ever_paid, ever_refs = False, [pref]
    else:
        ever_paid, ever_refs = None, []
    provenance["subscription_status"] = _fact(status, "billing_events_reconciled_with_profile", billing_refs, billing_known, now,
        status="unknown" if status == "unknown" else "known", conflicts=conflicts,
        resolution="Compare event-time estimate with fact update time; preserve ever-paid evidence and expire dated plans.")
    provenance["ever_paid"] = _fact(ever_paid, "paid_history_or_profile", ever_refs, billing_known, now,
        conflicts=conflicts if ever_paid is None and ever_refs else [],
        resolution="Paid events take precedence. Conflicting profile-only paid/never versions leave lifetime payment unknown.")

    consent = {}
    for channel, key in [("push", "notifications_opt_in"), ("whatsapp", "whatsapp_opt_in")]:
        value = _bool(profile.get(key))
        refs = [pref] if profile and profile.get(key) in {"true", "false", True, False} else []
        known = profile_known
        optouts = [e for e in events if e.get("event") == "notification_opt_out" and (e.get("properties") or {}).get("channel") == channel]
        if optouts:
            # There is no explicit opt-in event in this schema; do not infer reinstatement.
            value = False
            refs = [f"event:{optouts[-1]['event_id']}"]
            known = _dt(optouts[-1].get("received_at"))
        consent[key] = value
        provenance[key] = _fact(value, "consent_profile_with_optout_override", refs, known, now,
            status="known" if refs else "unknown", resolution="Only explicit affirmative consent permits an outbound channel.")

    history_start = _dt(_get(pack, "history_start", HISTORY_START)) or HISTORY_START
    lifetime = bool(signup and signup >= history_start and signup <= now)
    coverage_start = max(history_start, signup) if signup else history_start
    gaps = [(_dt(a), _dt(b)) for a, b in _get(pack, "observation_gaps", [])]
    incomplete = bool(_get(pack, "incomplete_live_coverage", False))
    lifetime_gap = incomplete or any(a and b and a < now and b > coverage_start for a, b in gaps)
    recent_gap = incomplete or any(a and b and a < now and b > now - timedelta(days=14) for a, b in gaps)
    plays, completes = [], []
    opens_before_trigger = []
    uncertain_recent_plays = []
    uncertain_recent_opens = []
    genres = Counter()
    completed_eps = defaultdict(set)
    series_plays = defaultdict(list)
    trigger_id = trigger.get("event_id")
    for e in events:
        at, _, usable = event_time(e)
        props = e.get("properties") or {}
        ev = e.get("event")
        title = _catalogue(pack).get(props.get("title_id")) or {}
        if ev == "play_started":
            plays.append(e)
            if not usable and (_dt(e.get("received_at")) or now) >= now - timedelta(days=14):
                uncertain_recent_plays.append(e)
            genres.update(_genres(title))
            if title.get("content_type") in {"show", "micro_drama"}:
                series_plays[title["content_slug"]].append(e)
        elif ev == "episode_completed":
            completes.append(e)
            if title.get("content_slug") and props.get("episode_id"):
                completed_eps[title["content_slug"]].add(props["episode_id"])
        if ev == "app_open" and e["event_id"] != trigger_id and at and at < now and usable:
            opens_before_trigger.append(e)
        elif ev == "app_open" and e["event_id"] != trigger_id and not usable and (_dt(e.get("received_at")) or now) >= now - timedelta(days=14):
            uncertain_recent_opens.append(e)
    play_days = sorted({event_time(e)[0].astimezone(IST).date().isoformat() for e in plays
        if event_time(e)[0] and event_time(e)[2] and now - timedelta(days=14) <= event_time(e)[0] < now})
    recent_opens = [e for e in opens_before_trigger if now - timedelta(days=14) <= event_time(e)[0] < now]
    unfinished = []
    for slug, starts in series_plays.items():
        inventory = _get(pack, "episodes", {}).get(slug, [])
        ordered = sorted(inventory, key=lambda r: (int(r.get("season", 1)), int(r.get("episode", 1))))
        remaining = [r for r in ordered if r.get("episode_id") not in completed_eps[slug]]
        credible = [e for e in starts if event_time(e)[2]]
        if remaining:
            latest = max(credible, key=lambda e: event_time(e)[0]) if credible else starts[-1]
            uncertain_recency = any(not event_time(e)[2] and (_dt(e.get("received_at")) or now) >= now - timedelta(days=3) for e in starts)
            unfinished.append({
                "content_slug": slug, "last_play_at": _iso(event_time(latest)[0]) if credible else None,
                "recency_uncertain": uncertain_recency or not credible,
                "next_episode_id": remaining[0].get("episode_id"),
                "next_episode_number": int(remaining[0].get("episode", 1)),
                "remaining_count": len(remaining),
                "evidence_refs": [f"event:{latest['event_id']}", f"episodes:{slug}"],
            })
    unfinished.sort(key=lambda s: s["last_play_at"] or "", reverse=True)
    behavior = {
        "play_count": len(plays), "completion_count": len(completes),
        "play_days_14d": play_days, "top_genres": [g for g, _ in genres.most_common(3)],
        "genre_counts": dict(genres.most_common()), "unfinished_series": unfinished,
        "prior_app_open_count_14d": len(recent_opens),
        "uncertain_play_count_14d": len(uncertain_recent_plays),
        "uncertain_open_count_14d": len(uncertain_recent_opens),
        "last_play_at": _iso(max((event_time(e)[0] for e in plays if event_time(e)[0]), default=None)),
        "last_app_open_before_trigger": _iso(max((event_time(e)[0] for e in opens_before_trigger), default=None)),
        "play_evidence": [f"event:{e['event_id']}" for e in plays[-5:]],
        "completion_evidence": [f"event:{e['event_id']}" for e in completes[-3:]],
        "recent_open_evidence": [f"event:{e['event_id']}" for e in recent_opens[-3:]],
    }
    _, trigger_age, trigger_time_usable = event_time(trigger)
    facts = {
        "user_id": uid, "dialect": dialect, "language": language,
        "subscription_status": status, "signup_at": _iso(signup),
        "signup_age_days": (now - signup).total_seconds() / 86400 if signup else None,
        "plan_started_at": _iso(plan_start), "plan_ends_at": _iso(plan_end),
        "ever_paid": ever_paid, **consent, "provenance": provenance,
        "behavior": behavior,
        "observation_coverage": {
            "start": _iso(coverage_start), "end": _iso(now),
            "lifetime_from_signup": lifetime and not lifetime_gap,
            "covers_prior_14d": coverage_start <= now - timedelta(days=14) and not recent_gap,
            "gaps": [[_iso(a), _iso(b)] for a, b in gaps],
            "assumption": "Live ingestion has no complete-observation watermark; negative activity claims remain unknown." if incomplete else "Supplied historical export covers instrumented app activity within its stated window; offline arrivals can still revise facts.",
        },
        "trigger_buffered_s": trigger_age, "trigger_is_fresh": trigger_age <= 600 and trigger_time_usable,
        "evidence_index": evidence,
    }
    return facts


def _genres(title: dict) -> list[str]:
    genres = title.get("genres") or []
    return genres.split("|") if isinstance(genres, str) else list(genres)


def eligible_goals(pack: Any, facts: dict, now: datetime) -> list[dict]:
    """Return all configured options, including explicit unknown/unsupported ones."""
    now = _dt(now)
    assert now
    result = []
    behavior, coverage = facts.get("behavior", {}), facts.get("observation_coverage", {})
    age = facts.get("signup_age_days")
    status = facts.get("subscription_status", "unknown")
    for gid, rule in sorted(_rules(pack).items()):
        option = {
            "id": gid, "goal_id": gid, "name": rule.get("name", gid),
            "description": rule.get("description", ""), "status": "ineligible", "reason": "condition_not_met",
            "version": f"{gid}@{_get(pack, 'goals_version', _get(pack, 'goal_version', '2026-09-30'))}",
            "success": deepcopy(rule.get("success", {})),
            "window_hours": rule.get("success", {}).get("window_hours"),
            "eligibility_evidence": [], "target_slug": None,
        }
        prov = facts.get("provenance", {})
        refs = lambda key: prov.get(key, {}).get("evidence_refs", [])
        def mark(state: str, reason: str, evidence_refs: list[str] | None = None):
            option.update(status=state, reason=reason)
            option["eligibility_evidence"] = list(dict.fromkeys((evidence_refs or []) + [f"goal_rule:{gid}"]))
        if gid in UNSUPPORTED:
            mark("unsupported", UNSUPPORTED[gid])
        elif gid == "G01":
            if behavior.get("play_count", 0):
                mark("ineligible", "prior_play_exists", behavior.get("play_evidence", []))
            elif age is None or not coverage.get("lifetime_from_signup"):
                mark("unknown", "signup_or_lifetime_coverage_unknown", refs("signup_at"))
            else:
                mark("eligible" if 0 <= age <= 7 else "ineligible", "recent_signup_no_observed_lifetime_play" if 0 <= age <= 7 else "signup_older_than_7d", refs("signup_at"))
        elif gid == "G02":
            if behavior.get("completion_count", 0):
                mark("ineligible", "prior_completion_exists", behavior.get("completion_evidence", []))
            elif not coverage.get("lifetime_from_signup"):
                mark("unknown", "no_completion_in_window_does_not_prove_lifetime_absence", refs("signup_at"))
            else:
                mark("eligible", "complete_observed_lifetime_without_completion", refs("signup_at") + behavior.get("play_evidence", []))
        elif gid == "G03":
            mark("unknown" if age is None else ("eligible" if 0 <= age <= 2 else "ineligible"), "signup_age_unknown" if age is None else ("signup_within_2d" if 0 <= age <= 2 else "signup_older_than_2d"), refs("signup_at"))
        elif gid in {"G04", "G08", "G09"}:
            desired = "trial_active" if gid == "G04" else "active"
            if status == "unknown":
                mark("unknown", "current_subscription_unknown", refs("subscription_status"))
            elif status != desired:
                mark("ineligible", f"requires_{desired}", refs("subscription_status"))
            elif gid == "G09" and not coverage.get("covers_prior_14d"):
                mark("unknown", "insufficient_14d_observation", refs("signup_at"))
            elif gid == "G09" and len(behavior.get("play_days_14d", [])) > 1:
                mark("ineligible", "plays_on_more_than_one_prior_day", behavior.get("play_evidence", []))
            elif gid == "G09" and behavior.get("uncertain_play_count_14d", 0):
                mark("unknown", "uncertain_play_times_could_change_prior_day_count", behavior.get("play_evidence", []))
            else:
                mark("eligible", "subscription_and_observed_window_support_goal", refs("subscription_status") + (behavior.get("play_evidence", []) if gid == "G09" else []))
        elif gid == "G05":
            if facts.get("ever_paid") is True:
                mark("ineligible", "paid_history_contradicts_never_paid", refs("ever_paid"))
            elif status == "never" and facts.get("ever_paid") is False:
                mark("eligible", "warehouse_never_status_without_paid_contradiction", refs("subscription_status") + refs("ever_paid"))
            elif status == "unknown" or facts.get("ever_paid") is None:
                mark("unknown", "never_paid_status_not_supported", refs("subscription_status"))
            else:
                mark("ineligible", "requires_status_never", refs("subscription_status"))
        elif gid == "G07":
            candidates = [s for s in behavior.get("unfinished_series", []) if not s.get("recency_uncertain") and _dt(s.get("last_play_at")) and _dt(s["last_play_at"]) <= now - timedelta(days=3)]
            gap_in_idle_window = bool(_get(pack, "incomplete_live_coverage", False)) or any(_dt(a) < now and _dt(b) > now - timedelta(days=3) for a, b in coverage.get("gaps", []) if _dt(a) and _dt(b))
            if candidates and gap_in_idle_window:
                mark("unknown", "observation_gap_cannot_prove_series_idle_3d", behavior.get("play_evidence", []))
            elif candidates:
                target = candidates[0]
                option["target_slug"] = target["content_slug"]
                option["target_options"] = [s["content_slug"] for s in candidates]
                mark("eligible", "unfinished_series_idle_at_least_3d", target["evidence_refs"])
            elif any(s.get("recency_uncertain") for s in behavior.get("unfinished_series", [])):
                mark("unknown", "uncertain_series_play_time_cannot_prove_3d_absence", behavior.get("play_evidence", []))
            else:
                mark("ineligible", "no_supported_unfinished_series_idle_3d", behavior.get("play_evidence", []))
        elif gid == "G12":
            if behavior.get("prior_app_open_count_14d", 0):
                mark("ineligible", "prior_open_within_14d_excluding_trigger", behavior.get("recent_open_evidence", []))
            elif behavior.get("uncertain_open_count_14d", 0):
                mark("unknown", "uncertain_open_time_cannot_prove_14d_absence", behavior.get("recent_open_evidence", []))
            elif not coverage.get("covers_prior_14d"):
                mark("unknown", "insufficient_14d_observation", refs("signup_at"))
            else:
                mark("eligible", "no_observed_open_in_covered_pretrigger_14d", refs("signup_at"))
        if option.get("window_hours"):
            option["deadline_if_committed_now"] = _iso(now + timedelta(hours=option["window_hours"]))
        result.append(option)
    return result


def title_available(title: dict | None, now: datetime, withdrawn: dict | None = None) -> bool:
    if not title:
        return False
    start, end = _dt(title.get("available_from")), _dt(title.get("available_until"))
    if start is None or start > now or (end is not None and end <= now):
        return False
    withdrawal = (withdrawn or {}).get(title.get("title_id"))
    if isinstance(withdrawal, dict):
        withdrawal = withdrawal.get("at")
    return not (_dt(withdrawal) and _dt(withdrawal) <= now)


def _day(value: datetime) -> str:
    return value.astimezone(IST).date().isoformat()


def _canonical_copy(title: dict) -> dict:
    return {"title": "Something to watch", "body": f"Watch {title.get('title', '')} in your app."}


def is_recent_release(title: dict, now: datetime) -> bool:
    """Date-only release metadata is compared on frozen IST calendar dates."""
    try:
        released = date.fromisoformat(str(title.get("release_date", ""))[:10])
    except ValueError:
        return False
    today = now.astimezone(IST).date()
    return today - timedelta(days=30) <= released <= today


def _candidate_record(title: dict, popularity: dict) -> dict:
    record = {key: title.get(key) for key in ["title_id", "content_slug", "title", "dialect", "language", "content_type", "genres", "release_date", "available_from", "available_until", "is_premium"]}
    record["popularity_count"] = popularity.get(title["content_slug"], 0)
    record["push_copy"] = _canonical_copy(title)
    record["evidence_ref"] = f"catalogue:{title['title_id']}"
    return record


def _candidates(pack: Any, facts: dict, now: datetime, withdrawn: dict, *,
                limit: int = 24, genre: str | None = None, exclude_slugs: list[str] | None = None) -> list[dict]:
    dialect, language = facts.get("dialect"), facts.get("language")
    if dialect not in VALID_DIALECTS:
        return []
    by_slug = {}
    for t in _catalogue(pack).values():
        if t.get("dialect") != dialect or not title_available(t, now, withdrawn):
            continue
        slug = t["content_slug"]
        if slug in (exclude_slugs or []) or (genre and genre not in _genres(t)):
            continue
        old = by_slug.get(slug)
        if old is None or (t.get("language") == language and old.get("language") != language):
            by_slug[slug] = t
    unfinished = {s["content_slug"] for s in facts.get("behavior", {}).get("unfinished_series", [])}
    genres = set(facts.get("behavior", {}).get("top_genres", []))
    popularity = _get(pack, "popularity", Counter())
    uid = facts.get("user_id", "")
    def score(t):
        new = is_recent_release(t, now)
        tie = hashlib.sha256(f"{uid}:{t['content_slug']}".encode()).hexdigest()
        return (-(100 * (t["content_slug"] in unfinished) + 4 * len(genres.intersection(_genres(t))) + 3 * new + (not _bool(t.get("is_premium")) and facts.get("subscription_status") != "active")), -popularity.get(t["content_slug"], 0), tie)
    ranked = sorted(by_slug.values(), key=score)
    # Ensure a feasible new-release option is represented alongside the top overall candidates.
    limit = max(1, min(100, int(limit)))
    selected = ranked[:max(0, limit - 3)]
    recent = [t for t in ranked if is_recent_release(t, now)]
    for t in recent[:3] + ranked:
        if t not in selected and len(selected) < limit:
            selected.append(t)
    output = []
    for t in selected:
        # Deliberately exclude descriptions, including injected instructions.
        output.append(_candidate_record(t, popularity))
    return output


def content_candidates(pack: Any, facts: dict, now: datetime, withdrawn: dict,
                       limit: int = 24, genre: str | None = None,
                       exclude_slugs: list[str] | None = None) -> list[dict]:
    """Bounded current-dialect retrieval used by the viewer-scoped read tool."""
    return _candidates(pack, facts, _dt(now), withdrawn, limit=limit, genre=genre, exclude_slugs=exclude_slugs)


def _active_continuation(active: dict, facts: dict, now: datetime) -> bool:
    if not active or active.get("status") not in {None, "open", "active", "pending"}:
        return False
    if _dt(active.get("deadline")) and _dt(active["deadline"]) <= now:
        return False
    gid = active.get("goal_id") or active.get("id")
    if gid in UNSUPPORTED:
        return False
    if gid == "G04" and facts.get("subscription_status") != "trial_active":
        return False
    if gid == "G05" and facts.get("ever_paid") is not False:
        return False
    if gid in {"G08", "G09"} and facts.get("subscription_status") != "active":
        return False
    if gid == "G01" and facts.get("behavior", {}).get("play_count", 0):
        return False
    if gid == "G02" and facts.get("behavior", {}).get("completion_count", 0):
        return False
    return True


def build_context(pack: Any, events: list[dict], trigger: dict, now: datetime,
                  active_goal: dict | None, prior_actions: list[dict],
                  rule_overrides: dict, withdrawn: dict) -> dict:
    now = _dt(now)
    assert now
    facts = resolve_facts(pack, events, trigger, now)
    assessments = eligible_goals(pack, facts, now)
    for option in assessments:
        override = rule_overrides.get(option["goal_id"])
        if override:
            option["success"].update(override.get("success") or override.get("new_success") or {})
            option["version"] = override.get("version", option["version"] + "-corrected")
            option["rule_effective_at"] = override.get("effective_at") or override.get("at")
    available_slugs = {t["content_slug"] for t in _catalogue(pack).values()
        if t.get("dialect") == facts.get("dialect") and title_available(t, now, withdrawn)}
    for option in assessments:
        option["source_eligibility_status"] = option["status"]
        if option["goal_id"] == "G07" and option["status"] == "eligible":
            source_targets = list(option.get("target_options") or [option.get("target_slug")])
            feasible_targets = [slug for slug in source_targets if slug in available_slugs]
            option["source_target_options"] = source_targets
            option["feasible"] = bool(feasible_targets)
            option["feasibility_reason"] = "available_current_dialect_variant_exists" if feasible_targets else "source_eligible_but_no_available_target_in_current_dialect"
            if feasible_targets:
                option["target_options"] = feasible_targets
                option["target_slug"] = feasible_targets[0]
                series = next((s for s in facts["behavior"]["unfinished_series"] if s["content_slug"] == option["target_slug"]), {})
                option["eligibility_evidence"] = list(dict.fromkeys(series.get("evidence_refs", []) + ["goal_rule:G07"]))
    eligible = [deepcopy(g) for g in assessments if g["status"] == "eligible" and g.get("feasible", True)]
    active_viable = _active_continuation(active_goal or {}, facts, now)
    active_invalid_reason = None
    if active_goal and (active_goal.get("goal_id") or active_goal.get("id")) == "G07" and active_goal.get("target_slug") not in available_slugs:
        active_viable = False
        active_invalid_reason = "anchored_series_has_no_available_current_dialect_variant"
    elif active_goal and not active_viable:
        active_invalid_reason = "existing_goal_no_longer_supported_by_current_facts_or_window"
    if active_viable:
        gid = active_goal.get("goal_id") or active_goal.get("id")
        existing = next((g for g in eligible if g["goal_id"] == gid), None)
        if existing is None:
            existing = deepcopy(next((g for g in assessments if g["goal_id"] == gid), {}))
            if existing:
                existing.update(status="eligible", reason="valid_existing_commitment_retains_original_window", continuation=True)
                existing["eligibility_evidence"] = list(active_goal.get("eligibility_evidence") or active_goal.get("evidence_refs") or [f"goal_commitment:{active_goal.get('goal_instance_id', gid)}"])
                eligible.append(existing)
        if existing:
            existing["continuation"] = True
            existing["deadline"] = active_goal.get("deadline")
            existing["started_at"] = active_goal.get("started_at")
            existing["target_slug"] = active_goal.get("target_slug")
    candidates = _candidates(pack, facts, now, withdrawn)
    # A bounded shortlist must not hide a feasible anchored goal's target.
    required_slugs = {g.get("target_slug") for g in eligible if g.get("goal_id") == "G07"}
    if active_viable and (active_goal or {}).get("target_slug"):
        required_slugs.add(active_goal["target_slug"])
    for slug in required_slugs - {c["content_slug"] for c in candidates}:
        title = next((t for t in _catalogue(pack).values() if t.get("content_slug") == slug and t.get("dialect") == facts.get("dialect") and title_available(t, now, withdrawn) and t.get("language") == facts.get("language")), None)
        title = title or next((t for t in _catalogue(pack).values() if t.get("content_slug") == slug and t.get("dialect") == facts.get("dialect") and title_available(t, now, withdrawn)), None)
        if title:
            candidate = _candidate_record(title, _get(pack, "popularity", {}))
            if len(candidates) >= 24:
                candidates.pop()
            candidates.insert(0, candidate)
    intr = _get(pack, "interventions", {})
    valid_actions = [a for a in prior_actions if _dt(a.get("published_at")) and _dt(a["published_at"]) <= now and a.get("status") != "cancelled"]
    outbound_today = [a for a in valid_actions if a.get("type") in {"push", "whatsapp"} and _day(_dt(a.get("send_at") or a["published_at"])) == _day(now)]
    delivered_today = [e for e in _events_asof(events, trigger, now) if e.get("event") == "message_delivered" and (e.get("properties") or {}).get("channel") in {"push", "whatsapp"} and event_time(e)[0] and _day(event_time(e)[0]) == _day(now)]
    # A published request reserves the cap even if eventual delivery fails.
    allowance = 0 if outbound_today or delivered_today else 1
    inapp_today = [a for a in valid_actions if a.get("type") == "in_app_message" and _day(_dt(a["published_at"])) == _day(now)]
    quiet = now.astimezone(IST).hour >= 22 or now.astimezone(IST).hour < 8
    dialect_ok = facts["dialect"] in VALID_DIALECTS
    push_allowed = dialect_ok and facts["notifications_opt_in"] and not quiet and allowance > 0
    wa_allowed = dialect_ok and facts["whatsapp_opt_in"] and facts["language"] == "hin" and not quiet and allowance > 0
    inapp_allowed = dialect_ok and not inapp_today
    rows = intr.get("home_page", {}).get("rows", {})
    constraints = {
        "policy_version": intr.get("version", "2026-09-30"),
        "allowed_action_types": (["home_page"] if dialect_ok and candidates else []) + (["push"] if push_allowed else []) + (["whatsapp"] if wa_allowed else []) + (["in_app_message"] if inapp_allowed else []),
        "push_allowed": push_allowed, "whatsapp_allowed": wa_allowed,
        "in_app_allowed": inapp_allowed, "quiet_hours": quiet,
        "messaging_allowance": allowance, "in_app_allowance": 0 if inapp_today else 1,
        "max_rows": rows.get("max_rows", 6), "min_titles_per_row": rows.get("min_titles_per_row", 3),
        "max_titles_per_row": rows.get("max_titles_per_row", 10),
        "row_types": rows.get("row_types", ["next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre"]),
        "approved_templates": intr.get("whatsapp", {}).get("approved_templates", []),
        "pricing": _get(pack, "pricing", {}).get("plans", []),
        "copy_policy": "Use supplied push_copy exactly or leave text fields null for the backend to compile grounded copy. No invented prices, deadlines, episodes, offers or claims.",
        "max_model_calls": 8, "max_tool_calls": 20,
    }
    evidence = {}
    needed = set()
    for fact in facts["provenance"].values():
        needed.update(fact["evidence_refs"])
    for goal in assessments + eligible:
        needed.update(goal["eligibility_evidence"])
    for series in facts["behavior"]["unfinished_series"]:
        needed.update(series["evidence_refs"])
    for key in ["play_evidence", "completion_evidence", "recent_open_evidence"]:
        needed.update(facts["behavior"][key])
    for ref in needed:
        if ref in facts["evidence_index"]:
            evidence[ref] = facts["evidence_index"][ref]
        elif ref.startswith("goal_rule:"):
            gid = ref.split(":", 1)[1]
            goal = next((g for g in assessments if g["goal_id"] == gid), {})
            evidence[ref] = {"source": "goals.json", "version": goal.get("version"), "known_at": goal.get("rule_effective_at") or "2026-09-30T00:00:00Z"}
        elif ref.startswith("episodes:"):
            evidence[ref] = {"source": "episodes.csv", "content_slug": ref.split(":", 1)[1]}
        elif ref.startswith("goal_commitment:"):
            evidence[ref] = {"source": "durable_goal_ledger", "goal_instance_id": (active_goal or {}).get("goal_instance_id"), "known_at": (active_goal or {}).get("started_at")}
    for candidate in candidates:
        evidence[candidate["evidence_ref"]] = {"source": "catalogue.csv", "title_id": candidate["title_id"], "availability_checked_at": _iso(now)}
    evidence["aggregate:historical_popularity"] = {
        "source": "deduplicated_historical_play_started_counts", "known_at": _iso(_dt(_get(pack, "popularity_cutoff"))),
        "resolution": "Counts are keyed by content_slug and frozen before replay start.",
    }
    facts.pop("evidence_index", None)
    # Bound source excerpts in the prompt; complete events remain in the ledger.
    facts["behavior"]["unfinished_series"] = facts["behavior"]["unfinished_series"][:8]
    return {
        "schema_version": "context-v1", "as_of": _iso(now), "state_revision": 0,
        "viewer_ref": facts["user_id"], "trigger": {k: trigger.get(k) for k in ["event_id", "event", "received_at", "deliver_at", "session_id"]},
        "facts": facts, "eligible_goals": eligible, "goal_assessments": assessments,
        "active_goal": deepcopy(active_goal), "active_goal_viable": active_viable,
        "active_goal_invalid_reason": active_invalid_reason, "prior_actions": deepcopy(valid_actions[-5:]),
        "candidates": candidates, "constraints": constraints, "evidence_index": evidence,
        "assumptions": [facts["observation_coverage"]["assumption"], "Phone buffering delta estimates event time only when sent/occurred share a clock offset."],
    }


def verify_proposal(pack: Any, context: dict, proposal: dict, now: datetime,
                    prior_actions: list[dict], withdrawn: dict, arm: str) -> dict:
    """Independently reject unsupported goals, references, content and permission.

    Atomic proposal semantics: any failed check suppresses all proposed actions.
    The caller may then submit a smaller independently verified fallback.
    """
    now = _dt(now)
    assert now
    failed: list[str] = []
    passed: list[str] = []
    output: list[dict] = []
    facts = context.get("facts", {})
    constraints = context.get("constraints", {})
    candidates = {c["title_id"]: c for c in context.get("candidates", [])}
    evidence = context.get("evidence_index", {})
    if not isinstance(proposal, dict):
        return {"passed": [], "failed": ["proposal_not_an_object"], "actions": [], "goal": None}
    gid = proposal.get("goal_id")
    transition = proposal.get("goal_transition")
    goal = next((g for g in context.get("eligible_goals", []) if g.get("goal_id") == gid), None)
    actions = proposal.get("actions") or []
    if not isinstance(actions, list):
        return {"passed": [], "failed": ["actions_must_be_a_list"], "actions": [], "goal": None}
    if arm != "agent":
        if actions:
            failed.append("holdout_no_actions")
        else:
            passed.append("holdout_no_actions")
    if not isinstance(transition, str) or transition not in {"keep", "commit", "replace", "none"}:
        failed.append("invalid_goal_transition")
    if transition == "none":
        if gid is not None or actions:
            failed.append("no_goal_cannot_publish_actions")
        if not proposal.get("no_action_reason"):
            failed.append("abstention_reason_required")
    elif goal is None:
        failed.append("goal_not_proven_eligible")
    else:
        passed.append("eligibility")
        active = context.get("active_goal") or {}
        active_id = active.get("goal_id") or active.get("id")
        if transition == "keep" and active_id != gid:
            failed.append("keep_requires_same_active_goal")
        if transition == "commit" and active_id:
            failed.append("commit_cannot_replace_active_goal")
        if transition == "replace" and not active_id:
            failed.append("replace_requires_active_goal")
        if transition == "replace" and active_id == gid and _active_continuation(active, facts, now):
            failed.append("cannot_restart_same_active_goal_window")
        if gid == "G07":
            allowed_targets = set(goal.get("target_options") or [goal.get("target_slug")])
            target = proposal.get("target_slug") or goal.get("target_slug")
            if transition == "keep" and target != active.get("target_slug"):
                failed.append("keep_cannot_change_anchored_series")
            if not isinstance(target, str) or target not in allowed_targets:
                failed.append("resume_target_not_supported")
            else:
                goal = deepcopy(goal)
                goal["target_slug"] = target
    refs = proposal.get("evidence_refs") or []
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        failed.append("evidence_references_must_be_strings")
        refs = []
    if transition != "none" and not refs:
        failed.append("evidence_references_required")
    if any(ref not in evidence for ref in refs):
        failed.append("unknown_evidence_reference")
    else:
        passed.append("evidence_references")
    if not isinstance(proposal.get("reason_summary"), str) or not proposal.get("reason_summary"):
        failed.append("reason_summary_required")
    confidence = proposal.get("confidence") or {}
    if not isinstance(confidence, dict):
        confidence = {}
    if not isinstance(confidence.get("level"), str) or confidence.get("level") not in {"high", "medium", "low"} or not isinstance(confidence.get("reason"), str) or not confidence.get("reason"):
        failed.append("confidence_explanation_required")
    dialect = facts.get("dialect")
    if actions and dialect not in VALID_DIALECTS:
        failed.append("current_dialect_unknown_or_unsupported")
    else:
        passed.append("dialect")
    if len(actions) > 3:
        failed.append("too_many_actions")
    seen_types = set()
    outbound_count = 0
    day = _day(now)
    previous_outbound = [a for a in prior_actions if a.get("type") in {"push", "whatsapp"} and _dt(a.get("send_at") or a.get("published_at")) and _day(_dt(a.get("send_at") or a.get("published_at"))) == day and a.get("status") != "cancelled"]
    previous_inapp = [a for a in prior_actions if a.get("type") == "in_app_message" and _dt(a.get("published_at")) and _day(_dt(a["published_at"])) == day and a.get("status") != "cancelled"]
    quiet = now.astimezone(IST).hour >= 22 or now.astimezone(IST).hour < 8
    intr = _get(pack, "interventions", {})
    row_cfg = intr.get("home_page", {}).get("rows", {})
    rowtypes = set(row_cfg.get("row_types", constraints.get("row_types", [])))
    min_row, max_row = row_cfg.get("min_titles_per_row", 3), row_cfg.get("max_titles_per_row", 10)
    max_rows = row_cfg.get("max_rows", 6)
    templates = {t["template_id"]: t for t in intr.get("whatsapp", {}).get("approved_templates", [])}
    unfinished = {s["content_slug"] for s in facts.get("behavior", {}).get("unfinished_series", [])}

    def checked_title(tid: Any, local_dialect: Any = None) -> dict | None:
        if not isinstance(tid, str):
            failed.append("title_id_must_be_a_string")
            return None
        title = _catalogue(pack).get(tid)
        if title is None:
            failed.append("unknown_title")
            return None
        if tid not in candidates:
            failed.append("title_not_in_validated_candidate_set")
        if title.get("dialect") != dialect or (local_dialect is not None and local_dialect != dialect):
            failed.append("wrong_dialect_content")
        if not title_available(title, now, withdrawn):
            failed.append("title_unavailable_at_publication")
        return title

    for action in actions:
        if not isinstance(action, dict):
            failed.append("action_not_an_object")
            continue
        kind = action.get("type")
        if not isinstance(kind, str) or kind not in {"home_page", "push", "whatsapp", "in_app_message"}:
            failed.append("unsupported_action_type")
            continue
        if kind in seen_types:
            failed.append("duplicate_action_type")
        seen_types.add(kind)
        if kind == "home_page":
            page_dialect = action.get("dialect") or dialect
            if page_dialect != dialect:
                failed.append("wrong_page_dialect")
            seen_slugs = set()
            hero_id = action.get("hero_title_id")
            hero = checked_title(hero_id, page_dialect) if hero_id else None
            if hero:
                seen_slugs.add(hero["content_slug"])
            rows = action.get("rows") or []
            if not isinstance(rows, list):
                failed.append("rows_must_be_a_list")
                rows = []
            if len(rows) > max_rows:
                failed.append("too_many_rows")
            normalized_rows = []
            for row in rows:
                if not isinstance(row, dict):
                    failed.append("row_must_be_an_object")
                    continue
                ids, rowtype = row.get("title_ids") or [], row.get("row_type")
                if not isinstance(ids, list):
                    failed.append("row_title_ids_must_be_a_list")
                    ids = []
                if not isinstance(rowtype, str) or rowtype not in rowtypes:
                    failed.append("unsupported_row_type")
                if not min_row <= len(ids) <= max_row:
                    failed.append("row_title_count")
                titles = []
                for tid in ids:
                    title = checked_title(tid, page_dialect)
                    if title:
                        if title["content_slug"] in seen_slugs:
                            failed.append("duplicate_content_slug_on_page")
                        seen_slugs.add(title["content_slug"])
                        titles.append(title)
                if rowtype == "next_episode" and any(t["content_slug"] not in unfinished for t in titles):
                    failed.append("next_episode_without_progress")
                if rowtype == "new_releases" and any(not is_recent_release(t, now) for t in titles):
                    failed.append("new_release_row_contains_nonrecent_title")
                if rowtype == "because_you_watched" and not facts.get("behavior", {}).get("play_count"):
                    failed.append("because_you_watched_without_play_evidence")
                normalized_rows.append({"row_type": rowtype, "title_ids": ids})
            if not hero_id and not rows:
                failed.append("empty_home_page")
            output.append({"type": kind, "dialect": page_dialect, "hero_title_id": hero_id, "rows": normalized_rows})
        else:
            title = checked_title(action.get("title_id"))
            if kind in {"push", "whatsapp"}:
                outbound_count += 1
                if quiet:
                    failed.append("quiet_hours")
                if previous_outbound or constraints.get("messaging_allowance", 0) <= 0 or outbound_count > 1:
                    failed.append("combined_daily_message_cap")
                consent_key = "notifications_opt_in" if kind == "push" else "whatsapp_opt_in"
                if facts.get(consent_key) is not True:
                    failed.append("missing_channel_consent")
            elif previous_inapp:
                failed.append("in_app_daily_cap")
            if title is None:
                continue
            canonical = _canonical_copy(title)
            if kind in {"push", "in_app_message"}:
                text = action.get("text") or {}
                if not isinstance(text, dict):
                    failed.append("text_must_be_an_object")
                    text = {}
                supplied_title = action.get("text_title") or text.get("title")
                supplied_body = action.get("text_body") or text.get("body")
                if supplied_title is not None and supplied_title != canonical["title"]:
                    failed.append("unverified_generated_copy")
                if supplied_body is not None and supplied_body != canonical["body"]:
                    failed.append("unverified_generated_copy")
                copy_cfg = intr.get("push", {}).get("copy", {})
                if len(canonical["title"]) > copy_cfg.get("title_max_chars", 40) or len(canonical["body"]) > copy_cfg.get("body_max_chars", 120):
                    failed.append("copy_length")
                all_text = (canonical["title"] + " " + canonical["body"]).lower()
                if any(phrase.lower() in all_text for phrase in copy_cfg.get("forbidden_phrases", [])):
                    failed.append("forbidden_copy_phrase")
                output.append({"type": kind, "title_id": title["title_id"], "text": canonical, "deeplink": f"ott://title/{title['title_id']}"})
            else:
                template_id = action.get("template_id")
                template = templates.get(template_id) if isinstance(template_id, str) else None
                if not template:
                    failed.append("unapproved_whatsapp_template")
                    continue
                if template.get("language") != facts.get("language"):
                    failed.append("whatsapp_template_language_mismatch")
                raw_variables = action.get("variables") or {}
                if not isinstance(raw_variables, dict):
                    failed.append("whatsapp_variables_must_be_an_object")
                    raw_variables = {}
                variables = {k: v for k, v in raw_variables.items() if v is not None}
                expected = {}
                if template_id == "wa_recommend_title_v1":
                    expected = {"title_name": title["title"], "dialect_name": title["dialect"]}
                elif template_id == "wa_trial_ending_v1":
                    end = _dt(facts.get("plan_ends_at"))
                    monthly = next((p for p in _get(pack, "pricing", {}).get("plans", []) if p.get("plan_id") == "P_MONTH"), None)
                    if facts.get("subscription_status") != "trial_active" or not end or not now < end <= now + timedelta(hours=24) or not monthly:
                        failed.append("trial_ending_claim_not_supported")
                    else:
                        expected = {"plan_price": f"INR {monthly['price_inr']}"}
                else:
                    # Episode inventory lacks release timestamps, so "new episode" cannot be proven.
                    failed.append("new_episode_claim_not_observable")
                if variables and variables != expected:
                    failed.append("whatsapp_variables_not_grounded")
                output.append({"type": kind, "template_id": template_id, "variables": expected, "title_id": title["title_id"]})
    if gid == "G07" and output and goal:
        target = goal.get("target_slug")
        promoted = []
        for action in output:
            if action["type"] == "home_page":
                ids = ([action.get("hero_title_id")] if action.get("hero_title_id") else []) + [tid for row in action.get("rows", []) for tid in row["title_ids"]]
                promoted.extend(_catalogue(pack).get(tid, {}).get("content_slug") for tid in ids)
            else:
                message_slug = _catalogue(pack).get(action.get("title_id"), {}).get("content_slug")
                promoted.append(message_slug)
                if message_slug != target:
                    failed.append("resume_message_not_anchored_series")
        if target not in promoted:
            failed.append("resume_plan_does_not_promote_anchored_series")
    if not failed:
        passed.extend(["availability", "content_slug_uniqueness", "consent", "frequency_cap", "quiet_hours", "copy_and_templates", "independent_verification"])
    return {"passed": list(dict.fromkeys(passed)), "failed": list(dict.fromkeys(failed)), "actions": [] if failed else output, "goal": deepcopy(goal)}
