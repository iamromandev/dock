package dev.dock.cross.desktop

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import dev.dock.cross.AskAnswer
import dev.dock.cross.AskInfo
import dev.dock.cross.AskState
import dev.dock.cross.DockClient
import dev.dock.cross.FormField
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/** Closing the dialog, or an ask this client cannot show, answers end-of-input. */
@Composable
fun AskDialog(
    ask: AskState,
    client: DockClient,
    scope: CoroutineScope,
) {
    val once = remember { AnswerOnce() }

    fun send(result: JsonObject) {
        if (once.first(ask.ask)) scope.launch { client.answer(ask.ask, result) }
    }
    when (val info = ask.info) {
        is AskInfo.Prompt -> {
            var text by remember(ask.ask) { mutableStateOf("") }
            AlertDialog(
                onDismissRequest = { send(AskAnswer.eof()) },
                title = { Text(info.text) },
                text = { OutlinedTextField(text, { text = it }, singleLine = true, modifier = Modifier.fillMaxWidth()) },
                confirmButton = { TextButton(onClick = { send(AskAnswer.prompt(text)) }) { Text("OK") } },
            )
        }

        is AskInfo.Form -> {
            var values by remember(ask.ask) { mutableStateOf(formDefaults(info.fields)) }
            AlertDialog(
                onDismissRequest = { send(AskAnswer.eof()) },
                title = { Text(info.title ?: "Input") },
                text = {
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        for (f in info.fields) FieldView(f, values) { name, v -> values = values + (name to v) }
                    }
                },
                confirmButton = {
                    if (!info.hasUnknown) TextButton(onClick = { send(AskAnswer.form(values)) }) { Text("OK") }
                },
                dismissButton = { if (info.hasUnknown) TextButton(onClick = { send(AskAnswer.eof()) }) { Text("Close") } },
            )
        }

        is AskInfo.Raw -> {
            AlertDialog(
                onDismissRequest = { send(AskAnswer.eof()) },
                title = { Text("Unsupported request: ${info.method}") },
                confirmButton = { TextButton(onClick = { send(AskAnswer.eof()) }) { Text("Close") } },
            )
        }
    }
}

@Composable
private fun FieldView(
    f: FormField,
    values: Map<String, JsonElement>,
    set: (String, JsonElement) -> Unit,
) {
    val current = (values[f.name] as? JsonPrimitive)?.content.orEmpty()
    when (f) {
        is FormField.Text -> {
            val hide: VisualTransformation = if (f.secret) PasswordVisualTransformation() else VisualTransformation.None
            OutlinedTextField(
                current,
                { set(f.name, JsonPrimitive(it)) },
                label = { Text(f.label) },
                singleLine = true,
                visualTransformation = hide,
                modifier = Modifier.fillMaxWidth(),
            )
        }

        is FormField.Confirm -> {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(current == "true", { set(f.name, JsonPrimitive(it)) })
                Text(f.label)
            }
        }

        is FormField.Choice -> {
            Column {
                Text(f.label)
                for (c in f.choices) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        RadioButton(current == c, { set(f.name, JsonPrimitive(c)) })
                        Text(c)
                    }
                }
            }
        }

        is FormField.Unknown -> {
            Text("${f.name}: unsupported field type")
        }
    }
}
