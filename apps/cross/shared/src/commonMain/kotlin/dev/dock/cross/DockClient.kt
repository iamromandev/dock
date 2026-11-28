package dev.dock.cross

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.json.JsonObject
import kotlin.concurrent.Volatile

/**
 * One connection to a dock server: sends requests, reduces every frame it receives into [state].
 * The token is only ever put into the hello frame; it is not kept, and no state or message holds it.
 */
class DockClient(
    private val transport: Transport,
    private val scope: CoroutineScope,
) {
    private val mutable = MutableStateFlow(ClientState())
    val state: StateFlow<ClientState> = mutable

    @Volatile
    private var connection: Connection? = null

    /** Held around every send, so the socket sees one send at a time; it also guards [nextId]. */
    private val sendLock = Mutex()
    private var nextId = 0

    /** Connects and says hello; returns once the server has welcomed or rejected us ([state] tells which). */
    suspend fun connect(
        host: String,
        port: Int,
        token: String,
    ) {
        disconnect()
        mutable.value = ClientState(conn = ConnState.Connecting)
        val conn =
            try {
                transport.connect("ws://$host:$port")
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                mutable.update { it.copy(conn = ConnState.Disconnected(e.message ?: "could not connect")) }
                return
            }
        connection = conn
        scope.launch {
            try {
                conn.incoming.collect { text -> mutable.update { reduce(it, Protocol.decode(text)) } }
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                // a failed socket ends the same way as a closed one
            }
            if (connection === conn) mutable.update { if (it.conn is ConnState.Disconnected) it else connectionLost(it) }
        }
        if (!sendLock.withLock { trySend(conn, ClientFrame.hello(token)) }) return
        val answered = withTimeoutOrNull(HANDSHAKE_TIMEOUT_MS) { mutable.first { it.conn !is ConnState.Connecting } }
        if (answered == null) mutable.update { it.copy(conn = ConnState.Disconnected("no answer from the server")) }
        if (mutable.value.conn !is ConnState.Connected) conn.close()
    }

    /** Asks for the plugin list; returns the request id the `plugins` reply carries. */
    suspend fun list(): String =
        sendLock.withLock {
            newId().also { sendLocked(ClientFrame.list(it)) }
        }

    /**
     * Starts a run; returns its id, which keys the run's frames and any error for it. Returns null, sending nothing,
     * while another run is still active (the server allows one run per connection).
     */
    suspend fun run(
        plugin: String,
        command: String,
        inputs: JsonObject?,
    ): String? =
        sendLock.withLock {
            val active = mutable.value.run
            if (active != null && active.done == null) return@withLock null
            val id = newId()
            mutable.update { runStarted(it, id, plugin, command) }
            sendLocked(ClientFrame.run(id, plugin, command, inputs))
            id
        }

    suspend fun answer(
        ask: Int,
        result: JsonObject,
    ) {
        mutable.update { s -> s.copy(run = s.run?.let { if (it.ask?.ask == ask) it.copy(ask = null) else it }) }
        sendLock.withLock { sendLocked(ClientFrame.answer(ask, result)) }
    }

    /** Cancels the active run, if there is one; the server ends it with `done`. */
    suspend fun cancel() {
        sendLock.withLock {
            val run = mutable.value.run ?: return
            if (run.done == null) sendLocked(ClientFrame.cancel(run.id))
        }
    }

    fun clearError(id: String) {
        mutable.update { clearError(it, id) }
    }

    fun disconnect() {
        val conn = connection ?: return
        connection = null
        conn.close()
        mutable.update { if (it.conn is ConnState.Disconnected) it else connectionLost(it).copy(conn = ConnState.Disconnected(null)) }
    }

    private fun newId(): String = "req-${++nextId}"

    private suspend fun sendLocked(text: String) {
        val conn = connection ?: return
        if (mutable.value.conn is ConnState.Connected) trySend(conn, text)
    }

    private suspend fun trySend(
        conn: Connection,
        text: String,
    ): Boolean =
        try {
            conn.send(text)
            true
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            if (connection === conn) connection = null
            conn.close()
            mutable.update { if (it.conn is ConnState.Disconnected) it else connectionLost(it) }
            false
        }

    private companion object {
        const val HANDSHAKE_TIMEOUT_MS = 10_000L
    }
}
