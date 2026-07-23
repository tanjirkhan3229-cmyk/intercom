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

# ---- Load & chaos (P0.12, RFC-001 §3/§5.2/§9, RFC-002 §9) ----
# k6 & terraform run via Docker images (not installed natively). BASE_URL defaults to the
# Docker-host address so containers reach the host-exposed API/gateway.
BASE_URL ?= http://host.docker.internal:8000

.PHONY: load
load: ## k6 message-path SLO gate (send p95<250, inbox p95<300; needs the stack up)
	docker run --rm -i -e BASE_URL=$(BASE_URL) grafana/k6 run - < load/k6/message_path.js

STORM_TARGET ?= 200
.PHONY: load-storm
load-storm: ## k6 Centrifugo reconnect storm (needs the stack up; STORM_TARGET=200 for local)
	docker run --rm -i -e BASE_URL=$(BASE_URL) -e STORM_TARGET=$(STORM_TARGET) grafana/k6 run - < load/k6/connection_storm.js

.PHONY: chaos
chaos: chaos-redis chaos-gateway chaos-pg chaos-restore ## Run all chaos drills in sequence (stops on first failure)

.PHONY: chaos-redis
chaos-redis: ## Chaos: Redis broker down -> zero loss (outbox buffers, relay replays)
	bash scripts/chaos/kill_redis_broker.sh

.PHONY: chaos-gateway
chaos-gateway: ## Chaos: Centrifugo down -> API serves, zero loss (fan-out buffered)
	bash scripts/chaos/kill_gateway.sh

.PHONY: chaos-pg
chaos-pg: ## Chaos: Postgres failover -> idempotency absorbs the retry
	bash scripts/chaos/pg_failover_sim.sh

.PHONY: chaos-restore
chaos-restore: ## Chaos: backup restore rehearsal + row-count checksum
	bash scripts/chaos/restore_drill.sh

# ---- Security & IaC gates (P0.12) ----

.PHONY: security
security: ## RLS audit + secret scan + dependency audit (in the api toolchain)
	cd apps/api && uv run python ../../scripts/audit_rls.py && uv run python ../../scripts/scan_secrets.py && uv run pip-audit

.PHONY: tf-validate
tf-validate: ## terraform fmt -check + init + validate (via the hashicorp/terraform Docker image)
	docker run --rm -v "$(PWD)/infra/terraform":/tf -w /tf hashicorp/terraform:1.9 fmt -check -recursive
	docker run --rm -v "$(PWD)/infra/terraform":/tf -w /tf hashicorp/terraform:1.9 init -backend=false -input=false
	docker run --rm -v "$(PWD)/infra/terraform":/tf -w /tf hashicorp/terraform:1.9 validate
