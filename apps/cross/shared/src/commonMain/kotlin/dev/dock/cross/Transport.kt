package dev.dock.cross

import kotlinx.coroutines.flow.Flow

/** Opens a text-message connection to [url]; the platform supplies the implementation. */
interface Transport {
    suspend fun connect(url: String): Connection
}

interface Connection {
    /** One element per message received, in order. Completes when the connection closes or fails. */
    val incoming: Flow<String>

    suspend fun send(text: String)

    fun close()
}
