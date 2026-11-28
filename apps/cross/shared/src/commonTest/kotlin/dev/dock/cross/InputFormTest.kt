package dev.dock.cross

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class InputFormTest {
    private val exitcode = listOf(InputInfo("code", "int", true, null))
    private val fetch =
        listOf(
            InputInfo("url", "string", true, null),
            InputInfo("retries", "int", false, JsonPrimitive(3)),
            InputInfo("quiet", "bool", false, null),
            InputInfo("output", "string", false, null),
        )

    // AC5. Bug caught: the probe's required int `code` renders as a text or optional field.
    @Test
    fun requiredIntIsARequiredNumberField() {
        assertEquals(listOf(Field("code", FieldKind.Number, true, "")), InputForm.fields(exitcode))
    }

    // Bug caught: types map to the wrong widget, or declared defaults are not pre-filled.
    @Test
    fun typesMapToWidgetsAndDefaultsAreFilled() {
        assertEquals(
            listOf(
                Field("url", FieldKind.Text, true, ""),
                Field("retries", FieldKind.Number, false, "3"),
                Field("quiet", FieldKind.Checkbox, false, "false"),
                Field("output", FieldKind.Text, false, ""),
            ),
            InputForm.fields(fetch),
        )
    }

    // AC5. Bug caught: the int is sent as a string, or empty optional fields override the server's defaults.
    @Test
    fun valuesBecomeTypedJsonAndBlankOptionalsAreOmitted() {
        assertEquals(buildJsonObject { put("code", 3) }, InputForm.toJson(exitcode, mapOf("code" to " 3 ")).getOrThrow())
        val json = InputForm.toJson(fetch, mapOf("url" to "u", "retries" to "", "quiet" to "true", "output" to "")).getOrThrow()
        assertEquals(
            buildJsonObject {
                put("url", "u")
                put("quiet", true)
            },
            json,
        )
    }

    // AC5. Bug caught: a missing required value or non-numeric int is sent to the server instead of caught in the form.
    @Test
    fun missingRequiredOrBadIntFails() {
        assertTrue(InputForm.toJson(exitcode, mapOf("code" to "")).isFailure)
        assertTrue(InputForm.toJson(exitcode, emptyMap()).isFailure)
        assertTrue(InputForm.toJson(exitcode, mapOf("code" to "abc")).isFailure)
        assertTrue(InputForm.toJson(fetch, mapOf("url" to "u", "retries" to "x")).isFailure)
        assertTrue(InputForm.toJson(exitcode, mapOf("code" to "+5")).isFailure, "a leading plus is not accepted")
        assertEquals(buildJsonObject { put("code", -5) }, InputForm.toJson(exitcode, mapOf("code" to "-5")).getOrThrow())
    }

    // Bug caught: a bool left at its default is sent anyway, overriding the server's default (a default-true bool sent false).
    @Test
    fun boolAtItsDefaultIsOmitted() {
        val flags = listOf(InputInfo("on", "bool", false, JsonPrimitive(true)), InputInfo("off", "bool", false, null))
        assertEquals(
            buildJsonObject { put("off", false) },
            InputForm.toJson(flags, mapOf("on" to "true", "off" to "false")).getOrThrow(),
            "a bool with no declared default is always sent",
        )
        assertEquals(
            buildJsonObject {
                put("on", false)
                put("off", true)
            },
            InputForm.toJson(flags, mapOf("on" to "false", "off" to "true")).getOrThrow(),
        )
    }
}
