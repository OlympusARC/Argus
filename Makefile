.PHONY: help install test lint fmt discover resolve poll stats db-push

help:            ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:         ## Create the venv and install with dev extras
	python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev,postgres]'

test:            ## Run the test suite
	./.venv/bin/pytest

lint:            ## Lint and check formatting
	./.venv/bin/ruff check . && ./.venv/bin/ruff format --check .

fmt:             ## Apply formatting and autofixes
	./.venv/bin/ruff check --fix . && ./.venv/bin/ruff format .

discover:        ## Sweep every ready source for boards and companies
	./.venv/bin/argus discover

resolve:         ## Attach careers pages to companies that lack one
	./.venv/bin/argus companies --resolve --limit 2000

poll:            ## Reconcile every due board and emit job events
	./.venv/bin/argus poll

stats:           ## Registry and feed summary
	./.venv/bin/argus stats

db-push:         ## Apply supabase/migrations to the linked project
	supabase db push
