# Relay — developer entrypoints. One-command bootstrap: `make dev`.
# All service orchestration lives in infra/docker-compose.yml.

SHELL := /bin/bash
COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env
API_EXEC := $(COMPOSE) exec -T api

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.env: ## Create .env from .env.example if absent
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")

.PHONY: dev
dev: .env ## Boot the full dev stack (Postgres+pgvector, Redis x2, MinIO, Mailpit, API, workers) and run migrations
	$(COMPOSE) up -d --build postgres redis-cache redis-broker minio createbuckets mailpit
	$(COMPOSE) run --rm --build migrate
	$(COMPOSE) up -d --build api worker beat
	@echo ""
	@echo "Relay dev stack is up:"
	@echo "  API        http://localhost:8000  (docs: /docs, health: /healthz)"
	@echo "  MinIO      http://localhost:9001  (console)"
	@echo "  Mailpit    http://localhost:8025  (inbox)"
	@echo "  Postgres   localhost:5432   Redis cache 6379   Redis broker 6380"
	@echo "Run 'make web' and 'make widget' for the frontends."

.PHONY: infra
infra: .env ## Boot only backing services (no app containers)
	$(COMPOSE) up -d postgres redis-cache redis-broker minio createbuckets mailpit

.PHONY: migrate
migrate: .env ## Run Alembic migrations (as the migrator role)
	$(COMPOSE) run --rm --build migrate

.PHONY: makemigration
makemigration: ## Autogenerate a migration: make makemigration m="add x"
	$(API_EXEC) alembic revision --autogenerate -m "$(m)"

.PHONY: down
down: ## Stop and remove all containers
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop containers and delete local data volumes
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail logs for all services
	$(COMPOSE) logs -f --tail=100

.PHONY: api-shell
api-shell: ## Open a shell in the running API container
	$(COMPOSE) exec api bash

# ---- Quality gates (mirror CI) ----

.PHONY: lint
lint: lint-api lint-web ## Run all linters/typecheckers

.PHONY: lint-api
lint-api: ## ruff + mypy + import-linter + migration lint (in the api container)
	cd apps/api && ruff check . && ruff format --check . && mypy src && lint-imports && python ../../scripts/check_migrations.py

.PHONY: lint-web
lint-web: ## eslint + tsc for web/widget/packages
	cd apps/web && npm run lint && npm run typecheck

.PHONY: test
test: test-api ## Run all tests

.PHONY: test-api
test-api: ## Run API unit + integration tests (testcontainers spin up PG/Redis)
	cd apps/api && pytest -q

# ---- Frontends (run on host with npm) ----

.PHONY: web
web: ## Run the Next.js agent app + marketing site (dev server)
	cd apps/web && npm install && npm run dev

.PHONY: widget
widget: ## Run the Preact messenger widget (dev server)
	cd apps/widget && npm install && npm run dev

.PHONY: sdk
sdk: ## Regenerate the TS SDK from the API OpenAPI spec
	cd apps/api && python -m relay.cli openapi > ../../openapi.json
	cd packages/sdk-ts && npm install && npm run generate
