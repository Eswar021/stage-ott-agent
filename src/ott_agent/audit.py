"""Compact review projections of immutable decision and publication records."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy


def decision_summaries(decisions, actions, pack=None):
    grouped = defaultdict(list)
    for action in actions:
        grouped[action["decision_id"]].append(action)
    rules = getattr(pack, "goals", {}) if pack is not None else {}
    for decision in decisions:
        published = grouped[decision["decision_id"]]
        titles = []
        for action in published:
            if action.get("hero_title_id"):
                titles.append(action["hero_title_id"])
            for row in action.get("rows", []):
                titles.extend(row.get("title_ids", []))
            if action.get("title_id"):
                titles.append(action["title_id"])
        goal = decision.get("goal")
        if goal:
            gid = goal.get("goal_id") or goal.get("id")
            goal = {"id": gid, "name": rules.get(gid, {}).get("name"),
                    "deadline": goal["deadline"], "success": deepcopy(goal.get("success")),
                    "goal_instance_id": goal.get("goal_instance_id")}
        dialect = decision.get("dialect") or {}
        kinds = [a["type"].upper() for a in published]
        yield {
            "schema_version": "decision-summary-v1",
            "decision_id": decision["decision_id"], "viewer_id": decision["viewer_id"],
            "trigger": deepcopy(decision["trigger"]), "goal": goal,
            "action": kinds[0] if len(kinds) == 1 else kinds if kinds else "NO_ACTION",
            "dialect": {"used": dialect.get("value"), "source": dialect.get("source"),
                        "conflicts": deepcopy(dialect.get("conflicts", [])), "freshness_s": dialect.get("freshness_s")},
            "content": ["title:" + tid for tid in dict.fromkeys(titles)],
            "confidence": deepcopy(decision.get("confidence")),
            "evidence": deepcopy(decision.get("evidence", [])),
            "assumptions": deepcopy(decision.get("assumptions", [])),
            "uncertainties": deepcopy(decision.get("uncertainties", [])),
            "checks": deepcopy(decision["checks"]), "decided_in_ms": decision["decided_in_ms"],
            "status": decision["status"], "action_ids": list(decision.get("action_ids", [])),
            "provider": decision.get("provider_result", {}).get("provider"),
            "model": decision.get("provider_result", {}).get("model"),
            "repair": decision.get("repair"), "fallback_reason": decision.get("fallback_reason"),
            "no_action_reason": decision.get("no_action_reason"),
        }
