package dev.dock.cross.desktop

import dev.dock.cross.DoneInfo
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class RunDisplayTest {
    private fun obj(s: String) = Json.parseToJsonElement(s).jsonObject

    @Test
    fun tableRowsAreTextAndPaddedToTheColumns() {
        val t = tableView(obj("""{"id":"t","title":"T","columns":["a","b"],"rows":[["x",1],["y"]]}"""))
        assertEquals(TableView("T", listOf("a", "b"), listOf(listOf("x", "1"), listOf("y", ""))), t)
    }

    @Test
    fun barPercentBecomesAClampedFractionAndNullMeansUnknown() {
        assertEquals(BarView("L", 0.5f, false), barView(obj("""{"id":"b","label":"L","percent":50}""")))
        assertEquals(1f, barView(obj("""{"id":"b","percent":250}""")).fraction)
        assertNull(barView(obj("""{"id":"b","percent":null,"done":true}""")).fraction)
        assertEquals("m", barView(obj("""{"id":"b","message":"m","percent":1}""")).label)
    }

    @Test
    fun anEmptyLabelOrMessageLeavesNoDanglingSeparator() {
        // the server always sends both, "" when the plugin sent none
        assertEquals("m", barView(obj("""{"id":"b","label":"","message":"m"}""")).label)
        assertEquals("L", barView(obj("""{"id":"b","label":"L","message":""}""")).label)
    }

    @Test
    fun doneLineSaysExitCancelledOrTheError() {
        assertEquals("exit 0", doneText(DoneInfo(0, null)))
        assertEquals("cancelled", doneText(DoneInfo(130, null)))
        assertEquals("exit 2: bad", doneText(DoneInfo(2, "bad")))
    }
}
