package dev.dock.cross

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

class AskInfoTest {
    private fun obj(s: String): JsonObject = Json.parseToJsonElement(s).jsonObject

    // The uiprobe fixture's `ask` command, as the host forwards it.
    private val settings =
        obj(
            """{"title":"Settings","fields":[
              {"name":"name","label":"Your name","type":"text","default":"Ada"},
              {"name":"token","label":"Token","type":"text","secret":true},
              {"name":"ok","label":"Overwrite?","type":"confirm","default":false},
              {"name":"mirror","label":"Mirror","type":"choice","choices":["eu","us","asia"],"default":"eu"}]}""",
        )

    // Bug caught: the prompt text is lost, or a prompt is parsed as a form.
    @Test
    fun promptParsesItsText() {
        assertEquals(AskInfo.Prompt("Name?"), AskInfo.parse("prompt_user", obj("""{"prompt":"Name?"}""")))
    }

    // Bug caught: a field type loses its default, secret flag or choices.
    @Test
    fun formParsesEveryFieldType() {
        val form = assertIs<AskInfo.Form>(AskInfo.parse("form", settings))
        assertEquals("Settings", form.title)
        assertEquals(
            listOf(
                FormField.Text("name", "Your name", "Ada", false),
                FormField.Text("token", "Token", null, true),
                FormField.Confirm("ok", "Overwrite?", false),
                FormField.Choice("mirror", "Mirror", listOf("eu", "us", "asia"), "eu"),
            ),
            form.fields,
        )
    }

    // Bug caught: the server's normalised form (empty title, null defaults, no raw values) parses to a blank dialog title.
    @Test
    fun serverNormalisedFormHasNoBlankTitle() {
        val form =
            assertIs<AskInfo.Form>(
                AskInfo.parse(
                    "form",
                    obj(
                        """{"title":"","fields":[{"name":"m","label":"M","type":"choice","default":null,"secret":false,
                          "choices":["eu�1","us"]}]}""",
                    ),
                ),
            )
        assertEquals(null, form.title)
        assertEquals(listOf(FormField.Choice("m", "M", listOf("eu�1", "us"), null)), form.fields)
    }

    // Bug caught: an unknown field type or malformed params throws, or is dropped silently so the UI cannot answer eof.
    @Test
    fun unknownFieldTypeAndMalformedParamsAreKeptRaw() {
        val field = obj("""{"name":"n","label":"L","type":"date"}""")
        val form =
            assertIs<AskInfo.Form>(
                AskInfo.parse("form", buildJsonObject { put("fields", kotlinx.serialization.json.JsonArray(listOf(field))) }),
            )
        assertEquals(listOf(FormField.Unknown("n", field)), form.fields)
        assertEquals(true, form.hasUnknown)
        assertEquals(AskInfo.Raw("other", JsonObject(emptyMap())), AskInfo.parse("other", JsonObject(emptyMap())))
        assertIs<AskInfo.Raw>(AskInfo.parse("form", obj("""{"fields":"nope"}""")))
    }

    // Bug caught: answers are not in the shape the host expects ({text}, {values}, {eof}).
    @Test
    fun answersHaveTheHostShapes() {
        assertEquals(obj("""{"text":"Ada"}"""), AskAnswer.prompt("Ada"))
        assertEquals(
            obj("""{"values":{"name":"Bob","ok":true}}"""),
            AskAnswer.form(mapOf("name" to JsonPrimitive("Bob"), "ok" to JsonPrimitive(true))),
        )
        assertEquals(obj("""{"eof":true}"""), AskAnswer.eof())
    }

    // Bug caught: the parsed ask is not attached to the run's state.
    @Test
    fun askFrameCarriesParsedInfoInRunState() {
        val s = runStarted(ClientState(), "r1", "p", "c")
        val ask = reduce(s, ServerFrame.Ask("r1", 1, "form", settings)).run!!.ask!!
        assertIs<AskInfo.Form>(ask.info)
    }
}
