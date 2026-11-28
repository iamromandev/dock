package dev.dock.cross.desktop

import dev.dock.cross.ClientState
import dev.dock.cross.ConnState
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class GuardsTest {
    @Test
    fun credentialsToStringNeverShowsTheToken() {
        // bug: a data class prints the token in logs and exceptions
        assertFalse("s3cret" in Credentials("h", 1, "s3cret").toString())
    }

    @Test
    fun anAskIsAnsweredOnceEvenIfOkAndDismissBothFire() {
        // bug: OK and dismiss in one frame send two answers for one ask
        val once = AnswerOnce()
        assertTrue(once.first(7))
        assertFalse(once.first(7))
        assertTrue(once.first(8))
    }

    @Test
    fun aSecondConnectIsIgnoredWhileOneIsRunning() =
        runBlocking {
            // bug: two connects race, and the loser's credentials end up remembered
            val flight = SingleFlight()
            val gate = CompletableDeferred<Unit>()
            val scope = CoroutineScope(Dispatchers.Default)
            var second = false
            assertTrue(flight.launch(scope) { gate.await() })
            assertFalse(flight.launch(scope) { second = true })
            gate.complete(Unit)
            flight.join()
            assertTrue(flight.launch(scope) { second = true })
            flight.join()
            assertTrue(second)
        }

    @Test
    fun errorsOfTheFormsOwnRunStayWithTheFormAndTheRestShowAtTheTop() {
        val errors = mapOf("req-2" to "busy", ClientState.GENERAL_ERROR to "general", "req-1" to "list failed")
        val (own, other) = splitErrors(errors, "req-2")
        assertEquals(mapOf("req-2" to "busy"), own)
        assertEquals(mapOf(ClientState.GENERAL_ERROR to "general", "req-1" to "list failed"), other)
        assertEquals(errors, splitErrors(errors, null).second)
    }

    @Test
    fun reconnectIsOfferedWheneverThereAreCredentialsAndTheConnectionIsDown() {
        val creds = Credentials("h", 1, "t")
        val down = ConnState.Disconnected("lost")
        assertEquals(Screen.Reconnect, screenFor(down, creds))
        assertEquals(Screen.Connect, screenFor(down, null))
        assertEquals(Screen.Connect, screenFor(ConnState.Connecting, creds))
        assertEquals(Screen.Plugins, screenFor(ConnState.Connected("s"), creds))
    }

    @Test
    fun formStartsFromTheLastConnectionElseTheSavedSettings() {
        val saved = ConnectionSettings("saved", 1)
        assertEquals(ConnectionSettings("h", 2), initialSettings(Credentials("h", 2, "t"), saved))
        assertEquals(saved, initialSettings(null, saved))
    }

    @Test
    fun theKeychainIsReadOnlyForAValidHostAndPortAndAnEditedTokenIsNeverOverwritten() =
        runBlocking {
            // bug: every host keystroke reads the keychain, or a slow read overwrites what the user typed meanwhile
            var reads = 0
            var edited = false
            val load: suspend (String, Int) -> String? = { _, _ ->
                reads++
                "stored"
            }
            assertNull(prefillToken("h", null, 0, { edited }, load))
            assertNull(prefillToken("  ", 1, 0, { edited }, load))
            assertEquals(0, reads)
            assertEquals("stored", prefillToken(" h ", 1, 0, { edited }, load))
            assertEquals("", prefillToken("h", 1, 0, { edited }) { _, _ -> null })
            assertNull(prefillToken("h", 1, 0, { edited }) { _, _ -> "x".also { edited = true } })
            edited = true
            assertNull(prefillToken("h", 1, 0, { edited }, load))
            assertEquals(1, reads)
        }
}
