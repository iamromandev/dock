## core
# variables
PYTHON := python3
GO := go
DL := plugin/download
IMG := plugin/image
CARGO := cargo
HAVE_CARGO := $(shell command -v $(CARGO) 2>/dev/null)
HAVE_WASM_TARGET := $(if $(HAVE_CARGO),$(shell { rustup target list --installed 2>/dev/null | grep -qx wasm32-wasip1 || [ -d "$$(rustc --print sysroot 2>/dev/null)/lib/rustlib/wasm32-wasip1" ]; } && echo yes))
PLATFORM := $(shell $(PYTHON) -c "from cli.manifest import platform_key; print(platform_key())")

# phony targets
.PHONY: build dist install test clean help image-skip

## operation
# build
build: $(DL)/bin/download-$(PLATFORM) $(if $(HAVE_WASM_TARGET),$(IMG)/plugin.wasm,image-skip) # Build plugins for this machine only

$(DL)/bin/download-$(PLATFORM): $(DL)/main.go
	cd $(DL) && $(GO) build -o bin/download-$(PLATFORM)$(if $(findstring windows,$(PLATFORM)),.exe) .

image-skip:
	@echo "skipping image plugin build (needs cargo and the wasm32-wasip1 target)"

$(IMG)/plugin.wasm: $(IMG)/Cargo.toml $(IMG)/Cargo.lock $(wildcard $(IMG)/src/*.rs)
	cd $(IMG) && $(CARGO) build --release --target wasm32-wasip1 && cp target/wasm32-wasip1/release/image.wasm plugin.wasm

dist: # Build the download plugin for every OS/arch listed in its plugin.toml
	cd $(DL) && \
	GOOS=linux   GOARCH=amd64 $(GO) build -o bin/download-linux-x86_64 . && \
	GOOS=linux   GOARCH=arm64 $(GO) build -o bin/download-linux-arm64 . && \
	GOOS=darwin  GOARCH=amd64 $(GO) build -o bin/download-darwin-x86_64 . && \
	GOOS=darwin  GOARCH=arm64 $(GO) build -o bin/download-darwin-arm64 . && \
	GOOS=windows GOARCH=amd64 $(GO) build -o bin/download-windows-x86_64.exe .

# plugins
install: build # Install the example plugins into $$DOCK_HOME (default ~/.dock)
	$(PYTHON) -m cli plugin install plugin/hello
	$(PYTHON) -m cli plugin install plugin/llm
	$(PYTHON) -m cli plugin install plugin/chat
	$(PYTHON) -m cli plugin install plugin/download
	@# Stage only what the plugin needs: installing $(IMG) itself would copy its cargo target/ too.
	@if [ -f $(IMG)/plugin.wasm ]; then \
		stage=$$(mktemp -d) && \
		cp $(IMG)/plugin.toml $(IMG)/plugin.wasm $(IMG)/README.md "$$stage" && \
		{ $(PYTHON) -m cli plugin install "$$stage"; rc=$$?; rm -f "$$stage/plugin.toml" "$$stage/plugin.wasm" "$$stage/README.md"; rmdir "$$stage"; exit $$rc; }; \
	else echo "skipping image plugin (needs cargo + wasm32-wasip1)"; fi

# checks
test: build # Run the unit test suite
	$(PYTHON) -m unittest discover -s tests -v
	$(if $(HAVE_CARGO),cd $(IMG) && $(CARGO) test)

# cleanup
clean: # Remove built plugin binaries
	rm -rf $(DL)/bin $(IMG)/target $(IMG)/plugin.wasm

# help
help:
	@grep -E '^[a-zA-Z_-]+:.*?#' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?#"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
