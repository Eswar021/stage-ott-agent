from types import SimpleNamespace

from ott_agent.audit import decision_summaries


def record():
    return {"decision_id": "D1", "viewer_id": "U1", "trigger": {"event": "app_open"},
            "goal": {"id": "G07", "deadline": "2026-10-04T00:00:00Z"},
            "dialect": {"value": "d3", "source": "app_settings_feed", "conflicts": []},
            "confidence": {"level": "high", "reason": "Supported evidence"}, "evidence": ["event:E1"],
            "checks": {"passed": ["eligibility"], "failed": []}, "decided_in_ms": 100,
            "status": "PUBLISHED", "action_ids": ["A1"],
            "provider_result": {"provider": "openai", "model": "gpt-6-luna",
                                "proposal": {"actions": [{"hero_title_id": "REJECTED"}]}}}


def test_summary_content_comes_from_verified_publication_not_raw_llm_response():
    d = record()
    actions = [{"decision_id": "D1", "type": "home_page", "hero_title_id": "T1",
                "rows": [{"row_type": "popular_in_dialect", "title_ids": ["T2", "T3", "T4"]}]}]
    summary = next(decision_summaries([d], actions, SimpleNamespace(goals={"G07": {"name": "resume_series_3d"}})))
    assert summary["content"] == ["title:T1", "title:T2", "title:T3", "title:T4"]
    assert summary["goal"]["name"] == "resume_series_3d"
    assert summary["action"] == "HOME_PAGE"
    assert summary["dialect"]["used"] == "d3"
    assert summary["confidence"] == d["confidence"]  # Never manufacture a numeric probability.


def test_abstention_summary_has_no_unpublished_content():
    d = record()
    d.update(status="NO_ACTION", action_ids=[], no_action_reason="No supported action")
    summary = next(decision_summaries([d], []))
    assert summary["action"] == "NO_ACTION"
    assert summary["content"] == []
    assert summary["no_action_reason"] == "No supported action"
