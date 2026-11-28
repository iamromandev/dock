package dev.dock.cross

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

sealed interface ServerFrame {
    data class Welcome(
        val version: Int,
        val server: String,
    ) : ServerFrame

    data class Plugins(
        val id: String?,
        val plugins: List<PluginInfo>,
    ) : ServerFrame

    data class Ui(
        val run: String,
        val method: String,
        val params: JsonObject,
    ) : ServerFrame

    data class Ask(
        val run: String,
        val ask: Int,
        val method: String,
        val params: JsonObject,
    ) : ServerFrame

    data class Done(
        val run: String,
        val code: Int,
        val error: String?,
    ) : ServerFrame

    data class Error(
        val id: String?,
        val code: String,
        val message: String,
    ) : ServerFrame

    data class Unknown(
        val raw: String,
    ) : ServerFrame
}

data class PluginInfo(
    val id: String,
    val commands: List<CommandInfo>,
)

/** `inputs` is null when the command declares none (it takes whatever it is given); empty means it takes none. */
data class CommandInfo(
    val name: String,
    val summary: String,
    val inputs: List<InputInfo>?,
)

data class InputInfo(
    val name: String,
    val type: String,
    val required: Boolean,
    val default: JsonElement?,
)

object Protocol {
    const val VERSION = 2

    /** The frame in [text]; anything malformed or of an unknown type is [ServerFrame.Unknown]. Never throws. */
    fun decode(text: String): ServerFrame =
        try {
            val o = Json.parseToJsonElement(text).jsonObject
            when (o.str("type")) {
                "welcome" -> ServerFrame.Welcome(o.int("version"), o.str("server"))
                "plugins" -> ServerFrame.Plugins(o.strOrNull("id"), o.array("plugins").map { plugin(it.jsonObject) })
                "ui" -> ServerFrame.Ui(o.str("run"), o.str("method"), o.obj("params"))
                "ask" -> ServerFrame.Ask(o.str("run"), o.int("ask"), o.str("method"), o.obj("params"))
                "done" -> ServerFrame.Done(o.str("run"), o.int("code"), o.strOrNull("error"))
                "error" -> ServerFrame.Error(o.strOrNull("id"), o.str("code"), o.str("message"))
                else -> ServerFrame.Unknown(text)
            }
        } catch (_: Exception) {
            ServerFrame.Unknown(text)
        }

    private fun plugin(o: JsonObject) = PluginInfo(o.str("id"), o.array("commands").map { command(it.jsonObject) })

    private fun command(o: JsonObject): CommandInfo {
        val inputs = o["inputs"]?.takeIf { it !is JsonNull }?.jsonArray?.map { input(it.jsonObject) }
        return CommandInfo(o.str("name"), o.strOrNull("summary") ?: "", inputs)
    }

    private fun input(o: JsonObject) =
        InputInfo(
            o.str("name"),
            o.str("type"),
            o["required"]?.jsonPrimitive?.boolean ?: false,
            o["default"]?.takeIf { it !is JsonNull },
        )

    private fun JsonObject.str(key: String): String = getValue(key).jsonPrimitive.also { require(it.isString) }.content

    private fun JsonObject.strOrNull(key: String): String? = get(key)?.jsonPrimitive?.contentOrNull

    private fun JsonObject.int(key: String): Int = getValue(key).jsonPrimitive.int

    private fun JsonObject.obj(key: String): JsonObject = getValue(key).jsonObject

    private fun JsonObject.array(key: String) = getValue(key).jsonArray
}

/** The client's messages as JSON text, in the shapes docs/SERVER_PROTOCOL.md defines (apps/server). */
object ClientFrame {
    fun hello(token: String): String =
        buildJsonObject {
            put("type", "hello")
            put("version", Protocol.VERSION)
            put("token", token)
        }.toString()

    fun list(id: String): String =
        buildJsonObject {
            put("type", "list")
            put("id", id)
        }.toString()

    fun run(
        id: String,
        plugin: String,
        command: String,
        inputs: JsonObject?,
    ): String =
        buildJsonObject {
            put("type", "run")
            put("id", id)
            put("plugin", plugin)
            put("command", command)
            if (inputs != null) put("inputs", inputs)
        }.toString()

    fun answer(
        ask: Int,
        result: JsonObject,
    ): String =
        buildJsonObject {
            put("type", "answer")
            put("ask", ask)
            put("result", result)
        }.toString()

    fun cancel(run: String): String =
        buildJsonObject {
            put("type", "cancel")
            put("run", run)
        }.toString()
}
