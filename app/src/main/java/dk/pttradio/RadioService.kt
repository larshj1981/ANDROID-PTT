package dk.pttradio

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioManager
import android.media.ToneGenerator
import android.net.wifi.WifiManager
import android.os.Binder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator

/**
 * Holder forbindelsen til hubben og lyden kørende, også når appen er i baggrunden
 * eller skærmen er slukket. Aktiviteten er kun en "fjernbetjening".
 */
class RadioService : Service() {

    companion object {
        const val DEFAULT_PORT = 50000
        const val ACTION_STOP = "dk.pttradio.STOP"
        private const val NOTIF_CHANNEL = "radio"
        private const val NOTIF_ID = 1
    }

    inner class LocalBinder : Binder() {
        val service: RadioService get() = this@RadioService
    }

    private val binder = LocalBinder()
    override fun onBind(intent: Intent): IBinder = binder

    /** UI-tilbagekald – kaldes altid på hovedtråden. */
    var onChange: (() -> Unit)? = null

    private val ui = Handler(Looper.getMainLooper())
    private val audio = AudioEngine()
    private var tones: ToneGenerator? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    @Volatile private var client: HubClient? = null
    @Volatile var transmitting = false
        private set
    var wantConnected = false
        private set
    var statusMsg = "Ikke forbundet"
        private set
    var channelNames: List<String> = (1..8).map { "Kanal $it" }
        private set
    var channelStatus: List<ChannelState> = emptyList()
        private set
    var channel = 0
        private set
    var pttHeld = false
        private set
    var receivingFrom: String? = null
        private set
    var flashMsg: String? = null
        private set
    @Volatile var micLevel = 0f
        private set
    val fx = VoiceFx("digital")
    val online: Boolean get() = client?.connected == true

    private var host = ""
    private var port = DEFAULT_PORT
    private var name = ""

    private val reconnect = Runnable { if (wantConnected) startClient() }
    private val clearFlash = Runnable { flashMsg = null; changed() }

    // ---------------------------------------------------------------- livscyklus

