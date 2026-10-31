# API and browser dashboard

Start from the project directory:

```bash
uv sync --extra dev --frozen
.venv/bin/python run.py serve
```

Open **http://127.0.0.1:8000**. Interactive API documentation is at **http://127.0.0.1:8000/docs**, with the schema at `/openapi.json`. Use `serve --port 8001` if necessary. The server binds only to loopback and owns one runtime; run one process, not multiple Uvicorn workers.

If `serve` reports **“This API runtime already has an owner”**, a process already holds this project's runtime lock. Open the existing dashboard instead of launching another instance. To restart it after changes, press Ctrl+C in the terminal running that server, wait for shutdown, then run `serve` again. If you need to identify it, use `lsof -nP -iTCP:8000 -sTCP:LISTEN` and `lsof runtime/api/server.lock`. Changing the port does not bypass the shared runtime lock. A lock file left on disk after shutdown is normal: the OS releases ownership when the process exits, and the file alone does not block startup. The CLI checks for an existing owner before starting Uvicorn and reports this case without a startup traceback.

## Reviewer walkthrough

1. **Decisions:** choose a saved workspace. Existing `sample_run`, `live_demo` and completed `runs/*` folders appear automatically. If `runs/my_openai_demo` exists, the console opens its actual OpenAI records first. Filters distinguish OpenAI, offline replay and gates; a fallback badge identifies replacement of a proposal.
2. **Viewer experience:** inspect the approved hero and rows using real source title names. The visual cards are a preview, not artwork supplied by the pack or a playable OTT app. Outcome status is shown separately.
3. **Evidence & goal / Model & checks:** inspect source conflicts, eligibility, goal history, the original provider proposal, independent checks, repairs/fallbacks and provider trace.
4. **Decision JSON:** copy or download the required compact decision. Content comes only from approved actions, never rejected recommendations.
5. **Playground:** run one original supplied trigger in an isolated sandbox, or send one new live event. Offline replay is free. Selecting OpenAI and pressing the explicitly labelled button starts paid inference under the existing ledger limits. Opening or filtering saved results never calls a model.
6. **Evaluation:** inspect full-run outcomes, denominators, intervals, latency and validation. Start a complete background replay with one button. **Background jobs** shows its progress and opens the result when ready.

## Three separate workflows

| Workflow | Clock and evidence | Outcome simulation |
|---|---|---|
| New live event: `POST /events` | Actual server receipt time; stored source history plus accepted HTTP events | None; later HTTP events update goals |
| One supplied trigger: `POST /demo/decisions` | Original supplied delivery time; prior delivered viewer facts; new isolated goal/plan state | None; not included in impact reports |
| Full evaluation: `POST /replays` | Entire original delivery/control stream, using the CLI evaluator | Unmodified supplied simulator and randomized-impact reports |

The source pack is loaded once at startup. Each live viewer's history is seeded once in its durable workspace. A normal application sends **only its new event**, not the history or all replay triggers. `/events` accepts the pack's known viewer IDs; signup/customer/profile synchronization for an arbitrary production population is outside this local assignment service.

Source dates are September/October 2026. A new HTTP call today cannot turn those timestamps into a current observation window. Live mode marks coverage incomplete, treats the snapshot's current subscription status as unknown, and requires event evidence for current entitlement. Missing activity does not prove lifetime absence or inactivity. Missing later success signals cannot prove failure without a completeness watermark; live deadline judgments can be inconclusive. Explicit observed success can still close a goal. Use the source-trigger sandbox for a grounded decision demonstration on the original timeline.

## Live-event form properties

Live events append new evidence to the separate `live:<viewer>` workspace. They do not edit the supplied profile/history files or previous replay/sandbox results. In production the app/backend reports these events automatically; the playground lets a reviewer submit them manually. Derived state changes according to the evidence (for example, a paid purchase can establish an active plan; an opt-out revokes channel consent). Past facts and original deadlines remain auditable.

