package dev.dock.cross.desktop

import dev.dock.cross.FormField
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive

/** The starting value of every known field, by name, in the types [dev.dock.cross.AskAnswer.form] takes. */
fun formDefaults(fields: List<FormField>): Map<String, JsonElement> =
    buildMap {
        for (f in fields) {
            when (f) {
                is FormField.Text -> put(f.name, JsonPrimitive(f.default ?: ""))
                is FormField.Confirm -> put(f.name, JsonPrimitive(f.default ?: false))
                is FormField.Choice -> put(f.name, JsonPrimitive(f.default ?: f.choices.firstOrNull() ?: ""))
                is FormField.Unknown -> Unit
            }
        }
    }
