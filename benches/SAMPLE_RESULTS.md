# Sample pool benchmark results

Captured on Firstmate box during `ego-runtime-livebench-001`.

- Chrome: `Chrome/151.0.7922.169` via `/usr/bin/google-chrome-stable`
- Generated (UTC): `2026-09-22T13:28:47.608814+00:00`
- Harness: `python scripts/bench_pool.py --mode BOTH`

## LIVE (real Chrome)

| Metric | ms |
|---|---|
| cold lease | 305.361 |
| CDP ready (`/json/version`) | 30.779 |
| navigate (`https://example.com` + title) | 176.817 |
| heartbeat RTT | 0.02 |
| release (→ FREE_WARM) | 0.014 |
| warm reuse lease (same space) | 0.102 |

Navigate snapshot: title=`Example Domain`, url=`https://example.com/`, warm_reuse_same_cdp=`True`.

## MOCK (`EGO_POOL_MOCK=1`)

| Metric | ms |
|---|---|
| cold lease | 0.134 |
| heartbeat RTT | 0.005 |
| release | 0.008 |
| warm reuse lease | 0.029 |

CDP ready / navigate are N/A under MOCK (no real browser).

## Notes

- Cold lease includes launcher settle sleep (~300 ms) before return.
- Warm reuse keeps the same CDP HTTP URL after explicit release when `W≥1`.
- Machine-readable twin: [`sample.json`](sample.json).
