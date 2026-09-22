# Architecture lock (Firstmate) — 2026-09-22

Captain approved CDP remote-drive. Open-Jev and TypeSafe Jev out. Laya deferred (not a browser).

## MVP spine (from ego-arch-pool-001)

Shared **Browser Pool Manager**:
- Hard cap **K=5** local Chromium process trees
- Agents **lease / heartbeat / release** — never own a permanent browser PID
- **Space** = dedicated Chromium `user-data-dir`
- Prefer **one process tree per slot** (not 16 always-on; not one global multi-context browser)
- Local CDP first; cloud overflow later behind the same lease API
- Warm pool **W=1**
- Soft-evict idle ~**5 min** (lease must heartbeat; LLM-think keeps heartbeat)
- Attach-to-real-Chrome = **stretch**, not MVP
- Same-trust tenancy (one captain / all agents)
- Linux-first (Firstmate box); Win/Mac follow
- Driver: thin in-house CDP service + Playwright `connectOverCDP` for tests

## Explicit non-goals (MVP)

Electron, CEF/Tauri embed, Chromium fork, Jev/Open-Jev, Laya-as-core, packing all agents into one Chromium.
