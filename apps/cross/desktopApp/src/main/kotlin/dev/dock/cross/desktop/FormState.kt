package dev.dock.cross.desktop

import dev.dock.cross.CommandInfo
import dev.dock.cross.Field
import dev.dock.cross.InputForm
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject

/** The form for one command: [fields] from its declared inputs, or a raw JSON box when it declares none. */
data class FormState(
    val fields: List<Field>?,
    val values: Map<String, String>,
    val raw: String,
) {
    val isRaw: Boolean get() = fields == null

    fun with(
        name: String,
        value: String,
    ): FormState = copy(values = values + (name to value))

    fun withRaw(text: String): FormState = copy(raw = text)

    /** The `inputs` for `run`: null (send none) for a blank raw box; a failure carries the message to show. */
    fun build(command: CommandInfo): Result<JsonObject?> {
        val inputs = command.inputs
        if (inputs != null) return InputForm.toJson(inputs, values)
        if (raw.isBlank()) return Result.success(null)
        return runCatching { Json.parseToJsonElement(raw) }
            .fold(
                { if (it is JsonObject) Result.success(it) else Result.failure(IllegalArgumentException("inputs must be a JSON object")) },
                { Result.failure(IllegalArgumentException("invalid JSON: ${it.message?.lineSequence()?.firstOrNull().orEmpty()}")) },
            )
    }

    companion object {
        fun initial(command: CommandInfo): FormState {
            val fields = command.inputs?.let { InputForm.fields(it) }
            return FormState(fields, fields.orEmpty().associate { it.name to it.initial }, "")
        }
    }
}
