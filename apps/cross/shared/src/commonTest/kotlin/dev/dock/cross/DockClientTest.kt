package dev.dock.cross

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

@OptIn(ExperimentalCoroutinesApi::class)
class DockClientTest {
    private val welcome = """{"type":"welcome","version":2,"server":"0.1.0"}"""

    private fun obj(s: String): JsonObject = Json.parseToJsonElement(s).jsonObject

    private fun TestScope.client(transport: FakeTransport) = DockClient(transport, CoroutineScope(UnconfinedTestDispatcher(testScheduler)))

    private fun answering(reply: String) = FakeTransport { text -> if (obj(text)["type"]!!.jsonPrimitive.content == "hello") push(reply) }

    // AC12 (part). Bug caught: hello is malformed, or the token leaks into state or toString output.
    @Test
    fun handshakeSendsHelloAndTheTokenStaysOutOfState() =
        runTest {
            val transport = answering(welcome)
            val c = client(transport)
            c.connect("127.0.0.1", 7464, "s3cret-token")
            assertEquals("ws://127.0.0.1:7464", transport.url)
            assertEquals(obj("""{"type":"hello","version":2,"token":"s3cret-token"}"""), obj(transport.connection!!.sent.single()))
            assertEquals(ConnState.Connected("0.1.0"), c.state.value.conn)
            assertFalse("s3cret-token" in c.state.value.toString())
            assertFalse("s3cret-token" in c.toString())
        }

    // AC2. Bug caught: a rejected token leaves connect hanging or the client looking connected.
    @Test
    fun authErrorLeavesTheClientDisconnected() =
        runTest {
            val c = client(answering("""{"type":"error","code":"auth","message":"wrong or missing token"}"""))
            c.connect("127.0.0.1", 7464, "bad")
            assertEquals(ConnState.Disconnected("authentication failed"), c.state.value.conn)
        }

    // Bug caught: an unreachable server throws out of connect instead of showing as disconnected.
    @Test
    fun unreachableServerIsDisconnectedWithAReason() =
        runTest {
            val t = FakeTransport().also { it.failWith = RuntimeException("refused") }
            val c = client(t)
            c.connect("127.0.0.1", 1, "t")
            val conn = c.state.value.conn
            assertIs<ConnState.Disconnected>(conn)
            assertTrue("refused" in conn.reason!!)
        }

    // Bug caught: list/run/answer/cancel frames carry the wrong ids or fields.
    @Test
    fun listRunAnswerAndCancelAreEncodedWithTheirIds() =
        runTest {
            val transport = answering(welcome)
            val c = client(transport)
            c.connect("h", 1, "t")
            val conn = transport.connection!!
            val listId = c.list()
            val runId = c.run("dev.dock.probe", "exitcode", buildJsonObject { put("code", 3) })!!
            c.answer(7, buildJsonObject { put("value", "x") })
            c.cancel()
            val sent = conn.sent.drop(1).map { obj(it) }
            assertEquals(obj("""{"type":"list","id":"$listId"}"""), sent[0])
            assertEquals(
                obj("""{"type":"run","id":"$runId","plugin":"dev.dock.probe","command":"exitcode","inputs":{"code":3}}"""),
                sent[1],
            )
            assertEquals(obj("""{"type":"answer","ask":7,"result":{"value":"x"}}"""), sent[2])
            assertEquals(obj("""{"type":"cancel","run":"$runId"}"""), sent[3])
            assertTrue(listId != runId)
            assertEquals(
                runId,
                c.state.value.run!!
                    .id,
            )
        }

