from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import time

import pytest

from ott_agent.budget import BudgetExceeded, BudgetLedger, UnknownPricing, pricing_for
from ott_agent.providers import OpenAIProvider, Proposal, ReplayProvider, _validate_tool_call, compact_context


def context():
    return {
        "facts": {"dialect": "d2", "behavior": {}},
        "eligible_goals": [{"goal_id": "G03", "name": "second_day_return_2d", "eligibility_evidence": ["event:signup"]},
                           {"goal_id": "G02", "name": "first_full_episode_3d", "eligibility_evidence": ["event:signup"]}],
        "goal_assessments": [{"goal_id": "G03"}, {"goal_id": "G02"}],
        "active_goal": None,
        "evidence_index": {"event:signup": {"source": "event"}},
        "constraints": {"allowed_action_types": ["home_page"]},
        "candidates": [{"title_id": f"T{i}", "content_slug": f"slug{i}", "dialect": "d2"} for i in range(9)]}


def proposal():
    return ReplayProvider().plan(context())["proposal"]


class FakeResponses:
    def __init__(self, values):
        self.values = iter(values)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        result = next(self.values)
        if isinstance(result, Exception):
            raise result
        return result


def response(text=None, output=None, usage=None, status="completed"):
    return SimpleNamespace(id="response-test", output_text=text or "", output=output or [], status=status,
                           usage=usage if usage is not None else {"input_tokens": 100, "output_tokens": 50,
                                                                  "output_tokens_details": {"reasoning_tokens": 10}})


def live_provider(tmp_path, responses, monkeypatch, **kwargs):
    import ott_agent.providers as module
    monkeypatch.setattr(module, "_token_count", lambda payload, model: 100)
    ledger = BudgetLedger(tmp_path / "budget.sqlite", run_id="tests")
    fake = FakeResponses(responses)
    return OpenAIProvider(ledger=ledger, client=SimpleNamespace(responses=fake), **kwargs), fake, ledger


def test_replay_plan_is_structured_goal_choice_and_no_slug_duplicates():
    ctx = context()
    result = ReplayProvider().plan(ctx)
    p = Proposal.model_validate(result["proposal"])
    assert p.goal_id == "G03"  # recent-return branch remains reachable
    assert p.goal_transition == "commit"
    page = p.actions[0]
    ids = [page.hero_title_id] + [tid for row in page.rows for tid in row.title_ids]
    assert len(ids) == len(set(ids))
    assert all(len(row.title_ids) >= 3 for row in page.rows)
    ctx["active_goal"] = {"goal_id": "G02", "deadline": "2026-10-03T00:00:00Z"}
    assert ReplayProvider().plan(ctx)["proposal"]["goal_id"] == "G02"
    assert ReplayProvider().plan(ctx)["proposal"]["goal_transition"] == "keep"


def test_very_new_cohort_prioritizes_later_day_return_without_fixture_ids():
    ctx = context()
    ctx["eligible_goals"].append({"goal_id": "G01", "name": "first_play_24h", "eligibility_evidence": ["event:signup"]})
    result = ReplayProvider().plan(ctx)
    assert result["proposal"]["goal_id"] == "G03"
    assert "later IST day" in result["proposal"]["reason_summary"]


def test_budget_persists_and_rejects_unknown_models(tmp_path):
    path = tmp_path / "budget.sqlite"
    a = BudgetLedger(path, run_id="a", run_limit_usd=0.0002, total_limit_usd=0.0002)
    reservation = a.reserve("gpt-6-luna", input_bound=1000, output_bound=100)
    assert a.summary()["total_accounted_usd"] > 0
    a.uncertain(reservation)
    a.close()
    b = BudgetLedger(path, run_id="b", run_limit_usd=0.0002, total_limit_usd=0.0002)
    with pytest.raises(BudgetExceeded):
        b.reserve("gpt-6-luna", input_bound=1000, output_bound=100)
    with pytest.raises(UnknownPricing):
        b.reserve("unreviewed-model", input_bound=1, output_bound=1)
    b.close()


def test_atomic_budget_prevents_competing_reservations(tmp_path):
    path = tmp_path / "budget.sqlite"
    # Initialize schema before competitors; requests still reserve concurrently.
    BudgetLedger(path, run_id="init").close()

    def reserve(run):
        ledger = BudgetLedger(path, run_id=run, run_limit_usd=0.0002, total_limit_usd=0.0002)
        try:
            ledger.reserve("gpt-6-luna", input_bound=1000, output_bound=100)
            return True
        except BudgetExceeded:
            return False
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ["one", "two"])) == [False, True]


