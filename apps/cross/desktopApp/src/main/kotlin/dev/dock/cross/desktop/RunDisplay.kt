package dev.dock.cross.desktop

import dev.dock.cross.DoneInfo
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull

data class TableView(
    val title: String?,
    val columns: List<String>,
    val rows: List<List<String>>,
)

/** [fraction] is 0..1, or null when the progress is unknown. */
data class BarView(
    val label: String,
    val fraction: Float?,
    val done: Boolean,
)

private const val CANCELLED = 130

private fun JsonObject.text(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content

private fun cell(e: kotlinx.serialization.json.JsonElement): String = (e as? JsonPrimitive)?.content ?: e.toString()

fun tableView(params: JsonObject): TableView {
    val columns = (params["columns"] as? JsonArray).orEmpty().map(::cell)
    val rows =
        (params["rows"] as? JsonArray).orEmpty().map { row ->
            val cells = (row as? JsonArray).orEmpty().map(::cell)
            cells + List((columns.size - cells.size).coerceAtLeast(0)) { "" }
        }
    return TableView(params.text("title"), columns, rows)
}

fun barView(params: JsonObject): BarView {
    val percent = (params["percent"] as? JsonPrimitive)?.doubleOrNull
    return BarView(
        label = listOfNotNull(params.text("label"), params.text("message")).filter { it.isNotEmpty() }.joinToString(" - "),
        fraction = percent?.let { (it / 100.0).coerceIn(0.0, 1.0).toFloat() },
        done = (params["done"] as? JsonPrimitive)?.booleanOrNull ?: false,
    )
}

fun doneText(done: DoneInfo): String =
    when {
        done.code == CANCELLED -> "cancelled"
        done.error != null -> "exit ${done.code}: ${done.error}"
        else -> "exit ${done.code}"
    }