| Playground event | Extra properties |
|---|---|
| `app_open` | `{}` is valid; a campaign deeplink is optional |
| `login_success` | `{}` is valid |
| `play_started` | Known `title_id` and matching `episode_id` |
| `episode_completed` | Known `title_id` and matching `episode_id` |
| `purchase_completed` | Known `plan_id`, boolean `is_trial`; the example includes the supplied `price_inr` |
| `notification_opt_out` | `channel`: `push` or `whatsapp` |
| `app_dialect_changed` | Set the top-level app dialect to the new value. Example JSON mirrors it in `to`; `from` can be added when known. Conflicting `to` is rejected. |

The form fetches a read-only example and explanation when event type or dialect changes. Playback examples contain actual available catalogue/episode references, not invented IDs or recommendations. Edit examples to match the scenario being reported; they are not submitted until the run button is pressed. **Reset to event example** restores the selected type's example. Blank properties for app-open/login do not mean the whole request is empty: viewer, event type, settings, session and generated timestamps are sent separately.

`GET /events/examples?event_type=play_started&dialect=d3` exposes the same form assistance. It does not record an event, create a job or call an LLM. Reported playback/purchase/opt-out events ordinarily update state; a subsequent app-open/login can request a fresh planning decision.

## Send one new event

```http
POST /events
Content-Type: application/json

{
  "event_id": "my-app-open-001",
  "user_id": "U-02344",
  "event": "app_open",
  "session_id": "my-session-001",
  "occurred_at": "2026-10-31T12:00:00Z",
  "sent_at": "2026-10-31T12:00:00Z",
  "app_dialect": "d3",
  "app_language": "hin",
  "properties": {},
  "provider": "replay"
}
```

Generate phone timestamps when the event occurs/is sent. The sample timestamps are illustrative. The server assigns `received_at` and `deliver_at`; supplying them is rejected. Missing phone timestamps remain uncertain rather than becoming trusted occurrence times. `provider` defaults to `replay`; use `openai` explicitly for paid planning.

The response is **202 Accepted**, with `job_id`, `status` and `status_url`. Poll `GET /jobs/{job_id}` until `SUCCEEDED` or `FAILED`. A successful decision job returns:

```json
{
  "run_id": "live:U-02344",
  "viewer_id": "U-02344",
  "decision_id": "D-...",
  "mode": "live_events"
}
```

Fetch `GET /decisions/D-...?run=live%3AU-02344`. It contains `summary` (the required decision JSON), `audit` (full reasoning/evidence/provider record), approved `actions`, source title names, viewer goal history and a historical publication snapshot.

`app_open` and `login_success` can plan; other supported events update evidence and outcomes without automatically calling the LLM. A state-only job returns `decision_id: null`. A successful job can also produce `NO_ACTION`; success means the job completed, not that the agent acted or a goal succeeded.

Content events require a known `properties.title_id`. Supplied episode references must belong to that title; `episode_completed` requires `episode_id`. Purchases require a boolean `is_trial` and known `plan_id`; renewals require a known plan. An opt-out requires `properties.channel` equal to `push` or `whatsapp`. The schema lists the supported event types.

## Read the current page

`GET /viewers/U-02344/home` reads live state at **server time**. It returns an approved page or `DEFAULT` with a reason. It rechecks the 120-minute TTL, dialect, availability, withdrawals and intervening viewer evidence. It refuses a newly invalid page even while the viewer's worker is still in inference; it never returns a rejected model proposal. A default response has `page: null`, leaving default rendering to the OTT frontend.

`POST /controls/withdrawals` with `{"title_id":"T0001"}` records an immediate local-admin withdrawal for live serving. It takes effect on the next page read and before publication, without waiting for another planning trigger. Historical audit records remain intact. This does not remotely recall a page already displayed by an external client.

For a **recorded** run, `GET /viewers/{id}/home?run=sample_run&at=<publication ISO timestamp>` provides a labelled historical snapshot. The `at` override is forbidden for live serving. Using the current date to read a September/October page normally returns an expired default.

## Replay one original trigger

