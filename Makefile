.PHONY: run test check demo

run:
	python -m uvicorn guanchao.api:create_app --factory --host 0.0.0.0 --port 8765

test:
	python -m pytest

check:
	python -m compileall -q guanchao tests
	node --check frontend/app.js
	python -m pytest

demo:
	python -m guanchao.cli
