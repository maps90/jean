# jean — local dev Makefile.
# `make` with no target prints help.

.DEFAULT_GOAL := help
.PHONY: help setup env sync run db-up db-down db-logs up down logs build verify test lint format fix

# ---- Setup -----------------------------------------------------------------

setup: sync env ## First-time setup: install deps and create .env
	@echo "Setup done. Edit .env, ensure ~/.jean/IDENTITY.md exists, then: make db-up && make run"

sync: ## Install/refresh Python deps (uv sync)
	uv sync

env: ## Create .env from .env.example (won't overwrite an existing .env)
	@if [ -f .env ]; then \
		echo ".env already exists — leaving it untouched."; \
	else \
		cp .env.example .env && echo "Created .env from .env.example — fill in your tokens."; \
	fi

# ---- Run (hybrid: Postgres in Docker, jean on host) ------------------------

run: ## Run jean on the host (loads .env). Needs Postgres up — see make db-up.
	@if [ ! -f .env ]; then echo "No .env found — run 'make env' first."; exit 1; fi
	set -a && . ./.env && set +a && uv run jean

db-up: ## Start the Postgres service in the background
	docker compose up -d postgres

db-down: ## Stop the Postgres service
	docker compose stop postgres

db-logs: ## Tail Postgres logs
	docker compose logs -f postgres

# ---- Run (full Docker: Postgres + jean, matches prod) ----------------------

up: ## Build and run the whole stack (Postgres + jean) in Docker
	docker compose up --build

down: ## Stop and remove the Docker stack
	docker compose down

logs: ## Tail all Docker service logs
	docker compose logs -f

build: ## Build the jean Docker image
	docker compose build

# ---- Quality gate ----------------------------------------------------------

verify: ## Run the full quality gate (lint + format-check + tests)
	./scripts/verify.sh

test: ## Run the test suite
	uv run pytest -q

lint: ## Lint with ruff
	uv run ruff check src tests

format: ## Format with ruff
	uv run ruff format src tests

fix: ## Auto-fix lint issues and format
	uv run ruff check --fix src tests
	uv run ruff format src tests

# ---- Help ------------------------------------------------------------------

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
