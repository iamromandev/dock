package dev.dock.cross.desktop

import dev.dock.cross.AskAnswer
import dev.dock.cross.FormField
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals

class AskModelTest {
    @Test
    fun formAnswerStartsFromDefaultsInTheTypesTheProtocolTakes() {
        val fields =
            listOf(
                FormField.Text("t", "T", "hi", false),
                FormField.Text("u", "U", null, true),
                FormField.Confirm("c", "C", null),
                FormField.Choice("k", "K", listOf("a", "b"), null),
                FormField.Choice("j", "J", listOf("a", "b"), "b"),
                FormField.Unknown("z", JsonObject(emptyMap())),
            )
        assertEquals(
            """{"values":{"t":"hi","u":"","c":false,"k":"a","j":"b"}}""",
            AskAnswer.form(formDefaults(fields)).toString(),
        )
    }

    @Test
    fun theDialogAnswersWithTheShapesTheHostTakes() {
        assertEquals("""{"text":"Ada"}""", AskAnswer.prompt("Ada").toString())
        assertEquals("""{"eof":true}""", AskAnswer.eof().toString())
        assertEquals(
            """{"values":{"k":"b","c":true}}""",
            AskAnswer.form(mapOf("k" to JsonPrimitive("b"), "c" to JsonPrimitive(true))).toString(),
        )
    }
}
