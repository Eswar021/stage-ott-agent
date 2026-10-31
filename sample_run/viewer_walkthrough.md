# One viewer from trigger to judgment

Actual keyless replay, not an OpenAI latency example. Stored times below are UTC; calendar-day rules use IST.

1. Viewer `U-04785` sends `app_open` at `2026-10-01T05:16:28.715Z`.
2. The agent resolves dialect `d4` and subscription `never`, retaining source freshness/conflicts in the context.
3. It commits goal `G01` at `2026-10-01T05:16:28.717Z`, deadline `2026-10-02T05:16:28.717Z`. Plan: first_play_24h
4. Independent checks pass `['eligibility', 'evidence_references', 'dialect', 'availability', 'content_slug_uniqueness', 'consent', 'frequency_cap', 'quiet_hours', 'copy_and_templates', 'independent_verification']`; failed checks `[]`.
5. It publishes `home_page` at `2026-10-01T05:16:28.717Z`, hero `T0917`.
6. Goal judgment is `succeeded`; achievement time `2026-10-01T05:17:07.715Z`; evidence `['event:oua-029931']`.
7. Attribution classification is `exposure_linked`. This is an observed linkage, not proof of individual causation.

Full audit from the project directory:

```bash
.venv/bin/python run.py inspect --run sample_run --user U-04785
.venv/bin/python run.py inspect --run sample_run --user U-04785 --out runs/viewer_audit.json
```

See `live_examples.jsonl` for the separate actual OpenAI contexts, tool results, proposals and checks. See `verification_report.json` for tests, costs and exact scope.
