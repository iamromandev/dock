package dev.dock.cross.desktop

import java.nio.file.Files
import java.nio.file.Path
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull

class SettingsStoreTest {
    private fun file(): Path = Files.createTempDirectory("dock-cross-test").resolve("sub/settings.properties")

    @Test
    fun missingFileGivesTheDefaults() {
        assertEquals(ConnectionSettings("127.0.0.1", 7464), SettingsStore(file()).load())
    }

    @Test
    fun savedHostAndPortComeBack() {
        val f = file()
        SettingsStore(f).save(ConnectionSettings("10.0.0.5", 9000))
        assertEquals(ConnectionSettings("10.0.0.5", 9000), SettingsStore(f).load())
        assertFalse(Files.readString(f).contains("token"))
    }

    @Test
    fun aBadPortInTheFileFallsBackToTheDefaultPort() {
        val f = file()
        Files.createDirectories(f.parent)
        Files.writeString(f, "host=example.org\nport=banana\n")
        assertEquals(ConnectionSettings("example.org", 7464), SettingsStore(f).load())
    }

    @Test
    fun portTextIsParsedWithinRange() {
        assertEquals(7464, parsePort(" 7464 "))
        assertNull(parsePort("0"))
        assertNull(parsePort("65536"))
        assertNull(parsePort("abc"))
        assertNull(parsePort(""))
    }

    @Test
    fun theSettingsFolderFollowsThePlatform() {
        assertEquals(Path.of("/h/Library/Application Support/dock-cross"), settingsDir(emptyMap(), "/h", "Mac OS X"))
        assertEquals(Path.of("/x/dock-cross"), settingsDir(mapOf("XDG_CONFIG_HOME" to "/x"), "/h", "Linux"))
        assertEquals(Path.of("/h/.config/dock-cross"), settingsDir(emptyMap(), "/h", "Linux"))
        assertEquals(Path.of("/a/dock-cross"), settingsDir(mapOf("APPDATA" to "/a"), "/h", "Windows 11"))
    }
}
