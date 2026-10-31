# Known limitations

- A process loss during an OpenAI request may cause that request to be attempted again on resume. The independent budget reservation survives, so uncertain spend is retained and bounded; published actions remain idempotent. Exactly-once billing across an external API and a local database is not claimed.

## Intentional product scope

This is an assignment runtime with CLI, local HTTP API, browser review dashboard, durable state and simulator-compatible actions. It does not connect to a real OTT frontend, production billing, push/WhatsApp providers, or TV devices. JSONL/API publication is concrete within this contract; it is not proof of real-world message delivery. The homepage is a preview using source title names, not a playable OTT frontend. Cloud services, vector databases, model training and microservices are outside scope.

The API is local and single-process: no authentication, production event signatures, tenant boundaries or distributed ownership. Four actual worker threads and per-viewer serialization are tested, but no production HTTP load/capacity claim is made. Keep the supplied server on loopback. Accepted work recovers after restart; explicitly failed jobs require a deliberate retry. See [API.md](API.md).

New HTTP events use actual server time, known pack viewer IDs and separate viewer state. There is no completeness watermark proving all activity since the historical export was delivered. Negative activity predicates remain unknown, stale snapshot current billing is not trusted, and missing live success signals cannot prove failure. Use the supplied-trigger sandbox for the original time/evidence context. Sandboxes have no prior agent commitments and no simulator followup, and are excluded from impact reports.

The agent is logically per viewer, sharing a worker pool and model. The local SQLite deployment is not claimed to handle ten million viewers or to survive distributed leader failures. Replay workers simulate scheduling from measured service times. They are not an actual production concurrency/load benchmark.

## Goal support and evidence

- **G06** is disabled because "recently lapsed" has no threshold.
- **G10** is disabled because "meaningful engagement" has no concrete success condition.
- **G11** is unsupported because `tv_app_open` is not an available instrumented signal.
- Other goals are available only when supported for the individual viewer. A thirty-day export cannot prove lifetime absence for older viewers; unknown history can require abstention.
- Event-time reconstruction assumes phone `sent_at` and `occurred_at` share a clock offset. It is an estimate, not a trusted actual-occurrence clock. Buffering and invalid deltas can leave eligibility/outcomes uncertain.
- Preferences are behavioral observations or hypotheses, not psychologically inferred traits. Learning is inspectable progress, goal transitions and observed response/delivery history; there is no trained causal personalization model.

## Interventions

Messages are **immediate-only**. Quiet hours and unmet constraints cause abstention rather than future scheduling. A request reserves the combined push/WhatsApp cap conservatively even if delivery later fails. Copy is grounded canonical text; creative/localized free-form copy is deliberately constrained. Approved templates requiring unavailable evidence, such as an episode's newly released timestamp, are not used merely because the template exists.

The supplied simulator has no retraction action. Withdrawals remove future candidates and invalidate affected stored plans, but an already-exported page/send cannot be represented as cancelled through that schema. The API now refuses expired, withdrawn, dialect-mismatched or evidence-invalidated pages on read, including while inference is running. This protects future API reads; it does not remotely recall a page already displayed or alter the supplied simulator's behavior.

## Simulator and measurement

The unmodified simulator is deliberately less strict than the application validator. It does not comprehensively enforce text, variables, dynamic consent, historical caps, schema or all page constraints. Its acceptance alone does not establish safe behavior.

It uses historical events without our deduplication, retains the last duplicate trigger copy, and simplifies organic purchase timing. Ordinary title/episode choice is not driven by the recommended titles; continuation quality is therefore not faithfully measured. In-app messages are queued without modeled display or behavioral effect. Page availability is checked at publication rather than each later exposure. Live-only users have no generated followup. Synthetic seeds and device-token mechanics must not be treated as viewer facts or optimized by ID.

Goal deadlines can extend beyond the supplied observation horizon, especially for late fourteen-day commitments. Those judgments remain inconclusive when no success has yet been observed. Fixed outcome reports preserve unknowns and bounds rather than silently coding missing followup as failure. Confidence intervals are descriptive independent-binomial normal approximations, unadjusted for multiple outcomes; rare outcomes and small live samples need particular caution.

Selected-goal success rates mix goals of different difficulty and can change with planning. They are diagnostics, not causal lift. The primary replay comparison uses common baseline-defined outcomes by randomized assignment. A temporal/session exposure link is not an individual causal claim. These results apply to a synthetic simulator, not production users.

## Live LLM and cost

The default `gpt-6-luna` configuration is a cost-conscious starting choice, not a claim of globally optimal decision quality. Small live runs verify integration and decision behavior; the demo uses OpenAI for a bounded sample and explicitly recorded replay fallback afterward. Its aggregate results are not a full OpenAI-policy experiment. Routine full replays use the deterministic provider, and their latency is not OpenAI latency.

The initial USD 0.50/run and USD 1.00 cumulative caps cover this local application's budget ledger, not other account usage or an authoritative account balance. Estimates conservatively include possible cache-write charges. Uncertain failures retain reservations; actual account billing may differ. An unknown model price fails closed; upgrading models requires a reviewed rate and fresh quality/latency validation.

The user confirmed external-provider use of the dummy data with the team. That authorization is recorded separately from the original pack policy. Keys stay in the local ignored `.env`; `store:false` does not itself establish Zero Data Retention.

## What to inspect before relying on a run

Read its provider/mode, seed, completeness, observation coverage, failures/fallbacks, latency and budget reports. Check the independent verifier and simulator reports together, inspect at least one viewer end to end, and run the automated suite. Limit conclusions to the features and conditions actually exercised in the recorded run.
