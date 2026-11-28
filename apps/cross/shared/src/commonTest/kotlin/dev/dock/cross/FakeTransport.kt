package dev.dock.cross

import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.receiveAsFlow

/** A server stand-in: records what the client sends, and [onSend] may push frames back through [push]. */
class FakeTransport(
    private val onSend: FakeConnection.(String) -> Unit = {},
) : Transport {
    var connection: FakeConnection? = null
    var url: String? = null
    var failWith: Exception? = null

    override suspend fun connect(url: String): Connection {
        failWith?.let { throw it }
        this.url = url
        return FakeConnection(onSend).also { connection = it }
    }
}

class FakeConnection(
    private val onSend: FakeConnection.(String) -> Unit,
) : Connection {
    private val channel = Channel<String>(Channel.UNLIMITED)
    val sent = mutableListOf<String>()
    var closed = false

    /** Set to make every send suspend this long first (so overlapping sends can be seen). */
    var sendDelayMs = 0L

    /** Set to make every send fail with it. */
    var failSend: Exception? = null

    private var inSend = false

    override val incoming: Flow<String> = channel.receiveAsFlow()

    override suspend fun send(text: String) {
        check(!inSend) { "send entered while the previous send had not completed" }
        inSend = true
        try {
            failSend?.let { throw it }
            if (sendDelayMs > 0) delay(sendDelayMs)
            sent += text
            onSend(text)
        } finally {
            inSend = false
        }
    }

    override fun close() {
        closed = true
        channel.close()
    }

    fun push(text: String) {
        channel.trySend(text)
    }
}