def test_usage_settlement_counts_reasoning_as_output_once(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", run_id="test")
    reservation = ledger.reserve("gpt-6-luna", input_bound=1000, output_bound=500)
    charged = ledger.settle(reservation, {"input_tokens": 100, "output_tokens": 50,
                                          "output_tokens_details": {"reasoning_tokens": 30}})
    assert charged == pricing_for("gpt-6-luna").upper_cost(100, 50)
    assert ledger.summary()["request_states"] == {"settled": 1}
    ledger.close()


def test_openai_uses_strict_output_store_false_and_settles(tmp_path, monkeypatch):
    provider, fake, ledger = live_provider(tmp_path, [response(json.dumps(proposal()))], monkeypatch)
    result = provider.plan(context())
    assert result["error"] is None
    assert result["proposal"]["goal_id"] == "G03"
    assert result["calls"] == 1
    assert result["usage"]["reasoning_tokens"] == 10
    request = fake.requests[0]
    assert request["store"] is False
    assert request["text"]["format"]["strict"] is True
    assert request["tool_choice"] == "none"
    assert request["timeout"] <= 8
    assert ledger.summary()["request_states"] == {"settled": 1}


def test_tool_round_is_bounded_and_goal_affects_final_plan(tmp_path, monkeypatch):
    tool = {"type": "function_call", "name": "read_goal_rule", "arguments": '{"goal_id":"G02"}', "call_id": "c1"}
    selected = proposal()
    selected["goal_id"] = "G02"
    seen = []
    provider, fake, _ = live_provider(tmp_path,
        [response(output=[tool]), response(json.dumps(selected))], monkeypatch)
    result = provider.plan(context(), tools=lambda name, args: seen.append((name, args)) or {"rule": "complete episode"})
    assert result["calls"] == 2
    assert result["tool_calls"] == 1
    assert result["proposal"]["goal_id"] == "G02"
    assert seen == [("read_goal_rule", {"goal_id": "G02"})]
    assert fake.requests[1]["input"][-1]["type"] == "function_call_output"


def test_tools_cannot_request_another_viewer_or_arbitrary_goal():
    with pytest.raises(ValueError):
        _validate_tool_call("read_goal_rule", {"goal_id": "G02", "user_id": "another-viewer"}, context())
    with pytest.raises(ValueError):
        _validate_tool_call("read_goal_rule", {"goal_id": "G99"}, context())
    with pytest.raises(ValueError):
        _validate_tool_call("read_viewer_evidence", {"fact_keys": [], "event_types": [], "window_days": 30, "limit": 10000}, context())


def test_context_cap_blocks_before_spending(tmp_path, monkeypatch):
    provider, fake, ledger = live_provider(tmp_path, [], monkeypatch, max_input_tokens=50)
    result = provider.plan(context())
    assert result["error"] == "context_token_budget_exceeded"
    assert fake.requests == []
    assert ledger.summary()["total_accounted_usd"] == 0


def test_runtime_remaining_call_budget_is_enforced_before_spending(tmp_path, monkeypatch):
    provider, fake, ledger = live_provider(tmp_path, [], monkeypatch)
    ctx = context()
    ctx["budget"] = {"remaining_model_calls": 0, "remaining_tool_calls": 0}
    result = provider.plan(ctx, tools=lambda *_: {})
    assert result["error"] == "model_call_budget_exceeded"
    assert result["calls"] == 0
    assert fake.requests == []
    assert ledger.summary()["total_accounted_usd"] == 0


def test_context_projection_keeps_infeasibility_and_source_proofs():
    ctx = context()
    ctx["active_goal_viable"] = False
    ctx["active_goal_invalid_reason"] = "anchored_series_unavailable"
    ctx["goal_assessments"].append({"goal_id": "G07", "status": "eligible", "reason": "unfinished",
                                     "feasible": False, "feasibility_reason": "target_not_available_in_current_dialect"})
    compact = compact_context(ctx)
    assert compact["active_goal_viable"] is False
    assert compact["active_goal_invalid_reason"] == "anchored_series_unavailable"
    assert any(g.get("feasibility_reason") == "target_not_available_in_current_dialect" for g in compact["excluded_goals"])
    assert compact["eligible_goals"][0]["eligibility_evidence"] == ["event:signup"]


def test_malformed_model_output_and_errors_are_safely_recorded(tmp_path, monkeypatch):
    provider, _, _ = live_provider(tmp_path, [response('{"bad": true}')], monkeypatch)
    result = provider.plan(context())
    assert result["proposal"] is None
    assert result["error"] == "malformed_model_output"

    provider2, _, ledger2 = live_provider(tmp_path, [RuntimeError("Do not log bearer SECRET")], monkeypatch)
    result = provider2.plan(context())
    assert result["error"] == "provider_connection_error"
    assert "SECRET" not in json.dumps(result)
    assert ledger2.summary()["request_states"]["uncertain"] == 1


def test_fixture_claim_is_passed_exactly_for_independent_rejection():
    output = {"goal_id": "G04", "eligible": True, "confidence": 0.97, "reason": "unverified billing claim"}
    result = ReplayProvider().plan(context(), fault={"fault": "unsupported_claim", "model_output": output})
    assert result["proposal"] == output
    assert result["fault_injected"] == "unsupported_claim"
    malformed = ReplayProvider().plan(context(), fault={"fault": "malformed_output", "model_output": '{"goal_id":'})
    assert malformed["error"] == "malformed_model_output"


def test_fixture_timeout_measures_real_bounded_wait():
    started = time.monotonic()
    result = ReplayProvider().plan(context(), fault={"fault": "timeout", "seconds": 45}, deadline_s=0.005)
    elapsed = time.monotonic() - started
    assert result["error"] == "model_timeout"
    assert elapsed >= 0.004
    assert elapsed < 0.5
    assert result["elapsed_s"] >= 0.004
