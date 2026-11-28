"""dock_plugin: the plumbing every dock container plugin shares (stdlib only). Import only the modules you use:
rpc (stdio JSON-RPC loop), ui (log, bar, table messages), cancel (stoppable calls, sockets, workers),
files (safe inputs and outputs), net (cancellable HTTP request), env (environment parsing).
Edit the kit in packages/plugin-kit/dock_plugin; plugins carry a vendored copy. See docs/PLUGINS.md."""