    // Bug caught: server frames after the handshake are not reduced into state; a closed socket is not noticed.
    @Test
    fun framesAreReducedAndAClosedSocketDisconnects() =
        runTest {
            val transport = answering(welcome)
            val c = client(transport)
            c.connect("h", 1, "t")
            val runId = c.run("p", "c", null)!!
            transport.connection!!.push("""{"type":"ui","run":"$runId","method":"output","params":{"text":"hi"}}""")
            assertEquals(
                listOf(RunItem.Text("output", "hi")),
                c.state.value.run!!
                    .items,
            )
            transport.connection!!.close()
            assertIs<ConnState.Disconnected>(c.state.value.conn)
            assertTrue(
                c.state.value.run!!
                    .done != null,
            )
            assertNull(
                c.state.value.run!!
                    .ask,
            )
        }

    // Bug caught: a second run replaces the active one, so the server's busy error then wipes the first run's UI.
    @Test
    fun runIsRefusedWhileAnotherRunIsActive() =
        runTest {
            val transport = answering(welcome)
            val c = client(transport)
            c.connect("h", 1, "t")
            val first = c.run("p", "c", null)!!
            assertNull(c.run("p", "c2", null))
            assertEquals(1, transport.connection!!.sent.count { "\"run\"" in it && "\"type\":\"run\"" in it })
            assertEquals(
                first,
                c.state.value.run!!
                    .id,
            )
            transport.connection!!.push("""{"type":"done","run":"$first","code":0}""")
            assertTrue(c.run("p", "c3", null) != null, "a finished run no longer blocks a new one")
        }

    // Bug caught: overlapping sends reach the socket (java.net.http.WebSocket rejects them).
    @Test
    fun sendsAreSerialised() =
        runTest {
            val transport = answering(welcome)
            val c = client(transport)
            c.connect("h", 1, "t")
            transport.connection!!.sendDelayMs = 10
            val jobs =
                listOf(
                    launch { c.run("p", "c", null) },
                    launch { c.answer(1, buildJsonObject { put("text", "x") }) },
                    launch { c.cancel() },
                    launch { c.list() },
                )
            jobs.joinAll()
            assertEquals(4, transport.connection!!.sent.size - 1)
            assertTrue(jobs.none { it.isCancelled })
        }

    // Bug caught: a failed send marks the client disconnected but leaves the socket open and referenced.
    @Test
    fun failedSendClosesTheConnection() =
        runTest {
            val transport = answering(welcome)
            val c = client(transport)
            c.connect("h", 1, "t")
            transport.connection!!.failSend = RuntimeException("broken pipe")
            c.list()
            assertTrue(transport.connection!!.closed)
            assertIs<ConnState.Disconnected>(c.state.value.conn)
            transport.connection!!.failSend = null
            c.list()
            assertEquals(1, transport.connection!!.sent.size, "nothing is sent after the connection was dropped")
        }

    // Bug caught: request ids collide when list() is called from several threads.
    @Test
    fun requestIdsAreUniqueUnderConcurrentCalls() =
        runTest {
            val transport = answering(welcome)
            val c = client(transport)
            c.connect("h", 1, "t")
            val ids = withContext(Dispatchers.Default) { (1..200).map { async { c.list() } }.awaitAll() }
            assertEquals(200, ids.toSet().size)
        }

    // Bug caught: disconnecting mid-run leaves the run looking live or the socket open.
    @Test
    fun disconnectMidRunEndsTheRunAndClosesTheSocket() =
        runTest {
            val transport = answering(welcome)
            val c = client(transport)
            c.connect("h", 1, "t")
            c.run("p", "c", null)
            c.disconnect()
            assertTrue(transport.connection!!.closed)
            assertNotNull(
                c.state.value.run!!
                    .done,
            )
            assertEquals(ConnState.Disconnected(null), c.state.value.conn)
        }

    // Bug caught: clearing an error does not reach the client's state.
    @Test
    fun clearErrorRemovesItFromState() =
        runTest {
            val transport = answering(welcome)
            val c = client(transport)
            c.connect("h", 1, "t")
            transport.connection!!.push("""{"type":"error","id":"q","code":"internal","message":"m"}""")
            assertEquals(mapOf("q" to "m"), c.state.value.errors)
            c.clearError("q")
            assertTrue(
                c.state.value.errors
                    .isEmpty(),
            )
        }
}
