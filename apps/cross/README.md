# cross

A desktop client for the dock server (`apps/server`), written in Kotlin Multiplatform. It connects to the server over
its WebSocket protocol (version 2, see `apps/server/docs/SERVER_PROTOCOL.md`), lists the installed plugins, runs their
commands, shows their output, bars, tables and panels live, and answers their prompts and forms.

## Toolchain

`proto install` in the repository root installs JDK 25 (and moon and uv) at the versions in `.prototools`. Gradle comes
from the wrapper in this folder.

## Run

Start the server with a token, then the client:

```
DOCK_SERVER_TOKEN=<a secret> moon run server:run
moon run cross:run
```

The client asks for the host, port and token on its connect screen.

## Tasks

Run from the repository root (`moon run cross:<task>`).

| Task | What it does |
|---|---|
| `check` | Lints (kotlinter) and compiles; no tests |
| `test` | The unit tests of `shared` and `desktopApp`; no server or container runtime needed |
| `test-live` | Runs the client against a real `apps/server` with the server's fixture plugins. Needs uv and a running Docker or Podman (the first run builds the fixture images). Not part of `check` or `test` |
| `run` | Starts the desktop app |

## Layout

- `shared/`: the client core (`dev.dock.cross`): protocol frames, state reduction, `DockClient`, the JVM WebSocket
  transport; `src/jvmLiveTest` holds the live-server tests
- `desktopApp/`: the Compose Desktop UI

## Token storage

The desktop app can remember the token in the operating system's keychain (macOS Keychain, Windows Credential Manager,
or the Secret Service on Linux), under the service name `dock-cross`. The token is never written to a file, and the
client keeps it only long enough to send the `hello` frame.
