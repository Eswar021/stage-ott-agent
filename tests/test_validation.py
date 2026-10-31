"""Export tampering and budget/path boundary tests, independent of execution."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ott_agent.budget import BudgetLedger
from ott_agent.cli import main
from ott_agent.util import write_jsonl
from ott_agent.validation import verify_run
from test_engine import mini_pack, run


def exported(tmp_path):
    p = mini_pack()
    store, _ = run(tmp_path, p)
    decisions, actions = store.decisions(), store.actions()
    store.close()
    write_jsonl(tmp_path / "decisions.jsonl", decisions)
    write_jsonl(tmp_path / "actions.jsonl", actions)
    return p, decisions, actions


def test_independent_export_audit_accepts_actual_engine_run(tmp_path):
    p, _, _ = exported(tmp_path)
    assert verify_run(p, tmp_path)["passed"]


def test_unlisted_extra_action_is_rejected_even_if_other_action_matches(tmp_path):
    p, _, actions = exported(tmp_path)
    actions.append({**deepcopy(actions[0]), "action_id": "A-injected"})
    write_jsonl(tmp_path / "actions.jsonl", actions)
    result = verify_run(p, tmp_path)
    assert not result["passed"]
    assert any(x["problem"] == "undeclared_exported_action" for x in result["issues"])


def test_no_action_decision_cannot_hide_an_exported_action(tmp_path):
    p, decisions, _ = exported(tmp_path)
    decisions[0].update(action_ids=[], no_action_reason="Abstain", status="NO_ACTION")
    write_jsonl(tmp_path / "decisions.jsonl", decisions)
    assert not verify_run(p, tmp_path)["passed"]


def test_export_must_match_entire_verified_payload_and_identity(tmp_path):
    p, _, actions = exported(tmp_path)
    actions[0]["hero_title_id"] = "unverified-title"
    actions[0]["trigger_event_id"] = "unrelated-trigger"
    write_jsonl(tmp_path / "actions.jsonl", actions)
    result = verify_run(p, tmp_path)
    problems = {x["problem"] for x in result["issues"]}
    assert {"proposal_export_mismatch", "action_decision_identity_mismatch"} <= problems


def test_declared_action_missing_from_export_is_rejected(tmp_path):
    p, _, _ = exported(tmp_path)
    write_jsonl(tmp_path / "actions.jsonl", [])
    assert not verify_run(p, tmp_path)["passed"]


@pytest.mark.parametrize("limit", [float("nan"), float("inf"), -1.0])
def test_nonfinite_or_negative_budgets_cannot_disable_spending_guard(tmp_path, limit):
    with pytest.raises(ValueError):
        BudgetLedger(tmp_path / "budget.sqlite3", run_id="r", run_limit_usd=limit)


def test_output_inside_immutable_source_pack_is_rejected_before_writing(tmp_path, monkeypatch):
    pack_root = tmp_path / "pack"
    pack_root.mkdir()
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("ott_agent.cli.Pack", lambda path, **kwargs: SimpleNamespace(root=pack_root))
    out = pack_root / "should-not-exist"
    assert main(["replay", "--pack", str(pack_root), "--out", str(out)]) == 1
    assert not out.exists()
