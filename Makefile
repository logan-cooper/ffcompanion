UV ?= uv
SEASON ?= 2025

.DEFAULT_GOAL := help
.PHONY: help sync test ingest chat eval clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) \
		| sort \
		| awk -F':.*##' '{ printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2 }'

sync: ## Install/refresh the virtualenv from the lockfile
	$(UV) sync

test: ## Run the test suite
	$(UV) run pytest

ingest: ## Load a season of nflverse stats (Phase 1) — make ingest SEASON=2025
	@echo "make ingest lands in Phase 1 (stats warehouse). SEASON=$(SEASON)"
	@exit 1

chat: ## Start the conversational REPL (Phase 5)
	@echo "make chat lands in Phase 5 (agent loop)."
	@exit 1

eval: ## Run the eval suite (Phase 6)
	@echo "make eval lands in Phase 6 (eval harness)."
	@exit 1

clean: ## Remove the local database, caches, and build artifacts
	rm -rf data/advisor.duckdb data/cache .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
