# AI collaboration log

The candidate used Codex to inspect the brief, audit the supplied data, plan and implement the application, and review code. The user directed the scope and explicitly required meaningful OpenAI reasoning in the core decision path while keeping infrastructure simple and API spending small.

## Direction and division of work

- Read all six brief pages, source configuration, replay controls/faults and simulator code before choosing an implementation.
- Audited full CSV/JSONL inputs for duplicate IDs, missing/stale profiles, dialect conflicts, catalogue variants, buffering and pilot measurement errors.
- Kept organized analysis in `my_analysis/cp_analysis`; implemented in the user-designated `stage_ott_agent` directory.
- Divided implementation by clear file ownership: domain facts/eligibility/verification; provider/context-tool/budget code; outcome judgment/reporting; and runtime/store/CLI integration.
- Used independent cross-review of domain, runtime and outcome code rather than accepting the first generated implementation.
- Consulted official OpenAI documentation for Responses, structured output, tool calling, model capabilities and prices. The selected live model is configurable; the budget layer rejects prices not explicitly reviewed.

## Suggestions checked, changed or rejected

| Suggestion or risk | Decision and check |
|---|---|
| Start with a mostly keyless deterministic product | User requested a real LLM at the core. OpenAI makes live goal/plan choices; replay remains an evaluator/fault adapter and explicit fallback. |
| Trust the pilot's served-versus-unserved uplift | Rejected. Recomputed randomized treatment/holdout rates from per-user rows and kept selection bias visible. |
| Let a confident model establish trial eligibility | Rejected. Code-owned eligibility and evidence-reference checks must reject the injected unsupported trial claims. |
| Fill ambiguous goals with reasonable-looking definitions | Rejected. Undefined G06, unmeasurable G10 and unobservable G11 remain unsupported instead of inventing business rules. |
| Feed the whole history or a growing model transcript into every request | Rejected. Built a bounded, provenance-bearing context from authoritative data and scoped tools. |
| Treat a publication as exposure or an individual success as causal lift | Rejected. Delivery/exposure/session linkage and randomized impact are separate outputs. |
| Restart a goal window when a model says replace/keep | Cross-review identified same-goal and nullable-target restart hazards. Goal transitions and retained windows are independently checked. |
| Ignore uncertain timestamps in absence-based eligibility | Cross-review identified that this can turn uncertainty into eligibility; negative predicates require explicit treatment of contradictory uncertain evidence. |
| Check state only before calling the model | Rejected. Controls/live events can arrive during inference; publication revalidates current state and records the changed revision. |
| Check call limits only after all tools ran | Cross-review required reserving internal read allowance before model tool execution, not merely rejecting afterward. |
| Present a logical worker replay as production load proof | Rejected. Reports describe measured service plus simulated queue wait and make no production-scale capacity claim. |
| Keep the original 6,000-token cap despite measured context size | Rejected. Initial manifests needed more room; actual tool continuations exposed further headroom needs. Tool replies were bounded to relevant evidence, and the configurable cap settled at 16,000 including schemas and continuation overhead. All paid probes remained in the cumulative ledger. |
| Replace a correct model goal because its page repeats a title | Added a small deterministic layout repair that removes duplicates/drops undersized rows, then re-verifies while preserving the model's goal. Other unsafe proposals still fall back or abstain. |
| Assume checking some expected exports proves every action was verified | Rejected after review. Bidirectional decision/action identity and exact payload reconciliation plus tampering tests now catch extra, missing or altered actions. |
| Store the active plan but omit it from the next prompt | An integration review caught this gap. The current plan and rationale are now supplied with the durable goal; a regression test checks the second decision's actual context. |
| Treat all fast gate/replay decisions as OpenAI latency evidence | Rejected. Reports expose a separate live-only timing/request/cost subset and explicitly label hybrid-policy impact results. |

## How generated work is checked

Source facts are traced to the pack, not to a model's explanation. Focused tests cover corrected goal rules, duplicate/late events, conflicting sources, tool failures, unsafe proposals, temporal windows, coverage and randomized denominators. Integration runs invoke the supplied simulator unchanged. Verification checks exported decisions/actions/state rather than relying only on a successful process exit or the simulator's incomplete acceptance rules.

