package dev.dock.cross.desktop

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.application
import dev.dock.cross.ConnState
import dev.dock.cross.DockClient
import dev.dock.cross.JdkTransport
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

fun main() {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    val client = DockClient(JdkTransport(), scope)
    val settings =
        SettingsStore(
            settingsDir(
                System.getenv(),
                System.getProperty("user.home").orEmpty(),
                System.getProperty("os.name").orEmpty(),
            ).resolve("settings.properties"),
        )
    val secrets = KeyringSecretStore()
    application {
        Window(onCloseRequest = {
            client.disconnect()
            exitApplication()
        }, title = "dock") {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) { App(client, scope, settings, secrets) }
            }
        }
    }
}

@Composable
private fun App(
    client: DockClient,
    scope: CoroutineScope,
    settings: SettingsStore,
    secrets: SecretStore,
) {
    val state by client.state.collectAsState()
    var last by remember { mutableStateOf<Credentials?>(null) }
    val saved = remember { settings.load() }
    val flight = remember { SingleFlight() }

    suspend fun connectAndLoad(c: Credentials) {
        client.connect(c.host, c.port, c.token)
        if (client.state.value.conn is ConnState.Connected) {
            withContext(Dispatchers.IO) {
                settings.save(ConnectionSettings(c.host, c.port))
                rememberToken(secrets, c.host, c.port, c.token)
            }
            client.list()
        }
    }

    fun connect(c: Credentials) {
        // one connect at a time: a second click or Reconnect while one is under way is ignored
        if (flight.launch(scope) { connectAndLoad(c) }) last = c
    }
    val conn = state.conn
    val run = state.run
    val credentials = last
    when (screenFor(conn, credentials)) {
        Screen.Plugins -> {
            PluginsScreen(client, state, scope)
        }

        Screen.Reconnect -> {
            Column(Modifier.fillMaxSize()) {
                Row(
                    Modifier.fillMaxWidth().padding(12.dp),
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    val reason = (conn as? ConnState.Disconnected)?.reason
                    Text("Disconnected${reason?.let { ": $it" }.orEmpty()}", color = MaterialTheme.colorScheme.error)
                    Button(onClick = { credentials?.let(::connect) }) { Text("Reconnect") }
                }
                if (run != null) RunView(run, client, scope)
            }
        }

        Screen.Connect -> {
            ConnectScreen(
                initial = initialSettings(credentials, saved),
                connecting = conn is ConnState.Connecting,
                error = (conn as? ConnState.Disconnected)?.reason,
                loadToken = { host, port -> withContext(Dispatchers.IO) { secrets.get(tokenAccount(host, port)) } },
                onConnect = { host, port, token -> connect(Credentials(host, port, token)) },
            )
        }
    }
}
