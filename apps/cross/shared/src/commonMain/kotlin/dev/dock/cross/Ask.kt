package dev.dock.cross

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** What the plugin is asking, parsed from an `ask` frame's `method` and `params`. */
sealed interface AskInfo {
    /** `prompt_user`: one line of text is the answer. */
    data class Prompt(
        val text: String,
    ) : AskInfo

    /** `form`: one value per field. */
    data class Form(
        val title: String?,
        val fields: List<FormField>,
    ) : AskInfo {
        /** True when a field has a type this client does not know; the UI should answer [AskAnswer.eof] for such a form. */
        val hasUnknown: Boolean get() = fields.any { it is FormField.Unknown }
    }

    /** An unknown method or params that do not fit; the UI answers [AskAnswer.eof]. */
    data class Raw(
        val method: String,
        val params: JsonObject,
    ) : AskInfo

    companion object {
        fun parse(
            method: String,
            params: JsonObject,
        ): AskInfo =
            when (method) {
                "prompt_user" -> params.text("prompt")?.let { Prompt(it) } ?: Raw(method, params)
                "form" -> form(params) ?: Raw(method, params)
                else -> Raw(method, params)
            }

        private fun form(p: JsonObject): Form? {
            val fields = (p["fields"] as? JsonArray) ?: return null
            val parsed = fields.map { f -> (f as? JsonObject)?.let { field(it) } ?: return null }
            return Form(p.text("title"), parsed)
        }

        private fun field(f: JsonObject): FormField? {
            val name = f.text("name") ?: return null
            val label = f.text("label") ?: name
            val default = f["default"]
            return when (f.text("type")) {
                "text" -> {
                    FormField.Text(name, label, f.text("default"), (f["secret"] as? JsonPrimitive)?.booleanOrNull ?: false)
                }

                "confirm" -> {
                    FormField.Confirm(name, label, (default as? JsonPrimitive)?.booleanOrNull)
                }

                "choice" -> {
                    val choices =
                        (f["choices"] as? JsonArray)?.map {
                            (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content
                                ?: return null
                        }
                    if (choices == null) null else FormField.Choice(name, label, choices, f.text("default"))
                }

                else -> {
                    FormField.Unknown(name, f)
                }
            }
        }

        private fun JsonObject.text(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content
    }
}

sealed interface FormField {
    val name: String

    data class Text(
        override val name: String,
        val label: String,
        val default: String?,
        val secret: Boolean,
    ) : FormField

    data class Confirm(
        override val name: String,
        val label: String,
        val default: Boolean?,
    ) : FormField

    data class Choice(
        override val name: String,
        val label: String,
        val choices: List<String>,
        val default: String?,
    ) : FormField

    /** A field type this client does not know, kept as sent. */
    data class Unknown(
        override val name: String,
        val raw: JsonObject,
    ) : FormField
}

/** The `result` objects an `answer` carries, in the shapes protocol/PROTOCOL.md defines for the host. */
object AskAnswer {
    fun prompt(text: String): JsonObject = buildJsonObject { put("text", text) }

    /** [values] by field name: a string for text and choice, a boolean for confirm. */
    fun form(values: Map<String, JsonElement>): JsonObject = buildJsonObject { put("values", JsonObject(values)) }

    /** Input has ended (or the UI cannot answer): the plugin sees `{"eof": true}`. */
    fun eof(): JsonObject = buildJsonObject { put("eof", true) }
}
