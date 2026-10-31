"""Read-only audit of exported decisions/actions, separate from execution."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path

from .domain import verify_proposal
from .util import iso, ist_day, jsonl, ts, write_json


def verify_run(pack, out: Path) -> dict:
    decisions = list(jsonl(out / "decisions.jsonl"))
    actions = list(jsonl(out / "actions.jsonl"))
    by_id = {d["decision_id"]: d for d in decisions}
    grouped = defaultdict(list)
    for action in actions:
        grouped[action["decision_id"]].append(action)
    issues = []
    if len(by_id) != len(decisions):
        issues.append({"problem": "duplicate_decision_id"})
    if len({a["action_id"] for a in actions}) != len(actions):
        issues.append({"problem": "duplicate_action_id"})
    days = Counter()
    for action in actions:
        uid = action["user_id"]
        decision = by_id.get(action["decision_id"])
        if not decision:
            issues.append({"action_id": action["action_id"], "problem": "missing_decision"})
            continue
        if action["action_id"] not in decision.get("action_ids", []):
            issues.append({"action_id": action["action_id"], "problem": "undeclared_exported_action"})
        if action["user_id"] != decision["user_id"] or action.get("trigger_event_id") != decision["trigger_event_id"]:
            issues.append({"action_id": action["action_id"], "problem": "action_decision_identity_mismatch"})
        if action["published_at"] != decision["published_at"]:
            issues.append({"action_id": action["action_id"], "problem": "action_decision_publication_mismatch"})
        if pack.arms.get(uid) != "agent":
            issues.append({"action_id": action["action_id"], "problem": "holdout_or_unassigned_action"})
        if ts(action["published_at"]) < ts(decision["trigger"]["deliver_at"]):
            issues.append({"action_id": action["action_id"], "problem": "publication_before_trigger"})
        if (ts(action["published_at"]) - ts(decision["trigger"]["deliver_at"])).total_seconds() > 60:
            issues.append({"action_id": action["action_id"], "problem": "publication_after_deadline"})
        if action["type"] in {"push", "whatsapp", "in_app_message"}:
            if action.get("send_at") != action["published_at"]:
                issues.append({"action_id": action["action_id"], "problem": "unsupported_delayed_dispatch"})
            group = "outbound" if action["type"] in {"push", "whatsapp"} else "in_app"
            key = (uid, str(ist_day(action.get("send_at", action["published_at"]))), group)
            days[key] += 1
            if days[key] > 1:
                issues.append({"action_id": action["action_id"], "problem": "daily_message_cap"})
    for decision in decisions:
        published = grouped[decision["decision_id"]]
        declared_ids = decision.get("action_ids", [])
        actual_ids = [a["action_id"] for a in published]
        if Counter(declared_ids) != Counter(actual_ids):
            issues.append({"decision_id": decision["decision_id"], "problem": "declared_action_export_set_mismatch"})
        if decision.get("model_calls", 0) > min(8, ((decision.get("context") or {}).get("budget") or {}).get("max_model_calls", 8)):
            issues.append({"decision_id": decision["decision_id"], "problem": "model_call_budget"})
        if decision.get("tool_calls", 0) > min(20, ((decision.get("context") or {}).get("budget") or {}).get("max_tool_calls", 20)):
            issues.append({"decision_id": decision["decision_id"], "problem": "tool_call_budget"})
        if not decision.get("action_ids"):
            if not decision.get("no_action_reason"):
                issues.append({"decision_id": decision["decision_id"], "problem": "missing_abstention_reason"})
            continue
        context = decision.get("validation_context") or decision.get("context")
        if not context:
            issues.append({"decision_id": decision["decision_id"], "problem": "missing_publication_evidence"})
            continue
        now = ts(decision["published_at"])
        withdrawn = {c["title_id"]: c["at"] for c in pack.control_events
                     if c["type"] == "title_withdrawn" and ts(c["at"]) <= now}
        prior = [a for a in actions if a["user_id"] == decision["user_id"] and
                 a["decision_id"] != decision["decision_id"] and ts(a["published_at"]) <= now]
        checked = verify_proposal(pack, context, decision["proposal"], now, prior, withdrawn,
                                  pack.arms.get(decision["user_id"], "unknown"))
        if checked["failed"]:
            issues.append({"decision_id": decision["decision_id"], "problem": "independent_revalidation", "checks": checked["failed"]})
        metadata = {"action_id", "decision_id", "user_id", "trigger_event_id", "published_at", "send_at", "goal_instance_id"}
        expected = Counter(json.dumps(a, sort_keys=True, ensure_ascii=False) for a in checked["actions"])
        actual = Counter(json.dumps({k: v for k, v in a.items() if k not in metadata}, sort_keys=True, ensure_ascii=False) for a in published)
        if actual != expected:
            issues.append({"decision_id": decision["decision_id"], "problem": "proposal_export_mismatch"})
    result = {"passed": not issues, "decisions_checked": len(decisions), "actions_checked": len(actions),
              "issues": issues, "issue_count": len(issues), "checks": ["holdout", "idempotency", "timing", "call_budgets",
              "no_action_reasons", "independent_policy_revalidation", "daily_caps", "proposal_export_consistency"]}
    write_json(out / "validation_report.json", result)
    return result
