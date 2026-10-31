# One User, One Agent

A runnable, evidence-driven lifecycle agent for the supplied OTT assignment. Each viewer has one durable owner, one active goal/plan, and an inspectable history. The OpenAI planner chooses among evidence-supported goals and interventions; independent code verifies every publication. A keyless replay provider runs the same decision contract for evaluation and fault testing.

Includes a browser dashboard, HTTP API and the original one-command CLI evaluation. A separate OTT frontend, billing service, push account, or WhatsApp account is not required.

## Clone this repository

```bash
git clone https://github.com/Eswar021/stage-ott-agent.git stage_ott_agent
cd stage_ott_agent
uv sync --extra dev --frozen
```

The supplied assignment data pack is an external input and is **not included in this repository**. Place the original, unchanged `one_user_one_agent_pack/` at this sibling location before starting the dashboard or a replay:

```text
your-workspace/
  stage_ott_agent/                 # this repository
  my_analysis/
    one_user_one_agent_pack/       # supplied data, config, replay, pilot and simulator
```

CLI replay accepts another location with `--pack /path/to/one_user_one_agent_pack`; the dashboard uses the sibling path above. The automated tests use local fixtures and can run without the supplied pack or an API key: `.venv/bin/python -m pytest -q`.

The repository includes source, tests, documentation, a dependency lock and curated `sample_run/` submission evidence. `.env`, `.venv/`, runtime databases and personal `runs/`/`live_demo/` output stay local. For paid inference on a fresh clone, create a local `.env` from `.env.example` and supply your own key; offline replay needs no key. Preserve an existing `.env` rather than overwriting it.

## Open the dashboard

```bash
cd stage_ott_agent
uv sync --extra dev --frozen
.venv/bin/python run.py serve
```

Visit **http://127.0.0.1:8000**. Review saved decisions, preview approved homepages, inspect real model responses and independent checks, copy/download the required JSON, run individual events, and view evaluation reports without navigating output folders. API docs: **http://127.0.0.1:8000/docs**. Stop with Ctrl+C; accepted jobs and viewer state are retained.

If another API runtime owner is reported, the server is already running: use its existing dashboard, or stop it with Ctrl+C in its original terminal before restarting. Changing `--port` does not permit a second owner of the same runtime. See [API.md](API.md) for startup troubleshooting.

Saved results are free to browse. Playground defaults to offline replay; its explicitly labelled OpenAI option shares the existing spending limits. A supplied-trigger sandbox preserves the source timeline, while new live events use server time and separate state. Full evaluations run as background jobs. See [API.md](API.md) for a reviewer walkthrough, request examples, endpoint contracts and local deployment limits.

## CLI/API commands and result locations

The brief's section 8 (page 5) asks for clear setup instructions and one command that replays the trigger script, and lists `README.md` in its submission layout. This README is the entry point; [API.md](API.md#copy-and-paste-curl-walkthrough) contains the complete curl walkthrough. All paths below are relative to `stage_ott_agent/`; run commands from that directory.

| Workflow                           | Start it with                                                 | Server-side results                                                |
| ---------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------ |
| CLI full offline replay            | `python run.py replay --out runs/cli_replay_01`             | `runs/cli_replay_01/`                                            |
| CLI bounded OpenAI demo            | `python run.py demo --decisions 3 --out runs/cli_openai_01` | `runs/cli_openai_01/`                                            |
| API full offline/hybrid evaluation | `POST /replays`                                             | `runs/api_<job_id>/`                                             |
| API one supplied-trigger demo      | `POST /demo/decisions`                                      | `runtime/api/demos/<job_id>/state.sqlite3` and `run_meta.json` |
| API individual new events          | `POST /events`                                              | `runtime/api/viewers/<viewer_id>/state.sqlite3`                  |

CLI runs and **full API evaluations** export the same artifact types: `decision_summaries.jsonl`, `decisions.jsonl`, `actions.jsonl`, `goals.jsonl`, `outcomes.jsonl`, `viewer_state/`, `state.sqlite3`, simulator output under `sim/`, and evaluation reports. They do not overwrite each other's run folders. Full API jobs return their folder as `result.run_id` when complete.

**Individual API events and source-trigger demos do not automatically export a full JSONL/report set.** Their facts, decisions, actions and goals are durable in SQLite; read them through `/decisions`, `/viewers` and the dashboard, or save an HTTP response as JSON. They do not run the outcome simulator. Source sandboxes use `run=demo:<job_id>`; live state uses `run=live:<viewer_id>`.

API job status, accepted event payloads and local admin controls are stored in `runtime/api/jobs.sqlite3`. CLI and API OpenAI requests share `runtime/inference_budget.sqlite3`. Runtime folders survive server restart and are ignored by Git; curated submission evidence remains in `sample_run/`.

Use the exact interpreter below if the virtual environment is not activated. Choose a new output name for a fresh CLI run; use the original command plus `--resume` to recover/reopen an existing run without repeating completed actions or model calls. The reviewed `sample_run/` is already populated, so leave it intact while experimenting.

## Setup

