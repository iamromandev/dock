## core
# variables
PYTHON := python3
GO := go
DL := plugin/download
PLATFORM := $(shell $(PYTHON) -c "from cli.manifest import platform_key; print(platform_key())")

# phony targets
.PHONY: build dist install test clean help

## operation
# build
build: $(DL)/bin/download-$(PLATFORM) # Build plugins for this machine only

$(DL)/bin/download-$(PLATFORM): $(DL)/main.go
	cd $(DL) && $(GO) build -o bin/download-$(PLATFORM)$(if $(findstring windows,$(PLATFORM)),.exe) .

dist: # Build the download plugin for every OS/arch listed in its plugin.toml
	cd $(DL) && \
	GOOS=linux   GOARCH=amd64 $(GO) build -o bin/download-linux-x86_64 . && \
	GOOS=linux   GOARCH=arm64 $(GO) build -o bin/download-linux-arm64 . && \
	GOOS=darwin  GOARCH=amd64 $(GO) build -o bin/download-darwin-x86_64 . && \
	GOOS=darwin  GOARCH=arm64 $(GO) build -o bin/download-darwin-arm64 . && \
	GOOS=windows GOARCH=amd64 $(GO) build -o bin/download-windows-x86_64.exe .

# plugins
install: build # Install the four example plugins into $$DOCK_HOME (default ~/.dock)
	$(PYTHON) -m cli plugin install plugin/hello
	$(PYTHON) -m cli plugin install plugin/llm
	$(PYTHON) -m cli plugin install plugin/chat
	$(PYTHON) -m cli plugin install plugin/download

# checks
test: build # Run the unit test suite
	$(PYTHON) -m unittest discover -s tests -v

# cleanup
clean: # Remove built plugin binaries
	rm -rf $(DL)/bin

# help
help:
	@grep -E '^[a-zA-Z_-]+:.*?#' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?#"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
