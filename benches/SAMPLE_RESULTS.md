# Sample pool benchmark results

Captured on Firstmate box during `ego-runtime-livebench-001`.

- Chrome: `Chrome/151.0.7922.169` via `/usr/bin/google-chrome-stable`
- Generated (UTC): `2026-09-22T13:33:41.204731+00:00`
- Harness: `python scripts/bench_pool.py --mode BOTH`

## LIVE (real Chrome)

| Metric | ms |
|---|---|
| cold lease | 303.522 |
| CDP ready (`/json/version`) | 59.346 |
| navigate (`https://example.com` + title) | 411.782 |
| heartbeat RTT | 0.015 |
| release (→ FREE_WARM) | 0.01 |
| warm reuse lease (same space) | 0.097 |

Navigate snapshot: title=`Example Domain`, url=`https://example.com/`, warm_reuse_same_cdp=`True`.

## MOCK (`EGO_POOL_MOCK=1`)

| Metric | ms |
|---|---|
| cold lease | 0.12 |
| heartbeat RTT | 0.006 |
| release | 0.008 |
| warm reuse lease | 0.026 |

CDP ready / navigate are N/A under MOCK (no real browser).

## Notes

- Cold lease includes launcher settle sleep (~300 ms) before return.
- Warm reuse keeps the same CDP HTTP URL after explicit release when `W≥1`.
- Machine-readable twin: [`sample.json`](sample.json).