Use Python **3.11 or newer**. The macOS system Python may be older; use the project environment explicitly.

```bash
cd stage_ott_agent
uv venv --python 3.14
uv pip install --python .venv/bin/python -e '.[dev]'
```

Any supported Python >=3.11 can replace 3.14. With an existing suitable interpreter, `python -m venv .venv` and `.venv/bin/python -m pip install -e '.[dev]'` are equivalent. Package installation requires network access; a configured replay run does not.

`uv.lock` pins the complete dependency graph. `uv sync --extra dev --frozen` recreates the tested environment from that lock. The local environment has already been installed and verified.

The supplied pack remains outside this project at `../my_analysis/one_user_one_agent_pack`. Commands accept a pack path so a checkout need not reproduce the developer's absolute directories.

## One-command replay

From this directory:

```bash
.venv/bin/python run.py replay --pack ../my_analysis/one_user_one_agent_pack --out runs/cli_replay_01
```

The replay imports history, merges trigger/live/control deliveries, produces decisions and actions, invokes the **unmodified supplied simulator**, judges goals, and writes latency and randomized-impact reports. It does not require an OpenAI key. Replay mode means a documented deterministic planning policy; its results are not represented as real-model quality or latency.

Startup prints progress while loading the 142,906 historical deliveries, then shows ledger preparation, replay, simulation and report phases. Let the command finish at `Run complete`. Ctrl+C exits cleanly: if interrupted during loading, rerun the same command; if a state ledger was created, add `--resume` with the same options/output directory.

Inspect and verify the resulting run:

```bash
.venv/bin/python run.py verify --run runs/cli_replay_01
.venv/bin/python run.py inspect --run runs/cli_replay_01 --user U-02344
.venv/bin/python run.py inspect --run runs/cli_replay_01 --user U-02344 --out runs/cli_replay_01/viewer_U-02344.json
.venv/bin/python -m pytest
```

Use `run.py --help` or a subcommand's `--help` for available options. The installed console entry point is `ott-agent`. CLI commands and API background replays use the same core decision and verification code.

## Live OpenAI demonstration

Keep `OPENAI_API_KEY` in the local `.env` file; it is ignored by version control. `.env.example` contains credential-free settings. Preserve an existing `.env` rather than overwriting it during setup.

```bash
.venv/bin/python run.py demo --decisions 3 --out runs/cli_openai_01
```

The demo processes the full supplied trigger stream, permits at most three meaningful live planning attempts in this example, then uses explicitly recorded replay fallback for the rest. Change `--decisions 3` to `--decisions 12` for the larger demonstration. Holdouts, duplicates, unknown dialects, and other safe gates need no model call. An attempt can abstain, time out, hit a context/cost limit, or be rejected by verification. Therefore `--decisions` is not a promise of that many successful paid decisions. The run is **mixed OpenAI/replay**, not a complete OpenAI-policy impact experiment. Results are in the specified `--out` directory, just like offline replay.

