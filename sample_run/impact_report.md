# Randomized impact report

Provider: `replay`; model: `deterministic-lifecycle-v1`. Synthetic simulator seed: `oua-practice`.

## Cohort and estimand

All randomized users with a supplied replay trigger and an observed first-trigger anchor; assignment-level analysis. Baseline includes 1746 users out of 1746 triggered users. Serving, exposure, model success, and goal selection do not remove a viewer from their assigned arm. Each viewer's first trigger delivery, recorded before agent action; identical outcome windows in both arms.

First-paid purchase uses only supported never-paid viewers at baseline (paid_at_baseline=false); known paid and unknown baseline histories are excluded before treatment. Baseline exclusions and unknowns are recorded in `impact_report.json`.

Coverage watermark: `2026-10-15T18:30:00.000Z`. Success within an incomplete window is observable; absence of success in an incomplete window remains unknown, with rate bounds. Unknowns are not silently coded as failures or removed from the randomized denominator. The 14-day metric can be censored for late triggers.

## Prespecified common outcomes

| Fixed outcome | Agent assigned | Holdout assigned | Absolute difference | Approx. 95% interval |
|---|---:|---:|---:|---:|
| any_play_7d | 580/863 (67.21%) | 571/883 (64.67%) | +2.54 pp | [-1.90 pp, +6.99 pp] |
| episode_completion_7d | 388/863 (44.96%) | 381/883 (43.15%) | +1.81 pp | [-2.85 pp, +6.47 pp] |
| first_paid_purchase_7d | 18/382 (4.71%) | 22/376 (5.85%) | -1.14 pp | [-4.32 pp, +2.05 pp] |
| three_play_days_14d | 337/863 (39.05%) | 332/883 (37.60%) | +1.45 pp | [-3.11 pp, +6.01 pp] |

Differences and intervals are in percentage points. Intervals use an unadjusted independent-binomial normal approximation; they are descriptive, with no multiplicity correction. No interval is produced where an arm is empty or followup is incomplete. A positive point estimate alone is not proof of improvement. These are outcomes in one synthetic simulator, not production effect estimates.

## Exposure and selected-goal diagnostics

Published actions: 887. Confirmed exposed pages: 761; confirmed delivered messages: 0; confirmed clicked messages: 0. Holdout actions: 0.

Selected-goal statuses: `{'failed': 653, 'succeeded': 41}`. These are lifecycle diagnostics, not causal comparisons: the agent can choose goals with different difficulty or revise them. A publication is not delivery, a page publication is not exposure, and an individual success is not proof the intervention caused it.

## Why the prior pilot headline is misleading

Historical pilot ITT: treatment 349/1912 (18.25%); holdout 387/1979 (19.56%); difference -1.30 pp, approximate 95% interval [-3.76 pp, +1.16 pp].

The internal served-vs-everyone-else headline conditions on post-randomization exposure and is selection biased; it does not justify rollout. The correct comparison keeps all users in their randomized arms. The pilot estimates a different historical policy and does not establish the new policy's impact.

## Measurement limitations

The supplied simulator omits live-only users, models ordinary title choice independently of page recommendations, does not model in-app display, and simplifies organic purchase timing. Our outcome evaluation deduplicates copied live events and reconstructed app opens, uses buffered-event time reconstruction, and preserves uncertainty. Exact recommendation/episode-continuation quality cannot be inferred from this simulator alone. Neither served-versus-unserved nor exposed-versus-unexposed rates are used as the primary effect estimate.
