package dev.dock.cross.desktop

import java.io.ByteArrayOutputStream
import java.io.PrintStream
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull

/** Only fakes here: nothing in this file opens the real keychain. */
class SecretStoreTest {
    private class FakeVault(
        var failWith: Throwable? = null,
    ) : Vault {
        val entries = mutableMapOf<String, String>()

        override fun get(
            service: String,
            account: String,
        ): String? {
            failWith?.let { throw it }
            return entries["$service/$account"]
        }

        override fun set(
            service: String,
            account: String,
            secret: String,
        ) {
            failWith?.let { throw it }
            entries["$service/$account"] = secret
        }

        override fun delete(
            service: String,
            account: String,
        ) {
            failWith?.let { throw it }
            check(entries.remove("$service/$account") != null) { "no entry" } // like a keychain: a missing entry fails
        }

        override fun close() = Unit
    }

    @Test
    fun aTokenIsStoredUnderTheDockCrossServiceAndHostPortAccount() {
        val vault = FakeVault()
        val store = KeyringSecretStore { vault }
        store.set(tokenAccount("h", 1), "tok")
        assertEquals(mapOf("dock-cross/h:1" to "tok"), vault.entries)
        assertEquals("tok", store.get("h:1"))
        store.delete("h:1")
        assertNull(store.get("h:1"))
    }

    private fun captureErr(block: () -> Unit): String {
        val old = System.err
        val buf = ByteArrayOutputStream()
        System.setErr(PrintStream(buf, true))
        try {
            block()
        } finally {
            System.setErr(old)
        }
        return buf.toString()
    }

    @Test
    fun anyKeyringFailureIsNoTokenAndNeverThrowsAndNeverLogsTheToken() {
        val log =
            captureErr {
                val store = KeyringSecretStore { FakeVault(IllegalStateException("boom secret-tok")) }
                assertNull(store.get("h:1"))
                store.set("h:1", "secret-tok")
                store.delete("h:1")
                val unavailable = KeyringSecretStore { throw IllegalStateException("no keychain") }
                assertNull(unavailable.get("h:1"))
            }
        assertFalse("secret-tok" in log)
    }

    @Test
    fun deletingAMissingEntryIsASuccessAndLogsNothing() {
        // bug: an empty-token connect with nothing stored logs "keychain delete failed" every time
        assertEquals("", captureErr { KeyringSecretStore { FakeVault() }.delete("h:1") })
    }

    @Test
    fun anEmptyTokenOnConnectDeletesTheStoredOne() {
        val store = KeyringSecretStore { FakeVault().also { it.entries["dock-cross/h:1"] = "old" } }
        rememberToken(store, "h", 1, "")
        assertNull(store.get("h:1"))
        rememberToken(store, "h", 1, "new")
        assertEquals("new", store.get("h:1"))
    }
}
