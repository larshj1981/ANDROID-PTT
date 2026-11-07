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
    /** reason = kort tekst til displayet (fx "HUB SVARER IKKE"), detail = forklaring. */
    data class Disconnected(val reason: String, val detail: String = "", val wasConnected: Boolean = false) : HubEvent()
    data class SetChannel(val index: Int) : HubEvent()
    data class Kicked(val message: String) : HubEvent()
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
        const val SET_CHANNEL = 16
        const val KICK = 17

        /** Kort, forståelig årsag til en forbindelsesfejl. */
        fun describe(e: Throwable): Pair<String, String> = when (e) {
            is java.net.UnknownHostException -> "FORKERT ADRESSE" to "adressen kunne ikke findes"
            is java.net.SocketTimeoutException -> "INGEN FORBINDELSE" to
                "intet svar – forkert adresse, computeren er slukket eller firewall blokerer"
            is java.net.ConnectException ->
                if ((e.message ?: "").contains("ECONNREFUSED") || (e.message ?: "").contains("refused", true))
                    "HUB SVARER IKKE" to "computeren svarer, men hubben kører ikke (eller forkert port)"
                else "NETVÆRK UTILGÆNGELIGT" to (e.message ?: "netværket kan ikke nå hubben")
            is java.net.NoRouteToHostException -> "NETVÆRK UTILGÆNGELIGT" to "ingen vej til hubben"
            is java.io.EOFException -> "FORBINDELSE TABT" to "hubben lukkede forbindelsen"
            else -> "FORBINDELSE TABT" to (e.message ?: e.javaClass.simpleName)
        }
    }

    @Volatile var connected = false
        private set
    @Volatile private var closed = false
    @Volatile private var socket: Socket? = null
    @Volatile private var out: DataOutputStream? = null
    private val writer = Executors.newSingleThreadScheduledExecutor()

    fun connect(host: String, port: Int, name: String, channel: Int, version: String = "") {
        Thread {
            var reason = "FORBINDELSE TABT"
            var detail = "forbindelsen blev lukket"
            var wasConnected = false
            try {
                val s = Socket()
                socket = s
                s.tcpNoDelay = true
                s.keepAlive = true
                s.connect(InetSocketAddress(host, port), 5000)
                s.soTimeout = 35_000
                out = DataOutputStream(BufferedOutputStream(s.getOutputStream(), 4096))
                connected = true
                wasConnected = true
                val hello = JSONObject().put("name", name).put("channel", channel)
                    .put("version", version).put("platform", "Android").toString()
                send(HELLO, hello.toByteArray(Charsets.UTF_8))
                writer.scheduleAtFixedRate({ send(PING) }, 10, 10, TimeUnit.SECONDS)
                onEvent(this, HubEvent.Connected)
                readLoop(DataInputStream(BufferedInputStream(s.getInputStream())))
            } catch (e: Exception) {
                val d = describe(e)
                reason = d.first
                detail = d.second
            } finally {
                val userClosed = closed
                shutdown()
                if (!userClosed) onEvent(this, HubEvent.Disconnected(reason, detail, wasConnected))
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
                SET_CHANNEL -> text().trim().toIntOrNull()?.let { HubEvent.SetChannel(it) }
                KICK -> HubEvent.Kicked(text().ifBlank { "Smidt af hubben" })
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
