## core
# variables
# This file owns no logic. Each target hands off to moon, which runs the project tasks defined in apps/*/moon.yml and plugins/*/moon.yml.
# The exception is repo setup (install-hooks), which is no project's task. cross-* and server-* targets will be added
# here when apps/cross and apps/server land. Tool versions (moon, uv) are in .prototools: run `proto install` once.
MOON := moon
UV := uv

# phony targets
.PHONY: install-hooks install check check-ci test clean help \
	cli-sync cli-install cli-uninstall cli-check cli-check-ci cli-test cli-run cli-clean \
	dm-install dm-check dm-check-ci dm-test dm-clean \
	planner-install planner-check planner-check-ci planner-test planner-clean

## repo setup
install-hooks: # Install pre-commit (as a uv tool) and its git hook for this repo. Run once per clone
	$(UV) tool install pre-commit
	$(UV) tool run pre-commit install

## all projects - runs the task in every project that defines it
install: # Install every project's dependencies
	$(MOON) run cli:sync dm:install planner:install

check: # Lint (with fixes) and type-check every project
	$(MOON) run :check

check-ci: # Lint and type-check every project without fixing anything. For CI.
	$(MOON) run :check-ci

test: # Run every project's tests (the CLI's need Docker or podman; dm's do not)
	$(MOON) run :test

clean: # Remove Python caches
	$(MOON) run :clean

## cli - delegates to apps/cli/moon.yml
cli-sync: # Install the CLI dependencies into apps/cli/.venv (uv sync)
	$(MOON) run cli:sync

cli-install: # Install the dock command on your PATH (editable, from this checkout)
	$(MOON) run cli:install

cli-uninstall: # Remove the installed dock command
	$(MOON) run cli:uninstall

cli-check: # Lint + typecheck the CLI (fixes what ruff can)
	$(MOON) run cli:check

cli-check-ci: # Lint + typecheck the CLI without fixing anything. For CI.
	$(MOON) run cli:check-ci

cli-test: # Run the CLI tests (needs Docker or podman; stops at once without one)
	$(MOON) run cli:test

cli-run: # Run dock, e.g. make cli-run ARGS="plugin list"
	$(MOON) run cli:run -- $(ARGS)

cli-clean: # Remove the CLI's Python caches
	$(MOON) run cli:clean

## dm - delegates to plugins/dm/moon.yml
dm-install: # Install dm's dev tools
	$(MOON) run dm:install

dm-check: # Lint + typecheck dm (fixes what ruff can)
	$(MOON) run dm:check

dm-check-ci: # Lint + typecheck dm without fixing anything. For CI.
	$(MOON) run dm:check-ci

dm-test: # Run dm's in-process tests (no Docker or podman needed)
	$(MOON) run dm:test

dm-clean: # Remove dm's Python caches
	$(MOON) run dm:clean

## planner - delegates to plugins/planner/moon.yml
planner-install: # Install the planner's dev tools
	$(MOON) run planner:install

planner-check: # Lint + typecheck the planner (fixes what ruff can)
	$(MOON) run planner:check

planner-check-ci: # Lint + typecheck the planner without fixing anything. For CI.
	$(MOON) run planner:check-ci

planner-test: # Run the planner's tests (a local stand-in model server; no Docker or model needed)
	$(MOON) run planner:test

planner-clean: # Remove the planner's Python caches
	$(MOON) run planner:clean

# help
help:
	@grep -E '^[a-zA-Z_-]+:.*?#' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?#"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
