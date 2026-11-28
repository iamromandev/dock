package dev.dock.cross

import java.net.http.WebSocket
import kotlin.test.Test
import kotlin.test.assertEquals

class JdkTransportTest {
    // Bug caught: replying to a close with 1005/1006 (never valid on the wire) makes sendClose throw and the socket hang.
    @Test
    fun closeReplyOnlyEchoesCodesThatMayBeSent() {
        for (code in listOf(1000, 1001, 1008, 1011, 3000, 4999)) assertEquals(code, closeReplyCode(code))
        for (code in listOf(1005, 1006, 1015, 1004, 0, 999, 5000, -1)) assertEquals(WebSocket.NORMAL_CLOSURE, closeReplyCode(code))
    }
}