    override fun onCreate() {
        super.onCreate()
        val prefs = getSharedPreferences("ptt", MODE_PRIVATE)
        channel = prefs.getInt("channel", 0)
        fx.setMode(prefs.getString("mode", "digital") ?: "digital")
        tones = try { ToneGenerator(AudioManager.STREAM_MUSIC, 70) } catch (_: Exception) { null }
        audio.startPlayback()
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(NOTIF_CHANNEL, "Radio", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Vises mens PTT Radio er forbundet"
                setShowBadge(false)
            }
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) disconnect()
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        wantConnected = false
        ui.removeCallbacksAndMessages(null)
        client?.close()
        client = null
        audio.stopRecording()
        audio.stopPlayback()
        tones?.release()
        releaseLocks()
        super.onDestroy()
    }

    // ---------------------------------------------------------------- forbindelse

    fun connect(hostPort: String, userName: String) {
        val raw = hostPort.trim()
        val idx = raw.lastIndexOf(':')
        host = if (idx > 0) raw.substring(0, idx) else raw
        port = if (idx > 0) raw.substring(idx + 1).toIntOrNull() ?: DEFAULT_PORT else DEFAULT_PORT
        name = userName.trim().ifEmpty { Build.MODEL }
        wantConnected = true
        statusMsg = "Forbinder…"

        // Gør tjenesten "startet" og forgrund, så den overlever at appen lukkes
        startForegroundService(Intent(this, RadioService::class.java))
        goForeground()
        acquireLocks()
        startClient()
        changed()
    }

    fun disconnect() {
        wantConnected = false
        ui.removeCallbacks(reconnect)
        stopTx()
        pttHeld = false
        receivingFrom = null
        channelStatus = emptyList()
        client?.close()
        client = null
        statusMsg = "Ikke forbundet"
        releaseLocks()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        changed()
    }

    private fun startClient() {
        client?.close()
        val c = HubClient(::onHubEvent)
        client = c
        c.connect(host, port, name, channel)
    }

    /** Netværkstråd: lyd (og start/slut-lyde) behandles her, så rækkefølgen bevares. */
    private fun onHubEvent(c: HubClient, e: HubEvent) {
        if (c !== client) return
        when (e) {
            is HubEvent.Audio -> {
                if (!transmitting) {
                    val out = fx.process(e.data)
                    if (out.isNotEmpty()) audio.play(out)
                }
                return
            }
            is HubEvent.RxStart -> if (!transmitting) audio.play(fx.startSound())
            is HubEvent.RxEnd -> if (!transmitting) audio.play(fx.endSound())
            else -> Unit
        }
        ui.post { if (c === client) handle(e) }
    }

    fun setMode(m: String) {
        fx.setMode(m)
        getSharedPreferences("ptt", MODE_PRIVATE).edit().putString("mode", fx.mode).apply()
        changed()
    }

    private fun handle(e: HubEvent) {
        when (e) {
            is HubEvent.Connected -> statusMsg = "Forbundet til $host"
            is HubEvent.Disconnected -> {
                stopTx()
                receivingFrom = null
                channelStatus = emptyList()
                client = null
                if (wantConnected) {
                    statusMsg = "Afbrudt – prøver igen…"
                    ui.removeCallbacks(reconnect)
                    ui.postDelayed(reconnect, 3000)
                } else {
                    statusMsg = "Ikke forbundet"
                }
            }
            is HubEvent.Channels -> {
                channelNames = e.names.ifEmpty { listOf("Kanal 1") }
                if (channel >= channelNames.size) {
                    channel = 0
                    client?.send(HubClient.JOIN, "0".toByteArray())
                    saveChannel()
                }
            }
            is HubEvent.Status -> channelStatus = e.channels
            is HubEvent.Granted -> if (pttHeld && !transmitting) startTx()
            is HubEvent.Denied -> if (pttHeld) {
                tone(ToneGenerator.TONE_SUP_BUSY, 400)
                vibrate(longArrayOf(0, 60, 80, 60))
                flash("Optaget – ${e.talker} taler")
            }
            is HubEvent.RxStart -> receivingFrom = e.name
            is HubEvent.RxEnd -> {
                if (transmitting) {
                    stopTx()
                    flash("Maks. sendetid nået")
                } else {
                    receivingFrom = null
                }
            }
            is HubEvent.Audio -> Unit
        }
        changed()
    }

    // ---------------------------------------------------------------- PTT

    /** @return false hvis mikrofon-tilladelse mangler. */
    fun pttDown(): Boolean {
        if (pttHeld) return true
        val c = client
        if (c == null || !c.connected) {
            tone(ToneGenerator.TONE_PROP_NACK, 200)
            flash("Ikke forbundet")
            return true
        }
        if (!hasMic()) return false
        pttHeld = true
        c.send(HubClient.PTT_START)
        changed()
        return true
    }

    fun pttUp() {
        if (!pttHeld) return
        pttHeld = false
        val wasTx = transmitting
        stopTx()
        client?.send(HubClient.PTT_END)
        if (wasTx && fx.mode == "clear") tone(ToneGenerator.TONE_PROP_BEEP2, 80)
        changed()
    }

    private fun startTx() {
        val c = client ?: return
        if (fx.mode == "clear") tone(ToneGenerator.TONE_PROP_BEEP, 100)
        vibrate(longArrayOf(0, 40))
        micLevel = 0f
        val ok = audio.startRecording { chunk ->
            c.send(HubClient.AUDIO, chunk)
            var ss = 0.0
            var i = 0
            while (i + 1 < chunk.size) {
                val v = ((chunk[i].toInt() and 0xff) or (chunk[i + 1].toInt() shl 8)).toShort() / 32768.0
                ss += v * v
                i += 2
            }
            val lvl = kotlin.math.sqrt(ss / (chunk.size / 2).coerceAtLeast(1)).toFloat()
            micLevel = maxOf(lvl, micLevel * 0.8f)
        }
        if (!ok) {
            c.send(HubClient.PTT_END)
            flash("Mikrofonen kunne ikke startes")
            return
        }
        transmitting = true
    }

    private fun stopTx() {
        if (!transmitting) return
        transmitting = false
        audio.stopRecording()
    }

    fun setChannel(idx: Int): Boolean {
        if (idx !in channelNames.indices) return false
        changeChannel(idx - channel)
        return true
    }

    fun changeChannel(delta: Int) {
        if (pttHeld || transmitting) return
        val n = channelNames.size
        channel = ((channel + delta) % n + n) % n
        receivingFrom = null
        client?.send(HubClient.JOIN, channel.toString().toByteArray())
        tone(ToneGenerator.TONE_PROP_BEEP, 30)
        saveChannel()
        changed()
    }

    // ---------------------------------------------------------------- notifikation

    private fun goForeground() {
        val n = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && hasMic()) type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            startForeground(NOTIF_ID, n, type)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, RadioService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val chName = channelNames.getOrElse(channel) { "Kanal ${channel + 1}" }
        val rx = receivingFrom
        val text = when {
            transmitting -> "● Sender"
            rx != null -> "◀ $rx taler"
            online -> "Lytter"
            else -> statusMsg
        }
        return Notification.Builder(this, NOTIF_CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_radio)
            .setContentTitle(String.format("CH %02d · %s", channel + 1, chName))
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .addAction(Notification.Action.Builder(null as android.graphics.drawable.Icon?, "Afbryd", stop).build())
            .build()
    }

    private fun changed() {
        if (wantConnected) {
            try {
                getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification())
            } catch (_: Exception) {
            }
        }
        onChange?.invoke()
    }

    // ---------------------------------------------------------------- låse (hold CPU og wifi vågne)

    @Suppress("DEPRECATION")
    private fun acquireLocks() {
        if (wakeLock == null) {
            wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PTTRadio:radio")
                .apply { setReferenceCounted(false); acquire() }
        }
        if (wifiLock == null) {
            val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifiLock = (applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
                .createWifiLock(mode, "PTTRadio:wifi")
                .apply { setReferenceCounted(false); acquire() }
        }
    }

    private fun releaseLocks() {
        try { wakeLock?.release() } catch (_: Exception) {}
        try { wifiLock?.release() } catch (_: Exception) {}
        wakeLock = null
        wifiLock = null
    }

    // ---------------------------------------------------------------- hjælpere

    fun hasMic() = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    /** Kaldes når brugeren netop har givet mikrofon-adgang, så forgrundstypen opdateres. */
    fun refreshForeground() {
        if (wantConnected) try { goForeground() } catch (_: Exception) {}
    }

    fun flash(msg: String) {
        flashMsg = msg
        ui.removeCallbacks(clearFlash)
        ui.postDelayed(clearFlash, 1800)
        changed()
    }

    private fun saveChannel() {
        getSharedPreferences("ptt", MODE_PRIVATE).edit().putInt("channel", channel).apply()
    }

    private fun tone(type: Int, ms: Int) {
        try { tones?.startTone(type, ms) } catch (_: Exception) {}
    }

    @Suppress("DEPRECATION")
    private fun vibrate(pattern: LongArray) {
        val v = getSystemService(VIBRATOR_SERVICE) as? Vibrator ?: return
        if (v.hasVibrator()) v.vibrate(VibrationEffect.createWaveform(pattern, -1))
    }
}
