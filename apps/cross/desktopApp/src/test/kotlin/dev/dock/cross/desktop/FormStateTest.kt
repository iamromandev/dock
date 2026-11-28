package dev.dock.cross.desktop

import dev.dock.cross.CommandInfo
import dev.dock.cross.InputInfo
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class FormStateTest {
    private val withInputs =
        CommandInfo(
            "go",
            "",
            listOf(
                InputInfo("name", "str", true, null),
                InputInfo("n", "int", false, JsonPrimitive(3)),
            ),
        )
    private val raw = CommandInfo("raw", "", null)

    @Test
    fun declaredDefaultsFillTheForm() {
        val s = FormState.initial(withInputs)
        assertEquals(mapOf("name" to "", "n" to "3"), s.values)
        assertEquals(false, s.isRaw)
    }

    @Test
    fun aMissingRequiredValueFailsAndFilledValuesBuildTheInputs() {
        val s = FormState.initial(withInputs)
        assertTrue(
            s
                .build(withInputs)
                .exceptionOrNull()!!
                .message!!
                .contains("name"),
        )
        val built = s.with("name", "x").build(withInputs).getOrThrow()
        assertEquals(JsonObject(mapOf("name" to JsonPrimitive("x"), "n" to JsonPrimitive(3))), built)
    }

    @Test
    fun aCommandWithNoDeclaredInputsTakesRawJson() {
        val s = FormState.initial(raw)
        assertTrue(s.isRaw)
        assertNull(s.build(raw).getOrThrow())
        assertEquals(JsonPrimitive(1), s.withRaw("""{"a":1}""").build(raw).getOrThrow()!!["a"])
    }

    @Test
    fun invalidOrNonObjectRawJsonSendsNothing() {
        val s = FormState.initial(raw)
        assertTrue(s.withRaw("{oops").build(raw).isFailure)
        assertTrue(s.withRaw("[1]").build(raw).isFailure)
    }

    @Test
    fun aCommandWithEmptyInputsSendsAnEmptyObject() {
        val none = CommandInfo("n", "", emptyList())
        assertEquals(JsonObject(emptyMap()), FormState.initial(none).build(none).getOrThrow())
    }
}
