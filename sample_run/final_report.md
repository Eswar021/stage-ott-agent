# Replay verification report

## Run

- Provider: `replay`; model: `deterministic-lifecycle-v1`.
- Replay complete: `True`; incomplete replay/cohort: `False`; live-provider sampled: `False`.
- Provider mode: Keyless deterministic replay with injected faults; not live model performance.
- Source pack: `/Users/manikantamb/_MyFiles/Work_/MyWork_Local/Shruthi_OTT_assignment/my_analysis/one_user_one_agent_pack`.
- Trigger deliveries/decisions: 2769; baseline users: 1746.
- Published actions: 887; goal commitments: 694.
- Canonical observed events: 31574 (31578 raw rows).

## Reliability and safety evidence

- Decision statuses: `{'PUBLISHED': 887, 'NO_ACTION': 1882}`.
- Action types: `{'home_page': 887}`.
- Holdout actions: 0.
- Decisions over 60 seconds: 0; over 300 seconds: 0.
- Maximum measured decision latency: 41.397 ms.
- Maximum model/tool calls per decision: 1 / 4.
- Simulator policy problems: 0.
- Independent validation summary: `{'passed': True, 'decisions_checked': 2769, 'actions_checked': 887, 'issues': [], 'issue_count': 0, 'checks': ['holdout', 'idempotency', 'timing', 'call_budgets', 'no_action_reasons', 'independent_policy_revalidation', 'daily_caps', 'proposal_export_consistency']}`.

Live OpenAI subset (separate from offline/gated decisions): `{'planning_attempts': 0, 'api_requests': 0, 'read_only_tool_calls': 0, 'provider_errors': 0, 'fallbacks': 0, 'layout_repairs': 0, 'no_action_decisions': 0, 'milliseconds': {'p50': None, 'p95': None, 'max': None}, 'conservative_cost_usd': 0}`.

The simulator's policy acceptance is not complete validation. The runtime independently checks eligibility, evidence, dialect, availability, consent, caps, text, schema and idempotency. The control log and per-decision records document corrected rules, withdrawal, faults and fallbacks. Scope/scale claims are limited to what was actually executed.

## Outcomes and impact

Goal statuses: `{'failed': 653, 'succeeded': 41}`. Read `impact_report.md` for the assignment-level common-outcome comparison and uncertainty. Goal achievement and exposure linkage are reported separately from causal impact. See `latency_report.json` for actual timing/call counts and provider usage. Live-model measurements are not replaced with offline-provider estimates.

## Audit path

Inspect `decisions.jsonl`, `actions.jsonl`, `outcomes.jsonl`, the exported viewer state, and the durable SQLite ledger to trace a viewer from evidence through verification, publication and judgment. The sample is reproducible from its documented command/provider/seed; exact wall-clock timing can vary by machine.