Search `GET /demo/triggers?viewer=U-02344`. Then:

```http
POST /demo/decisions
Content-Type: application/json
Idempotency-Key: my-source-demo-001

{"trigger_event_id":"<event_id from the supplied trigger list>","provider":"replay"}
```

This plans for only that trigger, including earlier delivered viewer facts and controls, and refreshes relevant evidence/controls that arrive during its decision window. It does not reconstruct all previous agent decisions or imply simulator followup. The resulting workspace is `demo:J-...`. The audit explicitly identifies the actual provider and any fallback.

## Run the whole evaluation

```http
POST /replays
Content-Type: application/json
Idempotency-Key: my-evaluation-001

{"provider":"replay","decisions":12}
```

Poll `/replays/{job_id}` or `/jobs/{job_id}`. Progress contains the CLI's current phase/counts. The worker runs the unchanged one-command evaluator in a separate subprocess and retains its normal durable state/resume path. Open its returned `run_id` through `/decisions` and `/reports`.

`provider: openai` uses at most `decisions` meaningful live attempts (0–20), then explicit fallback, while evaluating the full stream. This is a **hybrid policy**, not a full OpenAI-policy experiment. No manual event-by-event upload is needed.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health`, `/config` | Readiness and non-secret local settings |
| `GET /runs` | Available recorded, sandbox and live workspaces |
| `GET /decisions?run=...` | Paginated compact decisions; optional viewer/provider/status/search filters |
| `GET /decisions/{id}?run=...` | Compact summary, full audit and approved preview |
| `GET /viewers/{id}?run=...` | Durable facts, current plan and goal history |
| `GET /viewers/{id}/home` | Current live approved page or safe default |
| `GET /reports?run=...` | Evaluation reports; unavailable reports are null |
| `GET /demo/triggers` | Search original supplied triggers |
| `POST /events`, `/demo/decisions`, `/replays` | Start one of the three workflows |
| `GET /jobs`, `/jobs/{id}`, `/replays/{id}` | Recent jobs or a specific durable job |
| `POST /jobs/{id}/retry` with `{}` | Explicitly retry a failed job using retained state and the same budget ID |
| `POST /controls/withdrawals` | Invalidate a title for live serving |

Live `event_id` is its idempotency key. Resubmitting the same payload returns the original job; changing the payload under that ID returns 409. For demos/replays, reuse `Idempotency-Key` on retries; omitting it deliberately creates another independent job. Reading a job never retries inference. Accepted queued/running jobs recover after restart; failed jobs require an explicit retry. An uncertain external request may be repeated after a crash, while its prior spending reservation remains counted.

## Copy-and-paste curl walkthrough

Run these commands from `stage_ott_agent/`. They use curl and the existing Python environment, without requiring jq. Choose the workflow you want; you do not need to run every example.

### 1. Start the server

In terminal 1:

```bash
.venv/bin/python run.py serve
```

In terminal 2:

```bash
API_BASE_URL=http://127.0.0.1:8000
curl -fsS "$API_BASE_URL/health" | .venv/bin/python -m json.tool
```

### 2. Full offline evaluation (the API equivalent of CLI replay)

```bash
API_REPLAY_JOB=$(curl -fsS -X POST "$API_BASE_URL/replays" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-offline-replay-01' \
  -d '{"provider":"replay","decisions":3}' \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

curl -fsS "$API_BASE_URL/jobs/$API_REPLAY_JOB" | .venv/bin/python -m json.tool
```

Repeat the GET until `status` is `SUCCEEDED` or `FAILED`. This runs **all** supplied triggers and the simulator. No OpenAI calls are made. `decisions` is the live-attempt limit for OpenAI mode, not an offline replay length limit.

On success, the server writes `runs/api_<job_id>/`. Its `decisions.jsonl`, `decision_summaries.jsonl`, `actions.jsonl`, `outcomes.jsonl`, SQLite state, viewer exports and reports have the same format as a CLI full run. After success:

```bash
API_REPLAY_RUN=$(curl -fsS "$API_BASE_URL/jobs/$API_REPLAY_JOB" \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["result"]["run_id"])')

curl -fsS -G "$API_BASE_URL/decisions" \
  --data-urlencode "run=$API_REPLAY_RUN" \
  --data-urlencode 'viewer=U-02344' \
  | .venv/bin/python -m json.tool

curl -fsS -G "$API_BASE_URL/reports" --data-urlencode "run=$API_REPLAY_RUN" \
  | .venv/bin/python -m json.tool
```

### 3. Full evaluation with a small paid OpenAI sample

This is the API equivalent of CLI `demo --decisions 3`: the entire stream runs, with up to three meaningful OpenAI attempts and explicit gates/fallback elsewhere.

```bash
API_OPENAI_JOB=$(curl -fsS -X POST "$API_BASE_URL/replays" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-openai-replay-01' \
  -d '{"provider":"openai","decisions":3}' \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

curl -fsS "$API_BASE_URL/jobs/$API_OPENAI_JOB" | .venv/bin/python -m json.tool
```

The output folder is `runs/api_<this_job_id>/`. This is a hybrid policy, not OpenAI on every trigger. The server loads the key from `.env`; do not add it to curl.

### 4. Just one supplied-trigger decision

Search for available original triggers:

```bash
curl -fsS -G "$API_BASE_URL/demo/triggers" \
  --data-urlencode 'viewer=U-02344' --data-urlencode 'limit=5' \
  | .venv/bin/python -m json.tool
```

The following ID is an actual supplied app-open trigger for `U-02344`. First, the free offline variant:

```bash
API_DEMO_JOB=$(curl -fsS -X POST "$API_BASE_URL/demo/decisions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-source-offline-01' \
  -d '{"trigger_event_id":"9ffd7200-d9e5-4fd0-8106-bb555fdf33cb","provider":"replay"}' \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

curl -fsS "$API_BASE_URL/jobs/$API_DEMO_JOB" | .venv/bin/python -m json.tool
```

Or use one **paid OpenAI planning attempt** in its own sandbox:

```bash
API_OPENAI_DEMO_JOB=$(curl -fsS -X POST "$API_BASE_URL/demo/decisions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-source-openai-01' \
  -d '{"trigger_event_id":"9ffd7200-d9e5-4fd0-8106-bb555fdf33cb","provider":"openai"}' \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

curl -fsS "$API_BASE_URL/jobs/$API_OPENAI_DEMO_JOB" | .venv/bin/python -m json.tool
```

These sandboxes save decisions/actions/goals in `runtime/api/demos/<job_id>/state.sqlite3` and metadata in the same folder's `run_meta.json`. They do not generate the full replay's JSONL exports, outcome simulation or impact reports. Their API workspace ID is `demo:<job_id>`.

After the chosen job succeeds, fetch and optionally download its decision. The example below selects the offline sandbox; set `API_RESULT_JOB="$API_OPENAI_DEMO_JOB"` instead for the OpenAI sandbox.

```bash
API_RESULT_JOB="$API_DEMO_JOB"
API_RESULT_RUN=$(curl -fsS "$API_BASE_URL/jobs/$API_RESULT_JOB" \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["result"]["run_id"])')
API_DECISION_ID=$(curl -fsS "$API_BASE_URL/jobs/$API_RESULT_JOB" \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["result"]["decision_id"])')

mkdir -p runs/api_downloads
curl -fsS -G "$API_BASE_URL/decisions/$API_DECISION_ID" \
  --data-urlencode "run=$API_RESULT_RUN" \
  --output runs/api_downloads/decision_audit.json

.venv/bin/python -c 'import json; d=json.load(open("runs/api_downloads/decision_audit.json")); print(json.dumps(d["summary"],indent=2))'
```

The saved JSON contains `summary` (required decision output), `audit` (full context and provider result), approved `actions`, viewer history and preview data. Downloading is a client action; the server's durable SQLite copy stays in its original sandbox folder. Choose another download filename to keep multiple responses.

### 5. Submit one new live event

This example generates a fresh event ID and current phone timestamps locally, then sends only one app-open event. It uses offline planning by default.

```bash
API_EVENT_BODY=$(.venv/bin/python - <<'PY'
import json, uuid
from datetime import datetime, timezone
now = datetime.now(timezone.utc).isoformat()
print(json.dumps({
    "event_id": "curl-" + uuid.uuid4().hex,
    "user_id": "U-02344",
    "event": "app_open",
    "session_id": "curl-demo-session",
    "occurred_at": now,
    "sent_at": now,
    "app_dialect": "d3",
    "app_language": "hin",
    "properties": {},
    "provider": "replay"
}))
PY
)

API_EVENT_JOB=$(curl -fsS -X POST "$API_BASE_URL/events" \
  -H 'Content-Type: application/json' -d "$API_EVENT_BODY" \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

curl -fsS "$API_BASE_URL/jobs/$API_EVENT_JOB" | .venv/bin/python -m json.tool
```

On success, this viewer's persistent live workspace is `live:U-02344`, stored in `runtime/api/viewers/U-02344/state.sqlite3`. Read current facts and the current approved page/default:

```bash
curl -fsS -G "$API_BASE_URL/viewers/U-02344" \
  --data-urlencode 'run=live:U-02344' | .venv/bin/python -m json.tool

curl -fsS "$API_BASE_URL/viewers/U-02344/home" | .venv/bin/python -m json.tool
```

For a live decision, use the job's `result.decision_id` with `run=live:U-02344`. State-only events return a null decision ID, so read viewer state instead. Today's event may correctly yield `NO_ACTION` because the historical export does not establish current eligibility. No simulator followup or full evaluation report is created for these events.

### Retry and file behavior

- Repeat a demo/replay POST with the **same idempotency key and body** to retrieve the original job without creating another run. Use a new key for a deliberately new run.
- A live event's **event ID** is its idempotency key. Reuse the same body for a network retry. Regenerating the example body creates another new event.
- A successful HTTP acknowledgement is not a completed decision. Wait for `SUCCEEDED`; inspect `error` when status is `FAILED`. An explicit failed-job retry is `curl -fsS -X POST "$API_BASE_URL/jobs/<job_id>/retry" -H 'Content-Type: application/json' -d '{}'`.
- API jobs and accepted payloads are in `runtime/api/jobs.sqlite3`. Paid CLI/API requests share `runtime/inference_budget.sqlite3`.
- The dashboard can review completed CLI runs under `sample_run/`, `live_demo/` or `runs/*`. A CLI `--out` folder elsewhere still exists, but is not automatically discovered by this dashboard.
- Runtime files are local and ignored by Git. The curated `sample_run/` artifacts are the submission evidence. Restarting the API retains jobs and viewer state.

## Local operations and limits

Four real worker threads run background jobs. A per-viewer lock and separate viewer SQLite ledger serialize live decisions; different viewers can plan concurrently. Purchases, opt-outs and controls accepted during a model call are refreshed before publication. The registry persists accepted jobs/events before returning 202 and caps outstanding work at 128 jobs. Replay still uses its documented logical worker scheduler; neither benchmark demonstrates production capacity.

This is a **local, single-process** service. No authentication, tenant isolation, webhook signature checking, production event-bus completeness watermark, external messaging adapter or distributed ownership service is supplied. Keep it on loopback. Host/origin checks, JSON-only mutation requests and a 64 KiB body cap protect the local demo; they are not substitutes for production authentication. API keys stay server-side in `.env`. The shared persistent budget guard applies to CLI and API inference alike.

Framework implementation follows the official [FastAPI static-files guidance](https://fastapi.tiangolo.com/tutorial/static-files/). Jobs use an explicit persistent registry and worker executor rather than relying on request-scoped background callbacks.

Final live publication holds a short shared guard across inbox refresh, independent verification and ledger commit. Event/control acceptance uses the same guard and assigns receipt/effective time inside it. This closes the final refresh-to-publication race; external inference never holds that guard.
