package dev.dock.cross.desktop

import com.github.javakeyring.Keyring

const val KEYCHAIN_SERVICE = "dock-cross"

fun tokenAccount(
    host: String,
    port: Int,
) = "$host:$port"

/** Passwords in the OS keychain. Calls block and never throw: a failure reads as no secret. */
interface SecretStore {
    fun get(account: String): String?

    fun set(
        account: String,
        secret: String,
    )

    fun delete(account: String)
}

/** The blocking keychain calls, so [KeyringSecretStore] can be tested without one. */
interface Vault : AutoCloseable {
    fun get(
        service: String,
        account: String,
    ): String?

    fun set(
        service: String,
        account: String,
        secret: String,
    )

    fun delete(
        service: String,
        account: String,
    )
}

private class JavaKeyringVault(
    private val keyring: Keyring,
) : Vault {
    override fun get(
        service: String,
        account: String,
    ): String? = keyring.getPassword(service, account)

    override fun set(
        service: String,
        account: String,
        secret: String,
    ) = keyring.setPassword(service, account, secret)

    override fun delete(
        service: String,
        account: String,
    ) = keyring.deletePassword(service, account)

    override fun close() = keyring.close()
}

/**
 * The OS keychain under service [KEYCHAIN_SERVICE]. Any failure (no backend, locked keychain) means
 * "no stored token" and is logged by exception class only, since messages can quote what was being stored.
 */
class KeyringSecretStore(
    private val open: () -> Vault = { JavaKeyringVault(Keyring.create()) },
) : SecretStore {
    private var vault: Vault? = null

    @Synchronized
    private fun vault(): Vault = vault ?: open().also { vault = it }

    private inline fun <T> guarded(
        what: String,
        failed: T,
        call: (Vault) -> T,
    ): T =
        try {
            call(vault())
        } catch (e: Exception) {
            System.err.println("dock-cross: keychain $what failed: ${e.javaClass.simpleName}")
            failed
        } catch (e: LinkageError) {
            System.err.println("dock-cross: keychain $what failed: ${e.javaClass.simpleName}")
            failed
        }

    override fun get(account: String): String? = guarded("read", null) { it.get(KEYCHAIN_SERVICE, account) }

    override fun set(
        account: String,
        secret: String,
    ) = guarded("write", Unit) { it.set(KEYCHAIN_SERVICE, account, secret) }

    /** A missing entry is already deleted: it is not a failure, and nothing is logged. */
    override fun delete(account: String) =
        guarded("delete", Unit) {
            if (it.get(KEYCHAIN_SERVICE, account) != null) it.delete(KEYCHAIN_SERVICE, account)
        }
}

/** After a successful connect: keeps [token] for next time, or forgets the stored one when it is empty. */
fun rememberToken(
    store: SecretStore,
    host: String,
    port: Int,
    token: String,
) {
    val account = tokenAccount(host, port)
    if (token.isEmpty()) store.delete(account) else store.set(account, token)
}
