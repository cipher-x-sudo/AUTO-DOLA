.PHONY: dev test lint format migrate seed

dev:
	docker compose up --build

test:
	cd backend && python -m pytest

lint:
	cd backend && python -m ruff check app tests
	cd frontend && npm run lint

format:
	cd backend && python -m ruff format app tests

migrate:
	cd backend && alembic upgrade head

seed:
	cd backend && python -m app.seed
