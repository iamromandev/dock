package dev.dock.cross.desktop

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import dev.dock.cross.ClientState
import dev.dock.cross.CommandInfo
import dev.dock.cross.DockClient
import dev.dock.cross.FieldKind
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch

private data class Selected(
    val plugin: String,
    val command: CommandInfo,
)

@Composable
fun PluginsScreen(
    client: DockClient,
    state: ClientState,
    scope: CoroutineScope,
) {
    var selected by remember { mutableStateOf<Selected?>(null) }
    var ownRun by remember { mutableStateOf<String?>(null) }
    Row(Modifier.fillMaxSize()) {
        LazyColumn(Modifier.width(280.dp).fillMaxHeight().padding(8.dp)) {
            items(state.plugins) { plugin ->
                Text(plugin.id, style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 8.dp))
                for (command in plugin.commands) {
                    Column(Modifier.fillMaxWidth().clickable { selected = Selected(plugin.id, command) }.padding(vertical = 4.dp)) {
                        Text(command.name)
                        if (command.summary.isNotEmpty()) Text(command.summary, style = MaterialTheme.typography.bodySmall)
                    }
                }
                HorizontalDivider()
            }
        }
        Column(Modifier.weight(1f).fillMaxHeight().padding(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            val sel = selected
            ErrorList(splitErrors(state.errors, ownRun).second, client)
            if (sel == null) {
                Text("Pick a command.")
            } else {
                CommandForm(sel, client, state, scope, ownRun) { ownRun = it }
            }
            state.run?.let { RunView(it, client, scope, Modifier.weight(1f)) }
        }
    }
}

@Composable
private fun CommandForm(
    sel: Selected,
    client: DockClient,
    state: ClientState,
    scope: CoroutineScope,
    ownRun: String?,
    onRun: (String?) -> Unit,
) {
    var form by remember(sel) { mutableStateOf(FormState.initial(sel.command)) }
    var localError by remember(sel) { mutableStateOf<String?>(null) }
    val active = state.run?.let { it.done == null } ?: false

    Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("${sel.plugin} ${sel.command.name}", style = MaterialTheme.typography.titleMedium)
        val fields = form.fields
        if (fields == null) {
            OutlinedTextField(
                form.raw,
                { form = form.withRaw(it) },
                label = { Text("Inputs (JSON object, optional)") },
                minLines = 3,
                modifier = Modifier.fillMaxWidth(),
            )
        } else {
            for (f in fields) {
                val label = if (f.required) "${f.name} *" else f.name
                val value = form.values[f.name].orEmpty()
                when (f.kind) {
                    FieldKind.Checkbox -> {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(value == "true", { form = form.with(f.name, it.toString()) })
                            Text(label)
                        }
                    }

                    FieldKind.Text, FieldKind.Number -> {
                        OutlinedTextField(
                            value,
                            { form = form.with(f.name, it) },
                            label = { Text(label) },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                }
            }
        }
        localError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        ErrorList(splitErrors(state.errors, ownRun).first, client)
        Button(
            enabled = !active,
            onClick = {
                form.build(sel.command).fold(
                    {
                        localError = null
                        scope.launch { onRun(client.run(sel.plugin, sel.command.name, it)) }
                    },
                    { localError = it.message },
                )
            },
        ) { Text("Run") }
    }
}

@Composable
private fun ErrorList(
    errors: Map<String, String>,
    client: DockClient,
) {
    for ((id, message) in errors) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(message, color = MaterialTheme.colorScheme.error, modifier = Modifier.weight(1f))
            TextButton(onClick = { client.clearError(id) }) { Text("Dismiss") }
        }
    }
}
