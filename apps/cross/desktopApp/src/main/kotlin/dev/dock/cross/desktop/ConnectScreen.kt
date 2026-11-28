package dev.dock.cross.desktop

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp

@Composable
fun ConnectScreen(
    initial: ConnectionSettings,
    connecting: Boolean,
    error: String?,
    loadToken: suspend (String, Int) -> String?,
    onConnect: (String, Int, String) -> Unit,
) {
    var host by remember { mutableStateOf(initial.host) }
    var port by remember { mutableStateOf(initial.port.toString()) }
    var token by remember { mutableStateOf("") }
    var tokenEdited by remember { mutableStateOf(false) }
    val parsedPort = parsePort(port)

    // Prefill the stored token for this host and port, after typing pauses, and never over one the user typed.
    LaunchedEffect(host, parsedPort) {
        prefillToken(host, parsedPort, TOKEN_DEBOUNCE_MS, { tokenEdited }, loadToken)?.let { token = it }
    }

    Column(
        Modifier.padding(24.dp).widthIn(max = 420.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
        horizontalAlignment = Alignment.Start,
    ) {
        Text("Connect to a dock server", style = MaterialTheme.typography.titleLarge)
        OutlinedTextField(host, { host = it }, label = { Text("Host") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(
            port,
            { port = it },
            label = { Text("Port") },
            singleLine = true,
            isError = parsedPort == null,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            token,
            {
                token = it
                tokenEdited = true
            },
            label = { Text("Token") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
        )
        if (error != null) Text(error, color = MaterialTheme.colorScheme.error)
        Button(
            onClick = { if (parsedPort != null) onConnect(host.trim(), parsedPort, token) },
            enabled = !connecting && parsedPort != null && host.isNotBlank(),
        ) { Text(if (connecting) "Connecting..." else "Connect") }
    }
}
