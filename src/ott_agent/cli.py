from __future__ import annotations

import argparse
import json
import math
import os
import re
import runpy
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .audit import decision_summaries
from .budget import BudgetLedger
from .engine import Engine
from .outcomes import judge_goal
from .pack import Pack
from .domain import resolve_facts
from .providers import OpenAIProvider, ReplayProvider
from .reports import build_reports
from .store import Store
from .util import IST, UTC, iso, jsonl, stable_id, ts, write_json, write_jsonl
from .validation import verify_run

PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_PACK = PROJECT.parent / "my_analysis/one_user_one_agent_pack"


def _setting(name, default, kind=str):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return kind(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid configuration setting: {name}") from exc


def _line(message, **fields):
    print(json.dumps({"message": message, **fields}, default=str), flush=True)


def _export(store, out, pack=None):
    decisions, actions, goals = store.decisions(), store.actions(), store.goals()
    write_jsonl(out / "decisions.jsonl", decisions)
    write_jsonl(out / "actions.jsonl", actions)
    write_jsonl(out / "decision_summaries.jsonl", decision_summaries(decisions, actions, pack))
    write_jsonl(out / "goals.jsonl", goals)
    write_json(out / "baseline.json", store.baselines())
    write_jsonl(out / "control_log.jsonl", store.controls())
    folder = out / "viewer_state"
    folder.mkdir(exist_ok=True)
    baseline_users = set(store.baselines())
    for row in store.db.execute("SELECT user_id,payload,revision FROM viewers"):
        if row[0] in baseline_users:
            write_json(folder / f"{row[0]}.json", {**json.loads(row[1]), "revision": row[2]})
    return decisions, actions, goals


def run_replay(args):
    from dotenv import load_dotenv
    load_dotenv(PROJECT / ".env", override=False)
    _line("Loading source pack", path=str(args.pack), provider=args.provider)
    pack = Pack(args.pack, progress=lambda data: _line("Source loading progress", **data))
    out = Path(args.out).resolve()
    if out == pack.root or out.is_relative_to(pack.root):
        raise ValueError("Output must be outside the immutable supplied data pack.")
    _line("Checking source fingerprint")
    fingerprint = pack.fingerprint()
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / "state.sqlite3"
    if db_path.exists() and not args.resume:
        raise ValueError("Output already contains a run. Use --resume for idempotent recovery or choose a new --out directory.")
    store = Store(db_path)
    ledger = None
    try:
        existing = store.get_meta("configuration")
        requested = {"pack_fingerprint": fingerprint, "provider": args.provider, "seed": args.seed,
                     "live_decisions": args.live_decisions, "model": args.model,
                     "workers": args.workers, "model_timeout_s": args.model_timeout,
                     "max_model_calls": args.max_model_calls, "max_tool_calls": args.max_tool_calls,
                     "reasoning": args.reasoning, "max_input_tokens": args.max_input_tokens,
                     "max_output_tokens": args.max_output_tokens, "run_budget": args.run_budget,
                     "total_budget": args.total_budget}
        if existing and existing != requested:
            raise ValueError("Resume configuration/source differs from the durable run. Choose a new output directory.")
        with store.transaction():
            store.set_meta("configuration", requested)
        if store.get_meta("complete"):
            _line("Run already complete; no model calls or actions repeated", out=str(out))
            return 0
        run_id = store.get_meta("run_id") or stable_id("run", str(out), iso(datetime.now(UTC)))
        with store.transaction():
            store.set_meta("run_id", run_id)
        model = args.model
        if args.provider == "openai":
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                raise ValueError("OPENAI_API_KEY is not configured; use replay mode without a key.")
            ledger = BudgetLedger(PROJECT / "runtime/inference_budget.sqlite3", run_id=run_id,
                run_limit_usd=args.run_budget, total_limit_usd=args.total_budget)
            provider = OpenAIProvider(api_key=key, model=model, ledger=ledger,
                reasoning_effort=args.reasoning, max_input_tokens=args.max_input_tokens,
                max_output_tokens=args.max_output_tokens, max_model_calls=args.max_model_calls,
                max_tool_calls=max(0, args.max_tool_calls - 4))
        else:
            provider = ReplayProvider()
        started = time.perf_counter()
        _line("Preparing durable event ledger and viewer state", out=str(out))
        engine = Engine(pack, store, provider, workers=args.workers, model_timeout_s=args.model_timeout,
            max_model_calls=args.max_model_calls, max_tool_calls=args.max_tool_calls, live_decisions=args.live_decisions)
        _line("Replaying", provider=args.provider, triggers=len(pack.triggers), out=str(out))
        engine.run(progress=lambda data: _line("Replay progress", **data))
        _line("Exporting replay decisions and actions")
        decisions, actions, goals = _export(store, out, pack)
        simulator = pack.root / "simulator/simulate_outcomes.py"
        sim_out = out / "sim"
        _line("Running supplied outcome simulator", seed=args.seed)
        proc = subprocess.run([sys.executable, str(simulator), "--pack", str(pack.root), "--actions",
                               str(out / "actions.jsonl"), "--out", str(sim_out), "--seed", args.seed],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode:
            raise RuntimeError("Supplied simulator failed; check pack/actions schemas. " + proc.stderr[-1000:])
        _line("Judging goals and updating viewer memory")
        events = list(jsonl(sim_out / "outcome_events.jsonl"))
        # Read the unchanged supplied simulator's declared horizon. Inferring
        # coverage from the last event or a possibly shifted replay day overclaims.
        contract = runpy.run_path(str(simulator), run_name="ott_simulator_contract")
        coverage_end = (contract["REPLAY_START"] + timedelta(days=contract["HORIZON_DAYS"] + 1)).astimezone(UTC)
        with store.transaction():
            for event in events:
                engine._ingest(event, "simulator")
            outcomes = []
            for goal in store.goals():
                result = judge_goal(goal, engine.history[goal["user_id"]], pack, coverage_end)
                previous_status = goal["status"]
                if result.get("entry_condition_invalidated"):
                    goal["status"] = "invalidated"
                    goal["invalidation_evidence"] = result["evidence"]
                    goal["invalidation_reason"] = result["reason"]
                elif goal["status"] in {"open", "failed", "succeeded"}:
                    goal["status"] = result["status"]
                if previous_status != goal["status"] and goal.get("judgment"):
                    goal.setdefault("judgment_history", []).append({"previous_lifecycle_status": previous_status, "judgment": goal["judgment"]})
                goal["judgment"] = result
                store.put_goal(goal)
                outcomes.append(result)
            goals_by_user = defaultdict(list)
            for goal in store.goals():
                goals_by_user[goal["user_id"]].append(goal)
            for uid in store.baselines():
                state = store.viewer(uid)
                user_goals = goals_by_user[uid]
                state["goal_history"] = user_goals
                state["active_goal"] = store.open_goal(uid)
                if engine.history[uid]:
                    latest = max(engine.history[uid], key=lambda e: e["received_at"])
                    final_facts = resolve_facts(pack, engine.history[uid], latest, coverage_end)
                    final_facts.pop("evidence_index", None)
                    state["facts"] = final_facts
                    state["facts_as_of"] = iso(coverage_end)
                state["observed_outcomes"] = [{"goal_instance_id": g["goal_instance_id"], "status": g["judgment"]["status"],
                                               "evidence": g["judgment"].get("evidence", [])} for g in user_goals]
                state["learning"] = [{"goal_instance_id": g["goal_instance_id"], "observed_status": g["judgment"]["status"],
                    "evidence": g["judgment"].get("evidence", []), "note": "Observed result, not an individual causal effect."} for g in user_goals]
                if state["active_goal"] is None:
                    state["active_plan"] = None
                store.put_viewer(uid, state)
        write_jsonl(out / "outcomes.jsonl", outcomes)
        decisions, actions, goals = _export(store, out, pack)
        _line("Validating actions and writing reports")
        validation = verify_run(pack, out)
        simulator_report = json.loads((sim_out / "simulator_report.json").read_text())
        if pack.fingerprint() != fingerprint:
            raise RuntimeError("Source pack changed during run")
        provider_counts = Counter(d.get("provider_result", {}).get("provider", "unknown") for d in decisions)
        run_meta = {"application_version": __version__, "run_id": run_id, "source_pack": str(pack.root),
            "pack_fingerprint": fingerprint, "provider": args.provider, "model": getattr(provider, "model", None),
            "seed": args.seed, "coverage_end": iso(coverage_end), "partial_run": False, "replay_complete": True,
            "triggered_users_total": len({e["user_id"] for e in pack.triggers}), "control_log": store.controls(),
            "provider_summary": ledger.summary() if ledger else {"run_accounted_usd": 0, "total_accounted_usd": 0},
            "provider_decision_counts": dict(provider_counts), "live_sample_limit": args.live_decisions if args.provider == "openai" else None,
            "live_mode_note": "OpenAI on first bounded meaningful planning attempts; explicitly marked deterministic fallback thereafter." if args.provider == "openai" else "Keyless deterministic replay with injected faults; not live model performance.",
            "simulator_report": simulator_report, "validation_summary": validation,
            "wall_time_s": round(time.perf_counter() - started, 3),
            "scheduler": "Bounded logical worker pool with measured service times and logical queue waits; not a production load benchmark.",
            "call_accounting": "4 runtime tools: viewer snapshot, goal assessment, content retrieval, publication refresh; model read-only tool calls added.",
            "max_model_calls": args.max_model_calls, "max_tool_calls": args.max_tool_calls,
            "completed_at": iso(datetime.now(UTC))}
        write_json(out / "run_meta.json", run_meta)
        build_reports(pack, out, decisions, actions, goals, events, store.baselines(), run_meta)
        with store.transaction():
            store.set_meta("complete", True)
        _line("Run complete", decisions=len(decisions), actions=len(actions), goals=len(goals),
              validation_passed=validation["passed"], policy_problems=simulator_report["policy_problem_count"],
              provider_spend=run_meta["provider_summary"], out=str(out))
        return 0 if validation["passed"] and simulator_report["policy_problem_count"] == 0 else 2
    finally:
        if ledger:
            ledger.close()
        store.close()


def inspect_user(args):
    out = Path(args.run)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.user):
        raise ValueError("Invalid viewer ID")
    path = out / "viewer_state" / f"{args.user}.json"
    if not path.is_file():
        raise ValueError("Viewer not found in this run")
    state = json.loads(path.read_text())
    decisions = [d for d in jsonl(out / "decisions.jsonl") if d["user_id"] == args.user]
    actions = [a for a in jsonl(out / "actions.jsonl") if a["user_id"] == args.user]
    payload = {"viewer": state, "decisions": decisions, "actions": actions}
    if args.out:
        write_json(args.out, payload)
        _line("Viewer audit written", path=args.out)
    else:
        # Default summary avoids dumping entire contexts into the terminal.
        print(json.dumps({"user_id": args.user, "revision": state.get("revision"),
            "goals": [{"id": g["id"], "status": g["status"], "started_at": g["started_at"], "deadline": g["deadline"]}
                      for g in state.get("goal_history", [])],
            "decisions": [{"id": d["decision_id"], "at": d["published_at"], "actions": d["action"],
                           "reason": d.get("no_action_reason") or d["proposal"].get("reason_summary"),
                           "provider": d["provider_result"]["provider"], "fallback": d.get("fallback_reason")} for d in decisions],
            "actions": len(actions)}, indent=2))
    return 0


def parser():
    p = argparse.ArgumentParser(description="One User, One Agent: evidence-driven OTT lifecycle manager")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ["replay", "demo"]:
        cmd = sub.add_parser(name, help="Replay supplied stream" if name == "replay" else "Bounded live OpenAI demonstration plus explicit fallback")
        cmd.add_argument("--pack", type=Path, default=DEFAULT_PACK)
        cmd.add_argument("--out", type=Path, default=PROJECT / ("sample_run" if name == "replay" else "live_demo"))
        cmd.add_argument("--provider", choices=["replay", "openai"], default="replay" if name == "replay" else "openai")
        cmd.add_argument("--resume", action="store_true")
        cmd.add_argument("--seed", default="oua-practice")
        cmd.add_argument("--workers", type=int, default=4)
        cmd.add_argument("--model", default=_setting("OPENAI_MODEL", "gpt-6-luna"))
        cmd.add_argument("--reasoning", choices=["none", "low", "medium"], default=_setting("OPENAI_REASONING_EFFORT", "low"))
        cmd.add_argument("--model-timeout", type=float, default=12.0)
        cmd.add_argument("--max-model-calls", type=int, default=8)
        cmd.add_argument("--max-tool-calls", type=int, default=20)
        cmd.add_argument("--live-decisions", "--decisions", dest="live_decisions", type=int, default=20 if name == "replay" else 12)
        cmd.add_argument("--run-budget", type=float, default=_setting("OTT_AGENT_LIVE_RUN_BUDGET_USD", 0.50, float))
        cmd.add_argument("--total-budget", type=float, default=_setting("OTT_AGENT_LIVE_TOTAL_BUDGET_USD", 1.00, float))
        cmd.add_argument("--max-input-tokens", type=int, default=_setting("OTT_AGENT_MAX_INPUT_TOKENS", 16000, int))
        cmd.add_argument("--max-output-tokens", type=int, default=_setting("OTT_AGENT_MAX_OUTPUT_TOKENS", 1800, int))
    inspect = sub.add_parser("inspect", help="Audit a viewer's decisions and goal history")
    inspect.add_argument("--run", type=Path, default=PROJECT / "sample_run")
    inspect.add_argument("--user", required=True)
    inspect.add_argument("--out", type=Path)
    verify = sub.add_parser("verify", help="Independently revalidate an exported run")
    verify.add_argument("--run", type=Path, default=PROJECT / "sample_run")
    verify.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    serve = sub.add_parser("serve", help="Start the local API and browser review dashboard")
    serve.add_argument("--port", type=int, default=8000)
    return p


def main(argv=None):
    args = None
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT / ".env", override=False)
        args = parser().parse_args(argv)
        if args.command in {"replay", "demo"}:
            if args.workers < 1 or args.model_timeout <= 0 or not 1 <= args.max_model_calls <= 8 or not 4 <= args.max_tool_calls <= 20:
                raise ValueError("Use positive workers/timeouts and 1-8 model calls / 4-20 total tool calls.")
            if not all(math.isfinite(v) for v in (args.run_budget, args.total_budget, args.model_timeout)) or args.run_budget < 0 or args.total_budget < 0 or args.run_budget > args.total_budget or args.total_budget > 5:
                raise ValueError("Budgets must satisfy 0 <= run <= total <= USD 5.")
            if args.max_input_tokens < 1000 or args.max_output_tokens < 128 or args.live_decisions < 0 or args.reasoning not in {"none", "low", "medium"}:
                raise ValueError("Use valid token limits, reasoning effort and a nonnegative live decision limit.")
            return run_replay(args)
        if args.command == "inspect":
            return inspect_user(args)
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            import uvicorn
            from .web import create_app
            from .service import check_runtime_available
            check_runtime_available(PROJECT / "runtime/api")
            _line("Opening local review service", dashboard=f"http://127.0.0.1:{args.port}", api_docs=f"http://127.0.0.1:{args.port}/docs")
            uvicorn.run(create_app(), host="127.0.0.1", port=args.port, workers=1)
            return 0
        report = verify_run(Pack(args.pack), args.run)
        _line("Independent validation", **report)
        return 0 if report["passed"] else 2
    except KeyboardInterrupt:
        if args is not None and args.command in {"replay", "demo"}:
            if (Path(args.out) / "state.sqlite3").is_file():
                message = "Interrupted. Rerun the same command with --resume to recover recorded work."
            else:
                message = "Interrupted during source loading. No replay state was created; rerun the same command."
        else:
            message = "Interrupted."
        print("\n" + message, file=sys.stderr, flush=True)
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
