.PHONY: setup setup-env up-dev test cov lint format check precommit-install precommit smoke probe build-image run-image clean

setup:
	uv sync --extra test

setup-env:
	@if [ -f .env ]; then \
		echo ".env already exists, skipping copy"; \
	else \
		cp .env.example .env; \
		echo "Edit .env to set USERNAME / PASSWORD."; \
	fi

up-dev:
	uv run uvicorn decoapi.main:app --reload --app-dir src --host 127.0.0.1 --port 8000

test:
	uv run pytest

cov:
	uv run coverage run -m pytest
	uv run coverage report

lint:
	uv run ty check .
	uv run ruff check .

format:
	uv run ruff format .

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check .

precommit-install:
	uv run pre-commit install

precommit:
	uv run pre-commit run --all-files

# Live diagnostics against the real router (read-only). Need src on PYTHONPATH.
smoke:
	PYTHONPATH=src uv run python -m scripts.smoke

probe:
	PYTHONPATH=src uv run python -m scripts.probe

build-image:
	docker build -t deco-be85-api:latest .

run-image:
	docker run --rm -p 8000:8000 --env-file .env deco-be85-api:latest

clean:
	@find . -name '__pycache__' -type d -prune -exec rm -r {} + 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache .ty_cache htmlcov .coverage
