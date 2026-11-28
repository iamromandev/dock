package dev.dock.cross

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.ClosedSendChannelException
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.future.await
import kotlinx.coroutines.launch
import java.net.URI
import java.net.http.HttpClient
import java.net.http.WebSocket
import java.time.Duration
import java.util.concurrent.CompletionStage

/** [Transport] over the JDK's own `java.net.http.WebSocket`. */
class JdkTransport(
    private val client: HttpClient = HttpClient.newBuilder().connectTimeout(CONNECT_TIMEOUT).build(),
) : Transport {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override suspend fun connect(url: String): Connection {
        val channel = Channel<String>(INCOMING_BUFFER)
        val socket =
            client
                .newWebSocketBuilder()
                .connectTimeout(CONNECT_TIMEOUT)
                .buildAsync(URI.create(url), Listener(channel, scope))
                .await()
        return JdkConnection(socket, channel)
    }

    private class Listener(
        private val channel: Channel<String>,
        private val scope: CoroutineScope,
    ) : WebSocket.Listener {
        private val partial = StringBuilder()

        override fun onText(
            webSocket: WebSocket,
            data: CharSequence,
            last: Boolean,
        ): CompletionStage<*>? {
            partial.append(data)
            if (!last) {
                webSocket.request(1)
                return null
            }
            val message = partial.toString()
            partial.setLength(0)
            // Backpressure: the next frame is requested only once this one fits in the bounded buffer.
            scope.launch {
                try {
                    channel.send(message)
                    webSocket.request(1)
                } catch (_: ClosedSendChannelException) {
                    // the connection was closed meanwhile
                }
            }
            return null
        }

        override fun onClose(
            webSocket: WebSocket,
            statusCode: Int,
            reason: String,
        ): CompletionStage<*>? {
            channel.close()
            return webSocket.sendClose(closeReplyCode(statusCode), "")
        }

        override fun onError(
            webSocket: WebSocket,
            error: Throwable,
        ) {
            channel.close(error)
        }
    }

    private class JdkConnection(
        private val socket: WebSocket,
        private val channel: Channel<String>,
    ) : Connection {
        override val incoming: Flow<String> = channel.receiveAsFlow()

        override suspend fun send(text: String) {
            socket.sendText(text, true).await()
        }

        override fun close() {
            socket.sendClose(WebSocket.NORMAL_CLOSURE, "").whenComplete { _, _ -> channel.close() }
        }
    }
}

private val CONNECT_TIMEOUT: Duration = Duration.ofSeconds(5)
private const val INCOMING_BUFFER = 64

/** Codes 1004-1006 and 1015 are reserved and must not be sent, and only 1000-4999 are valid: answer those with a normal closure. */
internal fun closeReplyCode(received: Int): Int =
    if (received in 1000..4999 && received !in setOf(1004, 1005, 1006, 1015)) received else WebSocket.NORMAL_CLOSURE
