package dev.dock.cross.desktop

import java.io.IOException
import java.nio.file.Files
import java.nio.file.Path
import java.util.Properties

const val DEFAULT_HOST = "127.0.0.1"
const val DEFAULT_PORT = 7464
private const val APP_DIR_NAME = "dock-cross"

/** What is remembered between launches. The token is never part of it. */
data class ConnectionSettings(
    val host: String = DEFAULT_HOST,
    val port: Int = DEFAULT_PORT,
)

/** [text] as a TCP port, or null when it is not a number from 1 to 65535. */
fun parsePort(text: String): Int? = text.trim().toIntOrNull()?.takeIf { it in 1..65535 }

/** The folder of the settings file. Pure: the caller passes the environment, so tests never see the real one. */
fun settingsDir(
    env: Map<String, String>,
    home: String,
    os: String,
): Path =
    when {
        os.startsWith("Mac", ignoreCase = true) -> {
            Path.of(home, "Library", "Application Support", APP_DIR_NAME)
        }

        os.startsWith("Windows", ignoreCase = true) -> {
            (env["APPDATA"]?.takeIf { it.isNotBlank() }?.let { Path.of(it) } ?: Path.of(home, "AppData", "Roaming")).resolve(APP_DIR_NAME)
        }

        else -> {
            val xdg = env["XDG_CONFIG_HOME"]?.takeIf { it.isNotBlank() && Path.of(it).isAbsolute }
            (xdg?.let { Path.of(it) } ?: Path.of(home, ".config")).resolve(APP_DIR_NAME)
        }
    }

/** Host and port in a properties file. A missing or unreadable file, or a bad value in it, gives the default. */
class SettingsStore(
    private val file: Path,
) {
    fun load(): ConnectionSettings {
        val props = Properties()
        try {
            Files.newBufferedReader(file).use { props.load(it) }
        } catch (_: IOException) {
            return ConnectionSettings()
        } catch (_: IllegalArgumentException) {
            return ConnectionSettings()
        }
        return ConnectionSettings(
            host = props.getProperty("host")?.trim()?.takeIf { it.isNotEmpty() } ?: DEFAULT_HOST,
            port = props.getProperty("port")?.let { parsePort(it) } ?: DEFAULT_PORT,
        )
    }

    /** Best effort: a settings file that cannot be written must not stop the app. */
    fun save(settings: ConnectionSettings) {
        val props = Properties()
        props.setProperty("host", settings.host)
        props.setProperty("port", settings.port.toString())
        try {
            Files.createDirectories(file.parent)
            Files.newBufferedWriter(file).use { props.store(it, null) }
        } catch (e: IOException) {
            System.err.println("dock-cross: could not save settings: ${e.javaClass.simpleName}")
        }
    }
}
