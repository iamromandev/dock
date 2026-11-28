package dev.dock.cross

import java.io.File
import java.net.ServerSocket
import java.nio.file.Files
import java.util.UUID
import java.util.concurrent.TimeUnit

/** A real dock-server on a free port with a throwaway DOCK_HOME holding the probe and uiprobe fixtures. */
class LiveServer : AutoCloseable {
    private val repo = File(System.getProperty("dock.repo") ?: error("dock.repo is not set (run through the liveTest task)"))
    private val serverDir = File(repo, "apps/server")
    private val home: File = Files.createTempDirectory("dock-cross-live-").toFile()
    private val outDir: File = Files.createTempDirectory("dock-cross-live-out-").toFile()
    private val logFile: File = File(home.parentFile, "${home.name}.log")
    val token: String = UUID.randomUUID().toString()
    var port: Int = freePort()
        private set
    private var process: Process? = null

    /** Pid of the Python server (a descendant of the `uv run` process); null before start or when not found. */
    var pythonPid: Long? = null
        private set

    fun start(): LiveServer =
        try {
            install()
            // A free port can be taken between choosing it and the server binding it; try again on a new one.
            var started = false
            for (attempt in 1..3) {
                if (launch()) {
                    started = true
                    break
                }
                if (attempt < 3) port = freePort()
            }
            check(started) { "dock-server could not bind a port in 3 attempts:\n${log()}" }
            this
        } catch (e: Throwable) {
            close()
            throw e
        }

    private fun install() {
        for (fixture in listOf("probe", "uiprobe")) {
            val dir = File(serverDir, "tests/fixtures/$fixture").absolutePath
            val code = "import sys; from dock_host.store import Store; Store(sys.argv[1]).install(sys.argv[2])"
            val out = File(home.parentFile, "${home.name}.install.log")
            try {
                // Output goes to a file so waitFor can time out (reading the pipe would block until exit).
                val install =
                    uv("run", "--project", serverDir.path, "python", "-c", code, home.path, dir)
                        .redirectOutput(out)
                        .start()
                if (!install.waitFor(300, TimeUnit.SECONDS)) {
                    install.destroyForcibly().waitFor()
                    error("installing $fixture timed out:\n${out.readText()}")
                }
                check(install.exitValue() == 0) { "installing $fixture failed:\n${out.readText()}" }
            } finally {
                out.delete()
            }
        }
    }

    /** Starts the server on [port]; false when the port was already taken. */
    private fun launch(): Boolean {
        val builder = uv("run", "--project", serverDir.path, "python", "-m", "src", "--port", "$port", "--out", outDir.path)
        builder.directory(serverDir)
        builder.redirectOutput(logFile)
        builder.environment().putAll(mapOf("DOCK_HOME" to home.path, "DOCK_SERVER_TOKEN" to token, "DOCK_CANCEL_GRACE" to "1"))
        val p = builder.start()
        process = p
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60)
        // Ready means our own server logged that it listens on our port; a connect could reach a foreign listener.
        while (!log().contains("listening on ws://127.0.0.1:$port")) {
            if (!p.isAlive) {
                if (log().contains("Address already in use")) return false
                error("dock-server exited before listening:\n${log()}")
            }
            check(System.nanoTime() < deadline) { "dock-server did not start in 60s:\n${log()}" }
            Thread.sleep(100)
        }
        pythonPid = pythonProcess(p)?.pid()
        return true
    }

    private fun pythonProcess(p: Process): ProcessHandle? =
        p.descendants().toList().firstOrNull { h ->
            val info = h.info()
            info.arguments().map { a -> a.toList().contains("src") }.orElse(false) ||
                info.commandLine().map { it.contains("-m src") }.orElse(false)
        }

    private fun uv(vararg args: String) = ProcessBuilder("uv", *args).redirectErrorStream(true)

    fun exited(): Boolean = process?.isAlive == false

    fun log(): String = if (logFile.exists()) logFile.readText() else ""

    /**
     * Stops the server, gentlest first: SIGINT to the Python server (its KeyboardInterrupt path stops runs and
     * removes containers), up to 10s for `uv` to exit, then SIGTERM to every descendant and `uv`, and
     * `destroyForcibly` on all of them as the last resort.
     */
    override fun close() {
        process?.let { p ->
            pythonProcess(p)?.let { h ->
                runCatching { ProcessBuilder("kill", "-INT", "${h.pid()}").start().waitFor(5, TimeUnit.SECONDS) }
            }
            if (!p.waitFor(10, TimeUnit.SECONDS)) {
                p.descendants().forEach { it.destroy() }
                p.destroy()
                if (!p.waitFor(20, TimeUnit.SECONDS)) {
                    p.descendants().forEach { it.destroyForcibly() }
                    p.destroyForcibly().waitFor()
                }
            }
        }
        home.deleteRecursively()
        outDir.deleteRecursively()
        logFile.delete()
    }

    private fun freePort(): Int = ServerSocket(0).use { it.localPort }
}
