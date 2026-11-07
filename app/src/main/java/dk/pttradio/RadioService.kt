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
import android.media.session.MediaSession
import android.media.session.PlaybackState
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
import android.os.SystemClock
import android.view.KeyEvent

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
    /** Kort årsag til at forbindelsen fejler (vises på displayet), fx "HUB SVARER IKKE". */
    var lastError: String? = null
        private set
    /** Hvorfor radioen blev slukket af hubben (fx smidt af). */
    var offReason: String? = null
        private set
    val appVersion: String by lazy {
        try { packageManager.getPackageInfo(packageName, 0).versionName ?: "" } catch (_: Exception) { "" }
    }
    val fx = VoiceFx("digital")
    @Volatile var muted = false
        private set
    /** Sendingen blev startet fra skærmen (og skal stoppes, hvis skærmen forsvinder). */
    private var pttFromScreen = false

    // Bluetooth-/headset-knap
    private var session: MediaSession? = null
    private var btDownAt = 0L
    private var btLatched = false
    private var btIgnoreUp = false
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
        muted = prefs.getBoolean("muted", false)
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
        releaseMediaButton()
        super.onDestroy()
    }

    // ---------------------------------------------------------------- forbindelse

    fun connect(hostPort: String, userName: String) {
        val (h, p) = parseAddress(hostPort)
        host = h
        port = p
        name = userName.trim().ifEmpty { Build.MODEL }
        wantConnected = true
        statusMsg = "Forbinder…"
        lastError = null
        offReason = null

        // Gør tjenesten "startet" og forgrund, så den overlever at appen lukkes
        startForegroundService(Intent(this, RadioService::class.java))
        goForeground()
        acquireLocks()
        setupMediaButton()
        startClient()
        changed()
    }

    /** Tolerant hub-adresse: "http://192.168.1.71:8080/" -> ("192.168.1.71", 50000). */
    private fun parseAddress(text: String): Pair<String, Int> {
        var raw = text.trim().replace(Regex("^[a-zA-Z][a-zA-Z0-9+.-]*://"), "")
        raw = raw.substringBefore('/').substringBefore('?').replace(" ", "")
        if (Regex("""\d{1,3}(,\d{1,3}){3}(:\d+)?""").matches(raw)) raw = raw.replace(',', '.')
        var h = raw
        var p = DEFAULT_PORT
        val idx = raw.lastIndexOf(':')
        if (idx > 0 && raw.count { it == ':' } == 1) {
            raw.substring(idx + 1).toIntOrNull()?.let { h = raw.substring(0, idx); p = it }
        }
        if (p == 8080) p = DEFAULT_PORT   // webinterfacets port – radioen bruger 50000
        return h.trim('.') to p
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
        btLatched = false
        releaseLocks()
        releaseMediaButton()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        changed()
    }

    private fun startClient() {
        client?.close()
        val c = HubClient(::onHubEvent)
        client = c
        c.connect(host, port, name, channel, appVersion)
    }

    /** Netværkstråd: lyd (og start/slut-lyde) behandles her, så rækkefølgen bevares. */
    private fun onHubEvent(c: HubClient, e: HubEvent) {
        if (c !== client) return
        when (e) {
            is HubEvent.Audio -> {
                if (!transmitting && !muted) {
                    val out = fx.process(e.data)
                    if (out.isNotEmpty()) audio.play(out)
                }
                return
            }
            is HubEvent.RxStart -> if (!transmitting && !muted) audio.play(fx.startSound())
            is HubEvent.RxEnd -> if (!transmitting && !muted) audio.play(fx.endSound())
            else -> Unit
        }
        ui.post { if (c === client) handle(e) }
    }

    fun toggleMute() {
        muted = !muted
        getSharedPreferences("ptt", MODE_PRIVATE).edit().putBoolean("muted", muted).apply()
        flash(if (muted) "MUTE" else "LYD TIL")
    }

    fun setMode(m: String) {
        fx.setMode(m)
        getSharedPreferences("ptt", MODE_PRIVATE).edit().putString("mode", fx.mode).apply()
        changed()
    }

    private fun handle(e: HubEvent) {
        when (e) {
            is HubEvent.Connected -> { statusMsg = "Forbundet til $host"; lastError = null }
            is HubEvent.SetChannel -> if (e.index in channelNames.indices && e.index != channel) {
                channel = e.index        // hubben har flyttet/sorteret kanalerne
                receivingFrom = null
                saveChannel()
            }
            is HubEvent.Kicked -> {
                flash(e.message)
                disconnect()
                offReason = "SMIDT AF HUBBEN"
            }
            is HubEvent.Disconnected -> {
                stopTx()
                pttHeld = false
                btLatched = false
                receivingFrom = null
                channelStatus = emptyList()
                client = null
                lastError = e.reason
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
                if (btLatched) { btLatched = false; pttUp() }
            }
            is HubEvent.RxStart -> receivingFrom = e.name
            is HubEvent.RxEnd -> {
                if (transmitting) {
                    stopTx()
                    flash("Maks. sendetid nået")
                    if (btLatched) { btLatched = false; pttUp() }
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
    fun pttDown(fromScreen: Boolean = true): Boolean {
        if (pttHeld) return true
        val c = client
        if (c == null || !c.connected) {
            tone(ToneGenerator.TONE_PROP_NACK, 200)
            flash("Ikke forbundet")
            return true
        }
        if (!hasMic()) return false
        pttHeld = true
        pttFromScreen = fromScreen
        c.send(HubClient.PTT_START)
        changed()
        return true
    }

    /** Kaldes når skærmen forsvinder: stop kun en sending, der blev startet fra skærmen. */
    fun releaseScreenPtt() {
        if (pttHeld && pttFromScreen) pttUp()
    }

    fun pttUp() {
        if (!pttHeld) return
        btLatched = false
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

    // ---------------------------------------------------------------- Bluetooth-/headset-knap som PTT

    /**
     * Medieknappen (headset-knap, Bluetooth-PTT-knap, play/pause) bruges som PTT – også med slukket skærm.
     * Holdes knappen inde, sendes der så længe den holdes. Et kort klik starter sendingen, næste klik stopper.
     */
    private fun setupMediaButton() {
        if (!getSharedPreferences("ptt", MODE_PRIVATE).getBoolean("mediaptt", true)) {
            releaseMediaButton()
            return
        }
        if (session != null) return
        val s = MediaSession(this, "PTTRadio")
        s.setCallback(object : MediaSession.Callback() {
            override fun onMediaButtonEvent(intent: Intent): Boolean {
                @Suppress("DEPRECATION")
                val ev = intent.getParcelableExtra<KeyEvent>(Intent.EXTRA_KEY_EVENT) ?: return false
                return mediaKey(ev) || super.onMediaButtonEvent(intent)
            }
        }, ui)
        s.setPlaybackState(
            PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE or PlaybackState.ACTION_PLAY_PAUSE)
                .setState(PlaybackState.STATE_PLAYING, 0, 1f)
                .build()
        )
        s.isActive = true
        session = s
    }

    private fun releaseMediaButton() {
        session?.let {
            it.isActive = false
            it.release()
        }
        session = null
    }

    fun refreshMediaButton() {
        if (wantConnected) setupMediaButton() else releaseMediaButton()
    }

    private fun mediaKey(ev: KeyEvent): Boolean {
        when (ev.keyCode) {
            KeyEvent.KEYCODE_HEADSETHOOK, KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_MEDIA_PLAY, KeyEvent.KEYCODE_MEDIA_PAUSE -> Unit
            else -> return false
        }
        val now = SystemClock.uptimeMillis()
        when (ev.action) {
            KeyEvent.ACTION_DOWN -> {
                if (ev.repeatCount > 0) return true
                if (btLatched) {                       // andet klik: stop
                    btLatched = false
                    btIgnoreUp = true
                    pttUp()
                } else {
                    btDownAt = now
                    btIgnoreUp = false
                    if (!pttDown(fromScreen = false)) flash("Mikrofon-adgang mangler")
                }
            }
            KeyEvent.ACTION_UP -> {
                if (btIgnoreUp) { btIgnoreUp = false; return true }
                if (!pttHeld) return true
                if (now - btDownAt < 350) {
                    btLatched = true                   // kort klik: bliv ved med at sende
                    vibrate(longArrayOf(0, 30, 60, 30))
                } else {
                    pttUp()                            // holdt inde: stop ved slip
                }
            }
        }
        return true
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
            transmitting -> if (btLatched) "● Sender (klik på knappen for at stoppe)" else "● Sender"
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
