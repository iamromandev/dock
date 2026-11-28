package dev.dock.cross.desktop

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import dev.dock.cross.DockClient
import dev.dock.cross.RunItem
import dev.dock.cross.RunState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch

@Composable
fun RunView(
    run: RunState,
    client: DockClient,
    scope: CoroutineScope,
    modifier: Modifier = Modifier,
) {
    Column(modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("${run.plugin} ${run.command}", style = MaterialTheme.typography.titleSmall)
        LazyColumn(Modifier.weight(1f, fill = false), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            items(run.items) { RunItemView(it) }
        }
        val done = run.done
        if (done == null) {
            Button(onClick = { scope.launch { client.cancel() } }) { Text("Cancel") }
        } else {
            Text(doneText(done), color = if (done.code == 0) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.error)
        }
    }
    run.ask?.let { AskDialog(it, client, scope) }
}

@Composable
private fun RunItemView(item: RunItem) {
    when (item) {
        is RunItem.Text -> {
            Text(item.text, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
        }

        is RunItem.Warn -> {
            Text("warning: ${item.message}", color = MaterialTheme.colorScheme.tertiary)
        }

        is RunItem.Table -> {
            val t = tableView(item.params)
            Column {
                t.title?.let { Text(it, style = MaterialTheme.typography.titleSmall) }
                Row { for (c in t.columns) Text(c, Modifier.weight(1f), style = MaterialTheme.typography.labelLarge) }
                for (row in t.rows) Row { for (c in row) Text(c, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall) }
            }
        }

        is RunItem.Panel -> {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(8.dp)) {
                    if (item.title.isNotEmpty()) Text(item.title, style = MaterialTheme.typography.titleSmall)
                    for ((k, v) in item.items) {
                        Row {
                            Text(k, Modifier.weight(1f), style = MaterialTheme.typography.labelLarge)
                            Text(v, Modifier.weight(2f), style = MaterialTheme.typography.bodySmall)
                        }
                    }
                }
            }
        }

        is RunItem.Bar -> {
            val b = barView(item.params)
            Column {
                if (b.label.isNotEmpty()) Text(b.label, style = MaterialTheme.typography.bodySmall)
                val f = b.fraction
                if (f != null) {
                    LinearProgressIndicator(progress = { f }, modifier = Modifier.fillMaxWidth())
                } else if (b.done) {
                    Text("done", style = MaterialTheme.typography.bodySmall)
                } else {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                }
            }
        }
    }
}
