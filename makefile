## core
# variables
PYTHON := python3

# phony targets
.PHONY: build install test clean help

## operation
# build
build: # Nothing to build: plugins are built into images on first use
	@echo "nothing to build"

# plugins
install: build # Install the example plugin into $$DOCK_HOME (default ~/.dock)
	$(PYTHON) -m cli plugin install plugin/hello

# checks
test: build # Run the test suite (needs Docker or podman; stops at once without one)
	@$(PYTHON) -c "import sys; sys.excepthook = lambda t, e, tb: print('make: ' + str(e), file=sys.stderr); sys.path.insert(0, 'tests'); import dockenv; dockenv.require_runtime()"
	$(PYTHON) -m unittest discover -s tests -v

# cleanup
clean: # Remove Python caches
	find . -name __pycache__ -type d -prune -exec rm -r {} +

# help
help:
	@grep -E '^[a-zA-Z_-]+:.*?#' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?#"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
