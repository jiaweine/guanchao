.PHONY: run test check
run:
	python -m uvicorn guanchao.api:create_app --factory --host 0.0.0.0 --port 8765

test:
	python -m pytest -q

check:
	python -m compileall -q guanchao
	node --check frontend/app.js
	node --check frontend/app-core.js
	node --check frontend/runtime.mjs
	node --check frontend/interaction.mjs
	node --check frontend/creation.mjs
	node --test tests/test_runtime.mjs tests/test_creation.mjs
	python -m pytest -q
