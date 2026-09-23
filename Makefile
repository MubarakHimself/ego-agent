# Slipstream — thin developer targets (no MCP).
.PHONY: demo doctor test

demo:
	SLIPSTREAM_MOCK=1 python3 scripts/oob_smoke.py

doctor:
	python3 -m slipstream doctor

test:
	SLIPSTREAM_MOCK=1 pytest -q -m "not live and not bench and not live_stress"