Live calls must pass the local persistent budget guard. Their model, usage, cost estimates, raw structured proposals, validation results, final actions and timing remain distinguishable from replay-mode results. The final run's measured evidence belongs in `sample_run/final_report.md`, `latency_report.json`, `impact_report.md`, and the test output; this log does not substitute unrun claims for those artifacts.

## User clarifications retained

The user chose OpenAI, supplied a local key, disclosed approximately USD 5 in credits, and directed a USD 0.50/run and USD 1.00 initial cumulative development budget through the agreed plan. The user confirmed with the assignment team that the dummy pack can be sent to external providers, overriding the pack's initial retention restriction for this work. No secret value belongs in this log, prompts or exported reports.

## Executed verification

The initial CLI suite passed 90 tests. Complete keyless replay processed 2,769 deliveries and independently validated 887 actions with zero simulator policy problems. Actual OpenAI verification used 12 planning attempts through 20 API requests and 8 tools. One real request timed out within the configured deadline; an independently rejected proposal and that timeout used verified fallback. A duplicate page layout was reduced and re-verified while preserving its model-selected goal. One model decision abstained. These are not represented as perfect raw-model responses.

The measured artifacts include live-only timing, structured proposals, checks, hybrid-policy labels and cumulative spending, including earlier context-limit probes and uncertain failure reservations. All development API usage conservatively accounted to USD 0.080183, within the initial USD 1 local cap. Full replay, resume, second-seed simulation and the original source fingerprint were verified. No production uplift or load-capacity claim is made.

A final source audit found fourteen newest never-paid profiles conflicting with older paid-status versions without billing evidence. Lifetime payment is now unknown for those conflicts rather than allowing a first-paid goal; an explicit regression test covers the distinction between current status and lifetime history.

Startup follow-up: the user interrupted a silent history load with Ctrl+C. Added flushed source-loading and execution-phase progress, clean exit 130, and phase-aware rerun/resume guidance. Verified a real SIGINT during source loading, the two cancellation regression cases, and a complete offline replay without spending API credit.


## API and dashboard extension

At the user's request, added a local FastAPI service and browser review dashboard around the existing engine. The API has an individual-event adapter using actual receipt time, a source-trigger sandbox using the supplied timeline, and a separate full-replay job. It does not disguise a complete batch replay as a low-latency single-event API. Four actual worker threads and per-viewer locks preserve ownership; independent inbox refresh applies purchases/opt-outs received during inference before publication. Read-time TTL/dialect/withdrawal/evidence checks refuse invalid pages immediately. A persistent registry provides 202 acknowledgements, idempotency, restart recovery and explicit failed-job retry.

The UI distinguishes provider identity, original proposal, verifier repairs/fallbacks, approved content and later goal outcomes. Viewing saved results cannot spend inference credit. Live ingestion has no complete-observation watermark, so stale current billing and absent activity are not treated as proven facts. Interactive sandboxes are separate from causal evaluation reports.

The expanded suite passed 109 tests. A full evaluation started through HTTP independently validated all 887 actions from 2,769 decisions with zero simulator policy problems. One real OpenAI request through the HTTP sandbox returned a published decision with no failed checks, 5,964 input tokens and 770 output tokens (242 reasoning), conservatively accounted at USD 0.0011305. Application-wide accounted spend at that check was USD 0.10307575. No extra paid calls were used for browser browsing, offline HTTP tests or full offline replay. Chrome review verified recorded live proposals/checks and a completed offline playground decision. See `sample_run/api_verification.json` and `API.md`.

Live-event form follow-up: clarified append-only evidence versus source-data edits. Replaced empty playback defaults with read-only grounded title/episode examples, event-specific hints and a reset button. App-open/login retain valid empty properties; dialect changes use the top-level setting with a consistent optional `to` audit property. Playback now requires an episode reference. All seven form examples are covered through the real HTTP validator, and tests confirm purchases/opt-outs change live facts without editing the source profile. Expanded suite: 109 tests; no additional paid inference.

Startup-owner follow-up: a second CLI server launch encountered a genuinely running instance, verified by its listening socket, held OS lock and healthy endpoint. Added an early CLI ownership check, owner metadata written only after lock acquisition, and regression cases for duplicate launches and unlocked leftover files. A duplicate launch now exits with actionable guidance before Uvicorn startup, without touching the existing server/jobs. Full suite: 109 tests. No paid inference or live-state reset.
