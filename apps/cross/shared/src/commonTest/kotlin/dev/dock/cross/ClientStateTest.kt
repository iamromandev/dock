package dev.dock.cross

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.system.measureTimeMillis
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class ClientStateTest {
    private fun text(s: String): JsonObject = buildJsonObject { put("text", s) }

    private fun running(): ClientState = runStarted(ClientState(conn = ConnState.Connected("0.1.0")), "r1", "p", "c")

    // AC4. Bug caught: the plugins reply is not stored, or its commands lose their summary/inputs.
    @Test
    fun pluginsFrameIsListed() {
        val info = PluginInfo("dev.dock.probe", listOf(CommandInfo("code", "s", null)))
        assertEquals(listOf(info), reduce(ClientState(), ServerFrame.Plugins("L1", listOf(info))).plugins)
    }

    // AC9. Bug caught: an error is dropped, or attached to the wrong request.
    @Test
    fun everyErrorCodeIsAttachedToItsRequest() {
        var s = running()
        for (code in listOf("unknown_plugin", "bad_inputs", "busy", "capacity", "bad_message", "internal")) {
            s = reduce(s, ServerFrame.Error("req-$code", code, "msg $code"))
        }
        assertEquals(6, s.errors.size)
        assertEquals("msg busy", s.errors["req-busy"])
        s = reduce(s, ServerFrame.Error(null, "internal", "no id"))
        assertEquals("no id", s.errors[ClientState.GENERAL_ERROR])
        assertEquals(ConnState.Connected("0.1.0"), s.conn)
        val failedRun = reduce(running(), ServerFrame.Error("r1", "busy", "m"))
        assertNull(failedRun.run, "a run the server refused is not running")
        assertEquals("m", failedRun.errors["r1"])
    }

    // AC2. Bug caught: an auth or version rejection leaves the client looking connected.
    @Test
    fun authAndVersionErrorsDisconnect() {
        val connecting = ClientState(conn = ConnState.Connecting)
        assertEquals(
            ConnState.Disconnected("authentication failed"),
            reduce(connecting, ServerFrame.Error(null, "auth", "wrong or missing token")).conn,
        )
        val v = reduce(connecting, ServerFrame.Error(null, "version", "unsupported version; supported: [2]")).conn
        assertIs<ConnState.Disconnected>(v)
        assertTrue("[2]" in v.reason!!)
    }

    // AC10. Bug caught: a dropped socket leaves a run that looks live.
    @Test
    fun connectionLostDisconnectsAndEndsTheRun() {
        val s = connectionLost(reduce(running(), ServerFrame.Ask("r1", 1, "prompt_user", JsonObject(emptyMap()))))
        assertIs<ConnState.Disconnected>(s.conn)
        assertNotNull(s.run!!.done)
        assertNull(s.run.ask)
    }

    private fun clearOf(
        id: String,
        kind: String,
    ) = ServerFrame.Ui(
        "r1",
        "clear",
        buildJsonObject {
            put("id", id)
            put("kind", kind)
        },
    )

    private fun barOf(id: String) = ServerFrame.Ui("r1", "bar", buildJsonObject { put("id", id) })

    // AC11. Bug caught: clear wipes every item (output text, other bars) instead of only the live item named by id and kind.
    @Test
    fun clearRemovesOnlyTheNamedLiveItem() {
        var s = reduce(running(), ServerFrame.Ui("r1", "output", text("a")))
        s = reduce(reduce(s, barOf("x")), barOf("y"))
        s = reduce(s, clearOf("x", "bar"))
        assertEquals(listOf("output", "y"), s.run!!.items.map { (it as? RunItem.Bar)?.id ?: "output" })
        assertEquals(s, reduce(s, clearOf("nope", "bar")), "an unknown id is a no-op")
        assertEquals(s, reduce(s, clearOf("y", "table")), "same id of another kind is untouched")
    }

    // Bug caught: an error for an older request id cancels the active run (a refused second run kills the first).
    @Test
    fun errorForAnotherRequestKeepsTheActiveRun() {
        val s = reduce(running(), ServerFrame.Error("r2", "busy", "m"))
        assertEquals("r1", s.run!!.id)
        assertEquals("m", s.errors["r2"])
    }

    // Bug caught: errors pile up forever, or a repeated error for one id is not replaced.
    @Test
    fun errorsAreReplacedPerIdAndCanBeCleared() {
        var s = reduce(running(), ServerFrame.Error("q", "internal", "one"))
        s = reduce(s, ServerFrame.Error("q", "internal", "two"))
        assertEquals(mapOf("q" to "two"), s.errors)
        assertTrue(clearError(s, "q").errors.isEmpty())
    }

    // Bug caught: a live bar/table update appends a new row each time instead of replacing the old one.
    @Test
    fun barWithSameIdIsReplaced() {
        fun bar(p: Int) =
            ServerFrame.Ui(
                "r1",
                "bar",
                buildJsonObject {
                    put("id", "a")
                    put("percent", p)
                },
            )
        val s = reduce(reduce(running(), bar(1)), bar(2))
        assertEquals(1, s.run!!.items.size)
        assertEquals(2, (s.run.items.single() as RunItem.Bar).params["percent"].toString().toInt())
    }

    // Bug caught: a frame of another run is shown; done does not close the ask.
    @Test
    fun framesOfOtherRunsAreIgnoredAndDoneEndsRun() {
        var s = reduce(running(), ServerFrame.Ui("other", "output", text("x")))
        assertTrue(s.run!!.items.isEmpty())
        s = reduce(s, ServerFrame.Ask("r1", 4, "prompt_user", JsonObject(emptyMap())))
        assertEquals(4, s.run!!.ask!!.ask)
        s = reduce(s, ServerFrame.Done("r1", 3, null))
        assertEquals(DoneInfo(3, null), s.run!!.done)
        assertNull(s.run.ask)
    }

    // AC13. Bug caught: frames are lost or reduction is quadratic-slow under a burst.
    @Test
    fun thousandUiFramesAreReducedFastWithNoneLost() {
        var s = running()
        val ms = measureTimeMillis { repeat(1000) { s = reduce(s, ServerFrame.Ui("r1", "output", text("line $it"))) } }
        assertEquals(1000, s.run!!.items.size)
        assertEquals(RunItem.Text("output", "line 999"), s.run.items.last())
        assertTrue(ms < 2000, "took $ms ms")
    }
}
