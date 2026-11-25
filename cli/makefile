## core
# variables
UV := uv

# phony targets
.PHONY: install check check-ci test run plugin-install clean help

## operation
# python tools (uv)
install: # Install all dependencies
	$(UV) sync

check: install # Run lint + type checks
	$(UV) run ruff check --fix
	$(UV) run ty check

check-ci: install # Run lint + type checks without fixing anything. For CI.
	$(UV) run ruff check
	$(UV) run ty check

# checks
test: # Run the test suite (needs Docker or podman; stops at once without one)
	@$(UV) run python -c "import sys; sys.excepthook = lambda t, e, tb: print('make: ' + str(e), file=sys.stderr); sys.path.insert(0, 'tests'); import dockenv; dockenv.require_runtime()"
	$(UV) run pytest

# run
run: # Run dock, e.g. make run ARGS="plugin list"
	$(UV) run dock $(ARGS)

plugin-install: # Install the example plugin into $DOCK_HOME (default ~/.dock)
	$(UV) run dock plugin install plugins/hello

# cleanup
clean: # Remove Python caches
	find . -name __pycache__ -type d -prune -exec rm -r {} +
	rm -rf .pytest_cache .ruff_cache

# help
help:
	@grep -E '^[a-zA-Z_-]+:.*?#' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?#"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
