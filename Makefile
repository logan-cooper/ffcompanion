UV ?= uv

# One season by default; SEASONS overrides for a multi-season load. Three years
# is the working set: a single season makes an injured star look washed up, and
# dynasty valuation needs a multi-year trajectory.
SEASON  ?= 2025
SEASONS ?= $(SEASON)
ALL_SEASONS ?= 2023,2024,2025

.DEFAULT_GOAL := help
.PHONY: help sync test ingest warehouse status link-league verify-scoring chat eval clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) \
		| sort \
		| awk -F':.*##' '{ printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2 }'

sync: ## Install/refresh the virtualenv from the lockfile
	$(UV) sync

test: ## Run the test suite
	$(UV) run pytest

ingest: ## Load nflverse stats — make ingest SEASON=2025 | SEASONS=2023,2024,2025
	$(UV) run python -m advisor.cli ingest --season $(SEASONS)

warehouse: ## Build the full multi-season warehouse (2023,2024,2025)
	$(UV) run python -m advisor.cli ingest --season $(ALL_SEASONS)

status: ## Show what has been ingested and when
	$(UV) run python -m advisor.cli status

link-league: ## Link Sleeper leagues — make link-league USERNAME=you SEASON=2025
	$(UV) run python -m advisor.cli link-league --username $(USERNAME) --season $(SEASON)

verify-scoring: ## Check scored points against what Sleeper actually recorded
	$(UV) run python -m advisor.cli verify-scoring --season $(SEASON)

chat: ## Start the conversational REPL (Phase 5)
	@echo "make chat lands in Phase 5 (agent loop)."
	@exit 1

eval: ## Run the eval suite (Phase 6)
	@echo "make eval lands in Phase 6 (eval harness)."
	@exit 1

clean: ## Remove the local database, caches, and build artifacts
	rm -rf data/advisor.duckdb data/cache .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
