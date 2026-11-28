package dev.dock.cross

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

class ProtocolTest {
    // Bug caught: a field renamed or misread (e.g. `ask` as string, `default` dropped) in any server frame.
    @Test
    fun decodesEveryServerFrameShapeFromTheDoc() {
        assertEquals(ServerFrame.Welcome(2, "0.1.0"), Protocol.decode("""{"type":"welcome","version":2,"server":"0.1.0"}"""))

        val plugins =
            Protocol.decode(
                """{"type":"plugins","id":"L1","plugins":[{"id":"dev.dock.probe","commands":[
                |{"name":"code","summary":"s","inputs":null},
                |{"name":"exitcode","summary":"e","inputs":[{"name":"code","type":"int","required":true,"default":null}]},
                |{"name":"fetch","summary":"f","inputs":[{"name":"retries","type":"int","required":false,"default":3}]}]}]}
                """.trimMargin(),
            )
        assertIs<ServerFrame.Plugins>(plugins)
        val cmds = plugins.plugins.single().commands
        assertEquals(null, cmds[0].inputs)
        assertEquals(InputInfo("code", "int", true, null), cmds[1].inputs!!.single())
        assertEquals(
            3,
            cmds[2]
                .inputs!!
                .single()
                .default!!
                .jsonPrimitive.int,
        )

        assertEquals(
            ServerFrame.Ui("r1", "output", buildJsonObject { put("text", "hi") }),
            Protocol.decode("""{"type":"ui","run":"r1","method":"output","params":{"text":"hi"}}"""),
        )
        assertEquals(
            ServerFrame.Ask("r1", 7, "prompt_user", buildJsonObject { put("prompt", "Name?") }),
            Protocol.decode("""{"type":"ask","run":"r1","ask":7,"method":"prompt_user","params":{"prompt":"Name?"}}"""),
        )
        assertEquals(ServerFrame.Done("r1", 130, null), Protocol.decode("""{"type":"done","run":"r1","code":130}"""))
        assertEquals(ServerFrame.Done("r1", 1, "boom"), Protocol.decode("""{"type":"done","run":"r1","code":1,"error":"boom"}"""))
        assertEquals(
            ServerFrame.Error("r2", "busy", "m"),
            Protocol.decode("""{"type":"error","id":"r2","code":"busy","message":"m"}"""),
        )
        assertEquals(ServerFrame.Error(null, "auth", "m"), Protocol.decode("""{"type":"error","code":"auth","message":"m"}"""))
    }

    // Bug caught: a malformed or future frame throws and kills the receive loop.
    @Test
    fun badOrUnknownInputIsUnknownAndNeverThrows() {
        for (text in listOf("not json", "[]", """{"type":"nope"}""", """{"type":"done"}""", """{"type":"ui","run":"r"}""", "")) {
            assertEquals(ServerFrame.Unknown(text), Protocol.decode(text), text)
        }
    }

    // Bug caught: a client frame with a wrong field name or version is rejected by the server.
    @Test
    fun clientFramesCarryTheDocumentedFields() {
        fun obj(s: String): JsonObject =
            kotlinx.serialization.json.Json
                .parseToJsonElement(s)
                .jsonObject

        assertEquals(obj("""{"type":"hello","version":2,"token":"t"}"""), obj(ClientFrame.hello("t")))
        assertEquals(obj("""{"type":"list","id":"L1"}"""), obj(ClientFrame.list("L1")))
        assertEquals(
            obj("""{"type":"run","id":"r1","plugin":"p","command":"c","inputs":{"code":3}}"""),
            obj(ClientFrame.run("r1", "p", "c", buildJsonObject { put("code", 3) })),
        )
        assertEquals(obj("""{"type":"run","id":"r1","plugin":"p","command":"c"}"""), obj(ClientFrame.run("r1", "p", "c", null)))
        assertEquals(
            obj("""{"type":"answer","ask":7,"result":{"value":"x"}}"""),
            obj(ClientFrame.answer(7, buildJsonObject { put("value", JsonPrimitive("x")) })),
        )
        assertEquals(obj("""{"type":"cancel","run":"r1"}"""), obj(ClientFrame.cancel("r1")))
    }
}
