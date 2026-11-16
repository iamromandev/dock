PLATFORM := $(shell python3 -c "from dock.manifest import platform_key; print(platform_key())")
DL := plugins/download

.PHONY: build dist install test demo clean

## build: plugins for this machine only
build: $(DL)/bin/download-$(PLATFORM) plugins/hello/dock_sdk.py

$(DL)/bin/download-$(PLATFORM): $(DL)/main.go
	cd $(DL) && go build -o bin/download-$(PLATFORM)$(if $(findstring windows,$(PLATFORM)),.exe) .

plugins/hello/dock_sdk.py: sdk/python/dock_sdk.py
	cp $< $@

## dist: the download plugin for every OS/arch listed in its plugin.toml
dist: plugins/hello/dock_sdk.py
	cd $(DL) && \
	GOOS=linux   GOARCH=amd64 go build -o bin/download-linux-x86_64 . && \
	GOOS=linux   GOARCH=arm64 go build -o bin/download-linux-arm64 . && \
	GOOS=darwin  GOARCH=amd64 go build -o bin/download-darwin-x86_64 . && \
	GOOS=darwin  GOARCH=arm64 go build -o bin/download-darwin-arm64 . && \
	GOOS=windows GOARCH=amd64 go build -o bin/download-windows-x86_64.exe .

## install: put the three example plugins into $$DOCK_HOME (default ~/.dock)
install: build
	python3 -m dock plugins install plugins/hello
	python3 -m dock plugins install plugins/chat
	python3 -m dock plugins install plugins/download

test: build
	python3 -m unittest discover -s tests -v

clean:
	rm -rf $(DL)/bin plugins/hello/dock_sdk.py
