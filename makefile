## core
# variables
PYTHON := python3
DL := plugin/download
IMG := plugin/image
CARGO := cargo
HAVE_CARGO := $(shell command -v $(CARGO) 2>/dev/null)
HAVE_WASM_TARGET := $(if $(HAVE_CARGO),$(shell { rustup target list --installed 2>/dev/null | grep -qx wasm32-wasip1 || [ -d "$$(rustc --print sysroot 2>/dev/null)/lib/rustlib/wasm32-wasip1" ]; } && echo yes))
PLATFORM := $(shell $(PYTHON) -c "from cli.manifest import platform_key; print(platform_key())")
EXE := $(if $(findstring windows,$(PLATFORM)),.exe)

# phony targets
.PHONY: build dist install test clean help image-skip

## operation
# build
build: $(DL)/bin/download-$(PLATFORM)$(EXE) $(if $(HAVE_WASM_TARGET),$(IMG)/plugin.wasm,image-skip) # Build plugins for this machine only

$(DL)/bin/download-$(PLATFORM)$(EXE): $(DL)/Cargo.toml $(wildcard $(DL)/Cargo.lock) $(wildcard $(DL)/src/*.rs)
	cd $(DL) && $(CARGO) build --release --locked
	mkdir -p $(DL)/bin && cp $(DL)/target/release/download$(EXE) $(DL)/bin/download-$(PLATFORM)$(EXE)

image-skip:
	@echo "skipping image plugin build (needs cargo and the wasm32-wasip1 target)"

$(IMG)/plugin.wasm: $(IMG)/Cargo.toml $(IMG)/Cargo.lock $(wildcard $(IMG)/src/*.rs)
	cd $(IMG) && $(CARGO) build --release --target wasm32-wasip1 && cp target/wasm32-wasip1/release/image.wasm plugin.wasm

# Darwin is built natively for both arches; linux and windows go through zig (cargo-zigbuild), so there is no
# C toolchain or openssl to install (the plugin uses rustls). Needs the rustup targets and zig.
dist: # Build the download plugin for every OS/arch listed in its plugin.toml
	cd $(DL) && mkdir -p bin && \
	$(CARGO) build --release --locked --target aarch64-apple-darwin && \
	cp target/aarch64-apple-darwin/release/download bin/download-darwin-arm64 && \
	$(CARGO) build --release --locked --target x86_64-apple-darwin && \
	cp target/x86_64-apple-darwin/release/download bin/download-darwin-x86_64 && \
	$(CARGO) zigbuild --release --locked --target x86_64-unknown-linux-gnu && \
	cp target/x86_64-unknown-linux-gnu/release/download bin/download-linux-x86_64 && \
	$(CARGO) zigbuild --release --locked --target aarch64-unknown-linux-gnu && \
	cp target/aarch64-unknown-linux-gnu/release/download bin/download-linux-arm64 && \
	$(CARGO) zigbuild --release --locked --target x86_64-pc-windows-gnu && \
	cp target/x86_64-pc-windows-gnu/release/download.exe bin/download-windows-x86_64.exe

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
	cd $(DL) && $(CARGO) test --release --locked
	$(if $(HAVE_CARGO),cd $(IMG) && $(CARGO) test)

# cleanup
clean: # Remove built plugin binaries
	rm -rf $(DL)/bin $(DL)/target $(IMG)/target $(IMG)/plugin.wasm

# help
help:
	@grep -E '^[a-zA-Z_-]+:.*?#' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?#"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
