package dev.dock.cross

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

sealed interface ConnState {
    data class Disconnected(
        val reason: String?,
    ) : ConnState

    data object Connecting : ConnState

    data class Connected(
        val server: String,
    ) : ConnState
}

sealed interface RunItem {
    /** [kind] is `output` or `log`. */
    data class Text(
        val kind: String,
        val text: String,
    ) : RunItem

    data class Table(
        val id: String,
        val params: JsonObject,
    ) : RunItem

    data class Panel(
        val id: String,
        val title: String,
        val items: List<Pair<String, String>>,
    ) : RunItem

    data class Bar(
        val id: String,
        val params: JsonObject,
    ) : RunItem

    data class Warn(
        val message: String,
    ) : RunItem
}

data class AskState(
    val ask: Int,
    val method: String,
    val params: JsonObject,
) {
    /** [params] parsed for the UI; not part of equality. */
    val info: AskInfo = AskInfo.parse(method, params)
}

data class DoneInfo(
    val code: Int,
    val error: String?,
)

data class RunState(
    val id: String,
    val plugin: String,
    val command: String,
    val items: List<RunItem> = emptyList(),
    val ask: AskState? = null,
    val done: DoneInfo? = null,
)

data class ClientState(
    val conn: ConnState = ConnState.Disconnected(null),
    val plugins: List<PluginInfo> = emptyList(),
    val run: RunState? = null,
    /** Request id -> message; [GENERAL_ERROR] holds errors that carry no id. */
    val errors: Map<String, String> = emptyMap(),
) {
    companion object {
        const val GENERAL_ERROR = ""
    }
}

fun reduce(
    state: ClientState,
    frame: ServerFrame,
): ClientState =
    when (frame) {
        is ServerFrame.Welcome -> state.copy(conn = ConnState.Connected(frame.server))
        is ServerFrame.Plugins -> state.copy(plugins = frame.plugins)
        is ServerFrame.Ui -> onRun(state, frame.run) { run -> run.withUi(frame.method, frame.params) }
        is ServerFrame.Ask -> onRun(state, frame.run) { it.copy(ask = AskState(frame.ask, frame.method, frame.params)) }
        is ServerFrame.Done -> onRun(state, frame.run) { it.copy(ask = null, done = DoneInfo(frame.code, frame.error)) }
        is ServerFrame.Error -> onError(state, frame)
        is ServerFrame.Unknown -> state
    }

/** A run the client has just asked for; the server's frames for [id] fill it in. */
fun runStarted(
    state: ClientState,
    id: String,
    plugin: String,
    command: String,
): ClientState = state.copy(run = RunState(id, plugin, command))

/** Forgets the error shown for request [id] (use [ClientState.GENERAL_ERROR] for the one without an id). */
fun clearError(
    state: ClientState,
    id: String,
): ClientState = state.copy(errors = state.errors - id)

/** The socket closed or failed: the client is disconnected and the run, if any, will get no more frames. */
fun connectionLost(state: ClientState): ClientState =
    state.copy(
        conn = ConnState.Disconnected("connection lost"),
        run = state.run?.let { if (it.done == null) it.copy(ask = null, done = DoneInfo(-1, "connection lost")) else it },
    )

private inline fun onRun(
    state: ClientState,
    id: String,
    change: (RunState) -> RunState,
): ClientState {
    val run = state.run
    return if (run == null || run.id != id) state else state.copy(run = change(run))
}

private fun onError(
    state: ClientState,
    e: ServerFrame.Error,
): ClientState =
    when (e.code) {
        "auth" -> {
            state.copy(conn = ConnState.Disconnected("authentication failed"))
        }

        "version" -> {
            state.copy(conn = ConnState.Disconnected("version not supported: ${e.message}"))
        }

        else -> {
            val key = e.id ?: ClientState.GENERAL_ERROR
            val refused = state.run?.takeIf { it.id == e.id && it.done == null }
            state.copy(errors = state.errors + (key to e.message), run = if (refused != null) null else state.run)
        }
    }

private fun RunState.withUi(
    method: String,
    p: JsonObject,
): RunState =
    when (method) {
        "output", "log" -> copy(items = items + RunItem.Text(method, p.str("text") ?: p.str("message") ?: ""))
        "table" -> upsert(p.str("id")) { RunItem.Table(it, p) }
        "bar" -> upsert(p.str("id")) { RunItem.Bar(it, p) }
        "panel" -> upsert(p.str("id")) { RunItem.Panel(it, p.str("title") ?: "", p.pairs("items")) }
        "warn" -> copy(items = items + RunItem.Warn(p.str("message") ?: ""))
        "clear" -> remove(p.str("id"), p.str("kind"))
        else -> this
    }

/** Removes the live item of class [kind] (bar, table or panel) with [id]; anything else is a no-op. */
private fun RunState.remove(
    id: String?,
    kind: String?,
): RunState {
    val type =
        when (kind) {
            "bar" -> RunItem.Bar::class
            "table" -> RunItem.Table::class
            "panel" -> RunItem.Panel::class
            else -> return this
        }
    return copy(items = items.filterNot { it::class == type && it.liveId() == id })
}

/** Live items (bar, table, panel) are updated in place by id, so an update stream does not grow the list. */
private fun RunState.upsert(
    id: String?,
    make: (String) -> RunItem,
): RunState {
    if (id == null) return this
    val item = make(id)
    val at = items.indexOfFirst { it::class == item::class && it.liveId() == id }
    return copy(items = if (at < 0) items + item else items.toMutableList().also { it[at] = item })
}

private fun RunItem.liveId(): String? =
    when (this) {
        is RunItem.Table -> id
        is RunItem.Bar -> id
        is RunItem.Panel -> id
        else -> null
    }

private fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content

private fun JsonObject.pairs(key: String): List<Pair<String, String>> =
    (this[key] as? JsonArray).orEmpty().mapNotNull { row ->
        val cells = (row as? JsonArray)?.map { (it as? JsonPrimitive)?.content ?: it.toString() }
        if (cells != null && cells.size == 2) cells[0] to cells[1] else null
    }
