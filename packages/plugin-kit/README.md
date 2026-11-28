# plugin-kit

`dock_plugin`: the stdlib-only plumbing that dock container plugins share (the stdio JSON-RPC loop, cancel, safe files, a cancellable HTTP request, env parsing). Plugins carry a vendored copy; the kit here is the one place to edit.

- Edit `dock_plugin/`, then run `make plugin-kit-sync` to copy it into every plugin listed in `plugins.txt`.
- `make plugin-kit-check` lints, type-checks and verifies the copies; `make plugin-kit-test` runs the tests.

How to build a plugin on the kit, what belongs in it and the API: [docs/PLUGINS.md](../../docs/PLUGINS.md).
