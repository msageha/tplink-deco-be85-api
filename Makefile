.PHONY: setup
setup:
	uv sync --extra test
	@if [ -f .env ]; then \
		echo ".env already exists, skipping copy"; \
	else \
		cp .env.example .env; \
		echo "Edit .env to set USERNAME / PASSWORD."; \
	fi

.PHONY: run
run:
	uv run uvicorn main:app --reload --app-dir src --host 127.0.0.1 --port 8000

.PHONY: test
test:
	uv run coverage run -m pytest
	uv run coverage report

.PHONY: lint
lint:
	uv run ty check .
	uv run ruff check .

.PHONY: format
format:
	uv run ruff format .

# Live diagnostics against the real router (read-only). Need src on PYTHONPATH.
.PHONY: smoke
smoke:
	PYTHONPATH=src uv run python -m scripts.smoke

.PHONY: probe
probe:
	PYTHONPATH=src uv run python -m scripts.probe

.PHONY: build-image
build-image:
	docker build -t deco-be85-api:latest .

.PHONY: run-image
run-image:
	docker run --rm -p 8000:8000 --env-file .env deco-be85-api:latest

.PHONY: clean
clean:
	@find . -name '__pycache__' -type d -prune -exec rm -r {} + 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache .ty_cache htmlcov .coverage
