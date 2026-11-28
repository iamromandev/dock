package dev.dock.cross.desktop

import dev.dock.cross.ConnState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** The last connection, held in memory only so a dropped one can be reopened; the token never reaches disk from here. */
class Credentials(
    val host: String,
    val port: Int,
    val token: String,
) {
    /** Redacted: a data class would print the token in logs and exceptions. */
    override fun toString() = "Credentials(host=$host, port=$port)"
}

enum class Screen { Plugins, Reconnect, Connect }

/** Reconnect whenever the connection is down and there is something to reconnect to, run or not. */
fun screenFor(
    conn: ConnState,
    last: Credentials?,
): Screen =
    when {
        conn is ConnState.Connected -> Screen.Plugins
        conn is ConnState.Disconnected && last != null -> Screen.Reconnect
        else -> Screen.Connect
    }

/** The connect form starts from the last connection when there is one, else from the saved settings. */
fun initialSettings(
    last: Credentials?,
    saved: ConnectionSettings,
): ConnectionSettings = last?.let { ConnectionSettings(it.host, it.port) } ?: saved

const val TOKEN_DEBOUNCE_MS = 400L

/**
 * The stored token to prefill for [host] and [port], or null to leave the field alone: nothing valid to look up yet,
 * or the user typed a token ([edited], checked again after the slow read). Waits [debounceMs] first, so typing a host
 * does not read the keychain on every key (the caller cancels this when the host or port changes).
 */
suspend fun prefillToken(
    host: String,
    port: Int?,
    debounceMs: Long,
    edited: () -> Boolean,
    load: suspend (String, Int) -> String?,
): String? {
    if (port == null || host.isBlank() || edited()) return null
    delay(debounceMs)
    val token = load(host.trim(), port).orEmpty()
    return if (edited()) null else token
}

/** Remembers the ask ids already answered, so OK plus dismiss in one frame sends one answer. */
class AnswerOnce {
    private val answered = mutableSetOf<Int>()

    @Synchronized
    fun first(ask: Int): Boolean = answered.add(ask)
}

/** Runs one job at a time: [launch] does nothing and returns false while the previous one is active. */
class SingleFlight {
    private var job: Job? = null

    @Synchronized
    fun launch(
        scope: CoroutineScope,
        block: suspend () -> Unit,
    ): Boolean {
        if (job?.isActive == true) return false
        job = scope.launch { block() }
        return true
    }

    suspend fun join() {
        job?.join()
    }
}

/** Errors split into those of request [own] (shown by its command form) and all others (shown at the top). */
fun splitErrors(
    errors: Map<String, String>,
    own: String?,
): Pair<Map<String, String>, Map<String, String>> = errors.filterKeys { it == own } to errors.filterKeys { it != own }
