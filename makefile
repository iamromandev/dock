## core
# variables
# This file owns no logic. Each target hands off to the stack that does:
# cli/makefile for the Python host. The exception is repo setup (install-hooks),
# which is no stack's task. cross-* targets will be added here when cross/ lands.
#
# Variables typed on the command line reach the sub-make on their own: GNU make
# forwards command-line overrides through MAKEFLAGS. Run `make -C cli help` for
# the CLI's own targets.
CLI := cli
UV := uv

# phony targets
.PHONY: install-hooks check clean help \
	cli-install cli-check cli-check-ci cli-test cli-run cli-plugin-install

## repo setup
install-hooks: # Install pre-commit (as a uv tool) and its git hook for this repo. Run once per clone
	$(UV) tool install pre-commit
	$(UV) tool run pre-commit install

## all stacks
check: cli-check # Lint + typecheck every stack

clean: # Remove Python caches
	$(MAKE) -C $(CLI) clean

## cli: delegates to cli/makefile
cli-install: # Install CLI dependencies
	$(MAKE) -C $(CLI) install

cli-check: # Lint + typecheck the CLI (fixes what ruff can)
	$(MAKE) -C $(CLI) check

cli-check-ci: # Lint + typecheck the CLI without fixing anything. For CI.
	$(MAKE) -C $(CLI) check-ci

cli-test: # Run the CLI tests (needs Docker or podman)
	$(MAKE) -C $(CLI) test

cli-run: # Run dock, e.g. make cli-run ARGS="plugin list"
	$(MAKE) -C $(CLI) run

cli-plugin-install: # Install the example plugin into $DOCK_HOME (default ~/.dock)
	$(MAKE) -C $(CLI) plugin-install

# help
help:
	@grep -E '^[a-zA-Z_-]+:.*?#' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?#"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
