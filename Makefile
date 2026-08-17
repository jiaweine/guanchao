.PHONY: run test check
run:
	python -m uvicorn guanchao.api:create_app --factory --host 0.0.0.0 --port 8765

test:
	python -m pytest -q

check:
	python -m compileall -q guanchao
	node --check frontend/app.js
	python -m pytest -q
