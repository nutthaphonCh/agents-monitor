# Consumption score

`agent-monitor` does not use raw token volume as the comparison unit in its
Overall TUI or exported report. Raw provider telemetry is retained internally
for context diagnostics, but project, provider, model, session, and time-trend
rankings use a weighted consumption score.

## Default formula

```text
consumption = fresh × 1.0 + cache-read × 0.1
fresh = uncached input + cache creation + output
```

For example:

```text
Session A: 55M fresh + 250M cache-read = 80M consumption
Session B: 140M fresh                 = 140M consumption
```

Session B ranks above Session A even though Session A has more raw context
throughput.

## Configuration

The weights can be changed without modifying the aggregation code:

```sh
export AGENT_MONITOR_FRESH_WEIGHT=1.0
export AGENT_MONITOR_CACHE_WEIGHT=0.1
```

Values must be non-negative numbers. Invalid or negative values fall back to
the defaults.

## Interpretation warning

The score is a comparison heuristic. It is not any of the following:

- provider price or billed usage;
- hardware, energy, or direct compute consumption;
- latency or model efficiency;
- a claim that every provider implements caching identically.

The 0.1 cache weight is an explicit initial policy and may change as better
evidence becomes available. Reports include the active weights so exported
results remain interpretable.

## Percentages

All Overall percentages use weighted consumption as their denominator. This
includes project, provider, model, fresh/cache contribution, and trend views.

A session's displayed cache percentage is separate from its consumption share.
It is calculated across every measured request in every rollout shard:

```text
session cache percentage = total cache-read context / total input-side context
```

It never represents only the latest request. Latest and peak context remain
diagnostic measurements and do not control the session cache percentage.
