# JobHunter - development commands
.DEFAULT_GOAL := help
UV := $(shell command -v uv 2>/dev/null || echo "$$HOME/.local/bin/uv")

.PHONY: help install browser init doctor run scan serve schedule test test-cov lint format typecheck check migrate migration clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies into .venv
	$(UV) sync --extra ai

browser: ## Download the Chromium build Playwright needs
	$(UV) run playwright install chromium

init: ## First-run setup: migrate, find CVs, bootstrap the profile
	$(UV) run jobhunter init

doctor: ## Check that the environment is ready
	$(UV) run jobhunter doctor

scan: ## Run one discovery + scoring pass
	$(UV) run jobhunter scan

serve: ## Start the dashboard on http://127.0.0.1:8000
	$(UV) run jobhunter serve

schedule: ## Run the scheduler in the foreground
	$(UV) run jobhunter schedule

test: ## Run the test suite
	$(UV) run pytest

test-cov: ## Run tests with a coverage report
	$(UV) run pytest --cov --cov-report=term-missing

lint: ## Check formatting and lint rules
	$(UV) run ruff format --check src tests
	$(UV) run ruff check src tests

format: ## Auto-format and auto-fix
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

typecheck: ## Run mypy
	$(UV) run mypy

check: lint typecheck test ## Everything CI would run

migrate: ## Apply database migrations
	$(UV) run alembic upgrade head

migration: ## Create a migration (make migration m="message")
	$(UV) run alembic revision --autogenerate -m "$(m)"

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
