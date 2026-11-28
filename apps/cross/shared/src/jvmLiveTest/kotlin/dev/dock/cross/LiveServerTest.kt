package dev.dock.cross

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.AfterClass
import org.junit.BeforeClass
import java.io.File
import java.util.concurrent.TimeUnit
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class LiveServerTest {
    private lateinit var scope: CoroutineScope
    private lateinit var client: DockClient

    companion object {
        private lateinit var server: LiveServer
        private var before = emptySet<String>()
        private var owner: String? = null

        // One server (and one fixture install) for the class; each test gets its own client.
        @BeforeClass
        @JvmStatic
        fun startServer() {
            before = containers(null)
            server = LiveServer().start()
            // Without the server's Python pid the leak check could only compare all dock containers on the machine.
            val pid = checkNotNull(server.pythonPid) { "server's python process not found under uv\n${server.log()}" }
            owner = "${hostname()}:$pid"
        }

        // Bug caught (NFR4): a server, container or temp dir left behind after the tests.
        @AfterClass
        @JvmStatic
        fun stopServer() {
            val home = server
            home.close()
            assertTrue(home.exited(), "server process still alive")
            // Only containers of our server (owner label host:pid); without its pid, only containers new since the start.
            // `--rm` removal is asynchronous, so give it a moment, as the server's own tests do.
            val end = System.nanoTime() + TimeUnit.SECONDS.toNanos(15)
            var left = leftover()
            while (left.isNotEmpty() && System.nanoTime() < end) {
                Thread.sleep(200)
                left = leftover()
            }
            assertEquals(emptySet(), left, "containers left behind")
        }

        private fun leftover(): Set<String> = containers(owner) - (if (owner == null) before else emptySet())

        private fun hostname(): String {
            val p =
                ProcessBuilder(
                    "uv",
                    "run",
                    "--project",
                    File(System.getProperty("dock.repo"), "apps/server").path,
                    "python",
                    "-c",
                    "import socket; print(socket.gethostname())",
                ).redirectErrorStream(true).start()
            val out = p.inputStream.bufferedReader().readText()
            p.waitFor(60, TimeUnit.SECONDS)
            return out.trim()
        }

        private fun containers(owner: String?): Set<String> {
            val rt = System.getenv("DOCK_TEST_RUNTIME")?.takeIf { it.isNotBlank() } ?: "docker"
            val label = if (owner == null) "label=dev.dock.owner" else "label=dev.dock.owner=$owner"
            val p = ProcessBuilder(rt, "ps", "-a", "-q", "--filter", label).redirectErrorStream(true).start()
            val out = p.inputStream.bufferedReader().readText()
            p.waitFor(60, TimeUnit.SECONDS)
            return out.split(Regex("\\s+")).filter { it.isNotEmpty() }.toSet()
        }
    }

    @BeforeTest
    fun setUp() {
        scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        client = DockClient(JdkTransport(), scope)
    }

    // Leaves the server idle for the next test: an active run is cancelled and awaited before disconnecting.
    @AfterTest
    fun tearDown() {
        try {
            runBlocking {
                val run = client.state.value.run
                if (run != null && run.done == null) {
                    client.cancel()
                    runCatching { withTimeout(15_000) { client.state.first { it.run?.done != null } } }
                }
            }
        } finally {
            client.disconnect()
            scope.cancel()
        }
    }

    private fun connect() = runBlocking { withTimeout(15_000) { client.connect("127.0.0.1", server.port, server.token) } }

    private fun runUntilDone(
        plugin: String,
        command: String,
        script: String? = null,
        timeoutMs: Long = 60_000,
        onAsk: suspend (AskState) -> Unit = {},
    ): RunState =
        runBlocking {
            val inputs = script?.let { s -> buildJsonObject { put("script", s) } }
            client.run(plugin, command, inputs) ?: error("run refused")
            await(timeoutMs) {
                var answered = -1
                client.state
                    .first { s ->
                        val ask = s.run?.ask
                        if (ask != null && ask.ask != answered) {
                            answered = ask.ask
                            onAsk(ask)
                        }
                        s.run?.done != null
                    }.run!!
            }
        }

    // Bug caught: the real socket delivers ui frames out of order or drops them.
    @Test
    fun showScriptStreamsItsItemsThenFinishes() {
        connect()
        val run = runUntilDone("dev.dock.uiprobe", "show", "basic")
        assertEquals(0, run.done?.code)
        assertEquals(listOf("Bar", "Bar", "Table", "Panel", "Text"), run.items.map { it::class.simpleName })
        assertEquals(RunItem.Text("output", "basic done\n"), run.items.last())
    }

    // Bug caught: an answer is not delivered to the plugin over the real socket.
    @Test
    fun promptAnswerReachesThePlugin() {
        connect()
        val run = runUntilDone("dev.dock.uiprobe", "prompt") { client.answer(it.ask, AskAnswer.prompt("Ada")) }
        assertEquals(0, run.done?.code)
        assertEquals(listOf<RunItem>(RunItem.Text("output", "{\"text\": \"Ada\"}\n")), run.items)
    }

    // Bug caught: cancel is not sent, or the run does not end with 130.
    @Test
    fun cancelEndsASlowRunWith130() {
        connect()
        runBlocking {
            client.run("dev.dock.uiprobe", "show", buildJsonObject { put("script", "slow") })
            await(60_000) { client.state.first { s -> s.run?.items?.any { it is RunItem.Text } == true } }
            client.cancel()
            val done = await(5_000) { client.state.first { it.run?.done != null } }
            assertEquals(130, done.run?.done?.code)
        }
    }

    // Bug caught: a wrong token is reported as anything but an authentication failure.
    @Test
    fun wrongTokenIsRefused() {
        runBlocking { withTimeout(15_000) { client.connect("127.0.0.1", server.port, "wrong") } }
        assertEquals(ConnState.Disconnected("authentication failed"), client.state.value.conn)
    }

    // Bug caught: the token leaks into client state or the server log.
    @Test
    fun tokenAppearsNeitherInClientStateNorServerLog() {
        connect()
        runUntilDone("dev.dock.uiprobe", "show", "basic")
        assertTrue(
            !client.state.value
                .toString()
                .contains(server.token),
            "token in client state",
        )
        assertTrue(!server.log().contains(server.token), "token in server log")
    }

    // Bug caught: the plugin list or its input declarations are mis-decoded.
    @Test
    fun listShowsExitcodeWithItsRequiredIntInput() {
        connect()
        runBlocking {
            val id = client.list()
            val s = await(30_000) { client.state.first { it.plugins.isNotEmpty() } }
            assertTrue(id.startsWith("req-"))
            val probe = s.plugins.first { it.id.endsWith("probe") && !it.id.endsWith("uiprobe") }
            val input =
                probe.commands
                    .first { it.name == "exitcode" }
                    .inputs!!
                    .single()
            assertEquals(Triple("code", "int", true), Triple(input.name, input.type, input.required))
        }
    }

    /** [withTimeout] whose failure shows the client state and the server's log. */
    private suspend fun <T> await(
        ms: Long,
        block: suspend () -> T,
    ): T =
        try {
            withTimeout(ms) { block() }
        } catch (e: TimeoutCancellationException) {
            throw AssertionError("timed out after $ms ms\nstate: ${client.state.value}\nserver log:\n${server.log()}", e)
        }
}