Default live model: `gpt-6-luna`, low reasoning effort, Responses API, strict structured proposal, and bounded read-only function tools. The model is configurable, but an unreviewed model price fails closed rather than silently spending more. Current reviewed Standard short-context prices are USD 0.10/M input and USD 0.50/M output; budget reservations conservatively allow cache-write charges. See [OpenAI&#39;s model page](https://developers.openai.com/api/docs/models/gpt-6-luna) and [pricing](https://developers.openai.com/api/docs/pricing).

Local paid-inference ceilings are **USD 0.50 per run and USD 1.00 cumulative development spend initially**. The persistent budget ledger reserves before a call and retains uncertain charges after failures. Limits cover this application's ledger, not unrelated spending on the same OpenAI account. Output/reasoning, tool continuations, and retries count. Routine full replays and injected model faults remain offline. Live context is bounded, initially to 16,000 tokens including schemas, and output to 1,800 tokens per request.

The user confirmed with the assignment team that this dummy pack may be sent to external providers; this session clarification overrides the supplied pack's retention restriction for this work. The key is never part of model context or exported artifacts. `store:false` avoids Responses application-state persistence; it is not claimed to enable Zero Data Retention by itself.

## Start a full evaluation with curl

Keep the API running in one terminal:

```bash
.venv/bin/python run.py serve
```

In a second terminal, also inside `stage_ott_agent/`, submit a free full replay:

```bash
API_BASE_URL=http://127.0.0.1:8000
API_REPLAY_JOB=$(curl -fsS -X POST "$API_BASE_URL/replays" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-offline-replay-01' \
  -d '{"provider":"replay","decisions":3}' \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

curl -fsS "$API_BASE_URL/jobs/$API_REPLAY_JOB" | .venv/bin/python -m json.tool
```

The POST returns immediately; repeat the final GET until `status` is `SUCCEEDED` or `FAILED`. On success the folder is `runs/api_$API_REPLAY_JOB/`. To start the bounded **paid OpenAI** equivalent, use `{"provider":"openai","decisions":3}` with a different idempotency key, such as `readme-openai-replay-01`. Here `decisions` limits live attempts; the entire trigger stream is still evaluated. In offline mode it does not limit the replay length.

For **one supplied-trigger decision**, **one new live event**, and curl commands to read/save their decision outputs, use [the copy-and-paste walkthrough](API.md#copy-and-paste-curl-walkthrough). The OpenAI key stays in the server's local `.env`; no key is needed in curl commands.

## What to review

| Artifact                           | Purpose                                                                                                                      |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `decision_summaries.jsonl`       | Compact assignment-shaped records, including content from approved actions; full provenance remains in the detailed ledger   |
| `decisions.jsonl`                | Every trigger's evidence, selected goal/plan or abstention, provider result, checks, conflicts, budgets, and measured timing |
| `actions.jsonl`                  | Concrete publications in the simulator's input schema, linked to decisions and goal commitments                              |
| `outcomes.jsonl`                 | Goal judgments with rule versions, success evidence, coverage and descriptive exposure links                                 |
| `viewer_state/`                  | Durable per-viewer facts, current goal/plan, history and observed learning exports                                           |
| SQLite ledger in the run directory | Authoritative event/state/decision/action/control/job records for resumability and audit                                     |
| `impact_report.md` / `.json`   | Common-outcome randomized intention-to-treat comparisons, uncertainty, baseline exclusions, pilot correction and diagnostics |
| `latency_report.json`            | Measured processing plus logical queue wait, call counts and provider usage                                                  |
| `final_report.md`                | Reconciled run counts, reliability/safety checks, and scope of claims                                                        |
| Simulator outputs                  | Generated behavior and the supplied simulator's own acceptance/policy report                                                 |

Run reports are evidence for that run. Read their provider, seed, completeness, coverage and failure counts before interpreting results. A live sample demonstrates functionality; it does not establish statistically credible uplift. Selected-goal achievement and exposed-only rates are diagnostics, not the primary causal comparison.

To export a full, pretty-printed audit for one viewer, use `python run.py inspect --run runs/my_openai_demo --user U-02344 --out runs/my_openai_demo/viewer_U-02344.json`. Within each detailed decision, `provider_result.proposal` is the original model response; `proposal` is the final verified/repaired proposal. The published payload is linked in `actions.jsonl`. Qualitative confidence includes a level and explanation rather than an invented numeric probability. `PUBLISHED` means an action was issued to the simulated app; goal success is judged separately in `outcomes.jsonl`.

## Design and scope

- [ARCHITECTURE.md](ARCHITECTURE.md): ownership, context engineering, verification, replay timing and recovery.
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md): deliberate exclusions and simulator/measurement constraints.
- [AI_COLLABORATION.md](AI_COLLABORATION.md): how AI-assisted work was directed, reviewed and corrected.

The agent supports measurable goals G01, G02, G03, G04, G05, G07, G08, G09 and G12 when evidence establishes eligibility. G06 is disabled because "recently lapsed" is undefined; G10 has no measurable success condition; G11 requires an unavailable TV signal. Eligibility may also be unknown for a particular viewer. Abstention is a valid recorded result.

Source data and the simulator are treated as immutable. No production-scale throughput, real messaging delivery, or production causal-effect claim is implied by this assignment implementation.

## Recorded verification: 31 October 2026

| Check                   | Recorded result                                                                                                                                               |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Automated suite         | 109 tests passed, including HTTP jobs, live publication refresh, concurrent viewers, recovery, source conflicts, budget and export tampering                  |
| Complete offline replay | 2,769 trigger decisions for 1,746 viewers; 887 published actions; zero holdout actions, independent validation issues or simulator policy problems            |
| Goal correction         | G03 correction updated 26 already-open commitments without resetting their windows                                                                            |
| Actual OpenAI sample    | 12 planning attempts, 20 API requests, 8 read-only tool calls; 1 bounded timeout, 2 safe fallbacks, 1 verified layout repair; 1 deliberate no-action decision |
| Live-only latency       | Median 6.7 seconds; maximum 12.0 seconds; all within the 60-second decision budget                                                                            |
| API budget              | Final live sample conservatively accounted at USD 0.023773; all development requests including probes/uncertain reservations at USD 0.080183                  |
| Additional checks       | Completed-run resume repeated no action/model call; a second simulator seed reported zero policy problems; supplied pack fingerprint unchanged                |

Start with [sample_run/final_report.md](sample_run/final_report.md), [verification_report.json](sample_run/verification_report.json), [viewer_walkthrough.md](sample_run/viewer_walkthrough.md), and [live_summary.json](sample_run/live_summary.json). [live_examples.jsonl](sample_run/live_examples.jsonl) preserves twelve actual OpenAI contexts, proposals and checks separately from keyless replay. The complete local `live_demo/` and earlier development probes are ignored by Git; compact recorded live evidence is included under `sample_run/`.

These are recorded build-verification snapshots. A new run writes its own reports and must not inherit the recorded sample's claims. For another offline run without replacing the supplied sample, use `--out runs/my_replay`. Use `--resume` for an existing completed run or its durable pending work. The deliberate timeout/invalid-proposal recoveries demonstrate that a valid runtime result does not require pretending every raw model response was successful.
