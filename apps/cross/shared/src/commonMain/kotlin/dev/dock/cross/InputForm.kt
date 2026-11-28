package dev.dock.cross

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

enum class FieldKind { Text, Number, Checkbox }

/** One form field. [initial] is the declared default as text (`true`/`false` for a checkbox). */
data class Field(
    val name: String,
    val kind: FieldKind,
    val required: Boolean,
    val initial: String,
)

object InputForm {
    private val INT = Regex("-?[0-9]+")

    fun fields(inputs: List<InputInfo>): List<Field> =
        inputs.map { i ->
            val default = (i.default as? JsonPrimitive)?.content
            when (i.type) {
                "int" -> Field(i.name, FieldKind.Number, i.required, default ?: "")
                "bool" -> Field(i.name, FieldKind.Checkbox, i.required, ((i.default as? JsonPrimitive)?.booleanOrNull ?: false).toString())
                else -> Field(i.name, FieldKind.Text, i.required, default ?: "")
            }
        }

    /**
     * The `inputs` object for `run` from the form's [values] (by field name). Blank optional values, and bools left at their declared default, are left out so the
     * server fills its defaults; a missing required value or an unparsable int fails with a message naming the field.
     */
    fun toJson(
        inputs: List<InputInfo>,
        values: Map<String, String>,
    ): Result<JsonObject> =
        runCatching {
            buildJsonObject {
                for (i in inputs) {
                    val raw = values[i.name].orEmpty()
                    if (i.type == "bool") {
                        val value = raw == "true"
                        val declared = (i.default as? JsonPrimitive)?.booleanOrNull
                        if (value != declared) put(i.name, value)
                        continue
                    }
                    val text = if (i.type == "int") raw.trim() else raw
                    if (text.isBlank()) {
                        require(!i.required) { "${i.name} is required" }
                        continue
                    }
                    if (i.type == "int") {
                        put(i.name, requireNotNull(text.takeIf { INT.matches(it) }?.toLongOrNull()) { "${i.name} must be a whole number" })
                    } else {
                        put(i.name, text)
                    }
                }
            }
        }
}
