package dk.pttradio

import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.TimeUnit

data class ChannelState(val users: Int, val talker: String?)

sealed class HubEvent {
    object Connected : HubEvent()
    data class Disconnected(val reason: String) : HubEvent()
    data class Channels(val names: List<String>) : HubEvent()
    data class Status(val channels: List<ChannelState>) : HubEvent()
    object Granted : HubEvent()
    data class Denied(val talker: String) : HubEvent()
    data class RxStart(val name: String) : HubEvent()
    class Audio(val data: ByteArray) : HubEvent()
    object RxEnd : HubEvent()
}

/** TCP-forbindelse til hubben. Events leveres på læsetråden. */
class HubClient(private val onEvent: (HubClient, HubEvent) -> Unit) {

    companion object {
        const val HELLO = 1
        const val JOIN = 2
        const val PTT_START = 3
        const val AUDIO = 4
        const val PTT_END = 5
        const val PING = 6
        const val CHANNELS = 10
        const val GRANTED = 11
        const val DENIED = 12
        const val RX_START = 13
        const val RX_END = 14
        const val STATUS = 15
    }

    @Volatile var connected = false
        private set
    @Volatile private var closed = false
    @Volatile private var socket: Socket? = null
    @Volatile private var out: DataOutputStream? = null
    private val writer = Executors.newSingleThreadScheduledExecutor()

    fun connect(host: String, port: Int, name: String, channel: Int) {
        Thread {
            var reason = "Forbindelsen blev lukket"
            try {
                val s = Socket()
                socket = s
                s.tcpNoDelay = true
                s.keepAlive = true
                s.connect(InetSocketAddress(host, port), 5000)
                s.soTimeout = 35_000
                out = DataOutputStream(BufferedOutputStream(s.getOutputStream(), 4096))
                connected = true
                val hello = JSONObject().put("name", name).put("channel", channel).toString()
                send(HELLO, hello.toByteArray(Charsets.UTF_8))
                writer.scheduleAtFixedRate({ send(PING) }, 10, 10, TimeUnit.SECONDS)
                onEvent(this, HubEvent.Connected)
                readLoop(DataInputStream(BufferedInputStream(s.getInputStream())))
            } catch (e: Exception) {
                reason = e.message ?: e.javaClass.simpleName
            } finally {
                val userClosed = closed
                shutdown()
                if (!userClosed) onEvent(this, HubEvent.Disconnected(reason))
            }
        }.start()
    }

    private fun readLoop(inp: DataInputStream) {
        while (!closed) {
            val type = inp.readUnsignedByte()
            val len = inp.readInt()
            if (len < 0 || len > 1_000_000) throw IOException("Ugyldig pakke")
            val p = ByteArray(len)
            inp.readFully(p)
            val text = { String(p, Charsets.UTF_8) }
            val ev: HubEvent? = when (type) {
                AUDIO -> HubEvent.Audio(p)
                CHANNELS -> {
                    val a = JSONArray(text())
                    HubEvent.Channels(List(a.length()) { a.getString(it) })
                }
                STATUS -> {
                    val a = JSONArray(text())
                    HubEvent.Status(List(a.length()) {
                        val o = a.getJSONObject(it)
                        ChannelState(o.optInt("users"), if (o.isNull("talker")) null else o.getString("talker"))
                    })
                }
                GRANTED -> HubEvent.Granted
                DENIED -> HubEvent.Denied(text())
                RX_START -> HubEvent.RxStart(text())
                RX_END -> HubEvent.RxEnd
                else -> null
            }
            if (ev != null) onEvent(this, ev)
        }
    }

    fun send(type: Int, payload: ByteArray = ByteArray(0)) {
        if (closed) return
        try {
            writer.execute {
                val o = out ?: return@execute
                try {
                    o.writeByte(type)
                    o.writeInt(payload.size)
                    o.write(payload)
                    o.flush()
                } catch (e: IOException) {
                    try { socket?.close() } catch (_: Exception) {}
                }
            }
        } catch (_: RejectedExecutionException) {
        }
    }

    fun close() {
        closed = true
        shutdown()
    }

    private fun shutdown() {
        connected = false
        try { socket?.close() } catch (_: Exception) {}
        writer.shutdownNow()
    }
}
