# Sample pool benchmark results

Captured on Firstmate box during `ego-runtime-livebench-001`.

- Chrome: `Chrome/151.0.7922.169` via `/usr/bin/google-chrome-stable`
- Generated (UTC): `2026-09-22T13:38:03.606096+00:00`
- Harness: `python scripts/bench_pool.py --mode BOTH`

## LIVE (real Chrome)

| Metric | ms | Scope |
|---|---|---|
| cold lease | 303.149 | in-process BrowserPool |
| CDP ready (`/json/version`) | 49.242 | DevTools HTTP |
| navigate (`https://example.com` + title) | 194.594 | DevTools HTTP |
| heartbeat | 0.02 | in-process BrowserPool (not HTTP API RTT) |
| release (→ FREE_WARM) | 0.014 | in-process BrowserPool (not HTTP API RTT) |
| warm reuse lease (same space) | 0.106 | in-process BrowserPool |

Navigate snapshot: title=`Example Domain`, url=`https://example.com/`, warm_reuse_same_cdp=`True`, warm_reuse_same_pid=`True` (pid=596185).

## MOCK (`SLIPSTREAM_MOCK=1`)

| Metric | ms | Scope |
|---|---|---|
| cold lease | 0.144 | in-process BrowserPool |
| heartbeat | 0.005 | in-process BrowserPool (not HTTP API RTT) |
| release | 0.01 | in-process BrowserPool (not HTTP API RTT) |
| warm reuse lease | 0.034 | in-process BrowserPool |

CDP ready / navigate are N/A under MOCK (no real browser).

## Notes

- Cold lease includes launcher settle sleep (~300 ms) before return.
- Warm reuse keeps the same CDP HTTP URL **and** `chromium_pid` after explicit release when `W≥1`.
- Heartbeat / release / lease timings are **in-process BrowserPool** method calls (not HTTP API RTT). CDP ready / navigate use DevTools HTTP.
- Machine-readable twin: [`sample.json`](sample.json).
