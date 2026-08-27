.PHONY: install dev test lint fmt demo docker warm record

install:
	python3 -m venv .venv
	./.venv/bin/pip install -q --upgrade pip
	./.venv/bin/pip install -q -r requirements-dev.txt
	@echo "done. cp .env.example .env and add your cookies."

dev:
	./.venv/bin/uvicorn app.main:app --reload --port 8000

demo:
	DEMO_MODE=true ./.venv/bin/uvicorn app.main:app --reload --port 8000

test:
	./.venv/bin/python -m pytest tests/ -q

lint:
	./.venv/bin/ruff check app/ tests/ scripts/

fmt:
	./.venv/bin/ruff check --fix app/ tests/ scripts/

docker:
	docker build -t linkedin-profile-api .
	docker run --rm -p 8000:8000 --env-file .env linkedin-profile-api

record:
	@test -n "$(URL)" || (echo "usage: make record URL=https://www.linkedin.com/in/someone"; exit 1)
	./.venv/bin/python scripts/record_fixture.py "$(URL)"

warm:
	./.venv/bin/python scripts/warm_cache.py profiles.txt
