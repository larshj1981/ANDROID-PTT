package dk.pttradio

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.view.KeyEvent
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Skærmen: en taktisk håndradio. Al radio-logik ligger i RadioService (kører i baggrunden). */
class MainActivity : Activity(), RadioView.Listener {

    companion object {
        const val DISCOVERY_PORT = 50001
        const val REQ_PERMS = 1
    }

    private lateinit var view: RadioView
    private lateinit var am: AudioManager
    private val ui = Handler(Looper.getMainLooper())
    private var radio: RadioService? = null
    private var lastActivity = 0L
    private val clockFmt = SimpleDateFormat("HH:mm", Locale.getDefault())

    private val ticker = object : Runnable {
        override fun run() {
            updateUi()
            ui.postDelayed(this, 250)
        }
    }

    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            val s = (binder as RadioService.LocalBinder).service
            radio = s
            s.onChange = { updateUi() }
            updateUi()
        }

        override fun onServiceDisconnected(name: ComponentName) {
            radio = null
            updateUi()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        am = getSystemService(AUDIO_SERVICE) as AudioManager
        volumeControlStream = AudioManager.STREAM_MUSIC
        window.statusBarColor = Color.parseColor("#121412")
        window.navigationBarColor = Color.parseColor("#121412")
        applyLockScreen()
        view = RadioView(this)
        view.listener = this
        setContentView(view)
        askPermissions()
    }

    override fun onStart() {
        super.onStart()
        bindService(Intent(this, RadioService::class.java), conn, Context.BIND_AUTO_CREATE)
        ui.post(ticker)
    }

    override fun onStop() {
        ui.removeCallbacks(ticker)
        radio?.releaseScreenPtt()   // en sending startet fra skærmen må ikke fortsætte, når skærmen forsvinder
        view.releasePtt()
        radio?.onChange = null
        radio = null
        unbindService(conn)
        super.onStop()
    }

    // ---------------------------------------------------------------- RadioView.Listener

    override fun onPtt(down: Boolean) {
        touch()
        val r = radio ?: return
        if (down) {
            if (!r.pttDown()) askPermissions()
        } else {
            r.pttUp()
        }
    }

    override fun onChannelStep(d: Int) {
        touch()
        radio?.changeChannel(d)
    }

    override fun onVolumeStep(d: Int) {
        touch()
        am.adjustStreamVolume(
            AudioManager.STREAM_MUSIC,
            if (d > 0) AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER, 0
        )
        updateUi()
    }

    override fun onMute() {
        touch()
        radio?.toggleMute()
    }

    override fun onSettings() {
        touch()
        openMenu()
    }

    /** Tryk på displayet: vælg kanal direkte fra listen, eller tænd/sluk radioen. */
    override fun onDisplay() {
        touch()
        val r = radio ?: return
        if (!r.wantConnected) {
            powerOn(r)
            return
        }
        val names = r.channelNames
        val items = names.mapIndexed { i, n -> (if (i == r.channel) "●  " else "    ") + String.format("CH %02d   %s", i + 1, n) } +
            "⏻  Sluk radioen"
        AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
            .setTitle("Vælg kanal")
            .setItems(items.toTypedArray()) { _, which ->
                val rr = radio ?: return@setItems
                if (which < names.size) rr.setChannel(which) else rr.disconnect()
                updateUi()
            }
            .setNegativeButton("Annullér", null)
            .show()
    }

    private fun touch() {
        lastActivity = System.currentTimeMillis()
    }

    private fun powerOn(r: RadioService) {
        val prefs = getSharedPreferences("ptt", MODE_PRIVATE)
        val host = prefs.getString("host", "") ?: ""
        if (host.isBlank()) {
            openMenu()
            return
        }
        r.connect(host, prefs.getString("name", "") ?: "")
    }

    // ---------------------------------------------------------------- display

    /** Vis radioen oven på låseskærmen (så PTT kan bruges uden at låse telefonen op). */
    @Suppress("DEPRECATION")
    private fun applyLockScreen() {
        val on = getSharedPreferences("ptt", MODE_PRIVATE).getBoolean("lockscreen", true)
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(on)
        } else if (on) {
            window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED)
        }
    }

    private fun volumeLevel(): Int {
        val v = am.getStreamVolume(AudioManager.STREAM_MUSIC)
        val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC).coerceAtLeast(1)
        return (v * 10 + max / 2) / max
    }

    private fun updateUi() {
        if (intent?.getBooleanExtra("demo", false) == true) {  // bruges kun til skærmbilleder i bygningen
            view.state = RadioView.State(
                power = true, online = true, backlight = 2, txrx = "RX", mode = "DIG",
                clock = clockFmt.format(Date()), big = "CH 03", name = "CHARLIE", status = "RX ANNA", volume = 7
            )
            return
        }
        val r = radio
        val now = System.currentTimeMillis()
        val power = r?.wantConnected == true
        val online = r?.online == true
        val tx = r?.transmitting == true
        val held = r?.pttHeld == true
        val rx = r?.receivingFrom
        val muted = r?.muted == true
        if (tx || rx != null) lastActivity = now
        val ch = r?.channel ?: 0
        val names = r?.channelNames ?: listOf("Kanal 1")
        val status = r?.channelStatus ?: emptyList()

        if (!power) {
            val hasHost = !getSharedPreferences("ptt", MODE_PRIVATE).getString("host", "").isNullOrBlank()
            view.state = RadioView.State(
                big = "OFF", name = if (hasHost) "TRYK HER FOR AT TÆNDE" else "SE INDSTILLINGER",
                status = r?.offReason ?: "", muted = muted, volume = volumeLevel()
            )
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            return
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val name = names.getOrElse(ch) { "Kanal ${ch + 1}" }.uppercase().take(18)
        val here = status.getOrNull(ch)
        val flash = r?.flashMsg
        val line = when {
            !online -> (r?.lastError?.let { "$it: ${r.targetLabel}" } ?: "SØGER HUB").take(24) +
                ".".repeat(((now / 500) % 4).toInt())
            flash != null -> flash.uppercase().take(24)
            tx -> ""
            held -> "VENTER..."
            rx != null -> "RX ${rx.uppercase().take(16)}"
            here?.talker != null -> "OPTAGET ${here.talker.uppercase().take(12)}"
            else -> {
                val act = status.withIndex().filter { it.index != ch && it.value.talker != null }
                    .joinToString(" ") { "${it.index + 1}" }
                "${here?.users ?: 0} STN" + if (act.isNotEmpty()) "  AKT $act" else ""
            }
        }
        view.state = RadioView.State(
            power = true,
            online = online,
            tx = tx,
            held = held && !tx,
            backlight = if (now - lastActivity < 8000) 2 else 1,
            txrx = if (tx) "TX" else if (rx != null) "RX" else "",
            mode = when (r?.fx?.mode) { "fm" -> "FM"; "clear" -> "CLR"; else -> "DIG" },
            muted = muted,
            clock = clockFmt.format(Date(now)),
            big = String.format("CH %02d", ch + 1),
            name = name,
            status = line,
            level = r?.micLevel ?: 0f,
            showLevel = tx && flash == null,
            volume = volumeLevel(),
        )
    }

    // ---------------------------------------------------------------- menu (indstillinger)

    private fun openMenu() {
        val prefs = getSharedPreferences("ptt", MODE_PRIVATE)
        val dp = resources.displayMetrics.density
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding((20 * dp).toInt(), (12 * dp).toInt(), (20 * dp).toInt(), 0)
        }

        fun label(t: String) = TextView(this).apply {
            text = t
            setTextColor(Color.parseColor("#8a8f86"))
            textSize = 12f
            setPadding(0, (10 * dp).toInt(), 0, 0)
        }

        val hostRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val host = EditText(this).apply {
            setText(prefs.getString("host", ""))
            hint = "fx 192.168.1.10"
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            isSingleLine = true
        }
        val find = Button(this).apply { text = "Søg" }
        hostRow.addView(host, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        hostRow.addView(find)
        val name = EditText(this).apply {
            setText(prefs.getString("name", ""))
            hint = "Dit kaldenavn"
            inputType = InputType.TYPE_TEXT_VARIATION_PERSON_NAME
            isSingleLine = true
        }
        val modeKeys = VoiceFx.MODES.keys.toList()
        val mode = Spinner(this).apply {
            adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, VoiceFx.MODES.values.toList())
            setSelection(modeKeys.indexOf(radio?.fx?.mode ?: prefs.getString("mode", "digital")).coerceAtLeast(0))
        }
        val help = TextView(this).apply {
            text = "PTT: hold PTT-knappen eller lydstyrke ned.  Kanal: ▲▼, eller tryk på displayet for at " +
                "vælge direkte og tænde/slukke.  Radioen lytter videre i baggrunden.\n\n" +
                "Bluetooth-/headset-knap: hold inde for at tale, eller giv et kort klik for at starte og et klik mere for at stoppe."
            setTextColor(Color.parseColor("#8a8f86"))
            textSize = 11f
            setPadding(0, (14 * dp).toInt(), 0, 0)
        }
        box.addView(label("Hub-adresse"))
        box.addView(hostRow)
        box.addView(label("Kaldenavn"))
        box.addView(name)
        box.addView(label("Radiolyd"))
        box.addView(mode)
        val lock = CheckBox(this).apply {
            text = "Vis radioen på låseskærmen"
            isChecked = prefs.getBoolean("lockscreen", true)
        }
        val media = CheckBox(this).apply {
            text = "Bluetooth-/headset-knap som PTT (også med slukket skærm)"
            isChecked = prefs.getBoolean("mediaptt", true)
        }
        box.addView(lock)
        box.addView(media)
        box.addView(help)

        find.setOnClickListener {
            find.isEnabled = false
            find.text = "…"
            Thread {
                val f = discover()
                ui.post {
                    find.isEnabled = true
                    find.text = "Søg"
                    if (f != null) host.setText(f) else toast("Ingen hub fundet – skriv adressen fra PTTHub-vinduet")
                }
            }.start()
        }

        fun save(): Boolean {
            val oldHost = prefs.getString("host", "")
            val oldName = prefs.getString("name", "")
            val h = host.text.toString().trim()
            val n = name.text.toString().trim()
            prefs.edit().putString("host", h).putString("name", n)
                .putBoolean("lockscreen", lock.isChecked).putBoolean("mediaptt", media.isChecked).apply()
            applyLockScreen()
            radio?.refreshMediaButton()
            radio?.setMode(modeKeys[mode.selectedItemPosition.coerceIn(0, modeKeys.size - 1)])
            return h != oldHost || n != oldName
        }

        AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
            .setTitle("Indstillinger · PTT Radio ${radio?.appVersion ?: ""}")
            .setView(box)
            .setPositiveButton("Gem og tænd") { _, _ ->
                val changed = save()
                val r = radio ?: return@setPositiveButton
                if (r.wantConnected && changed) r.disconnect()
                if (!r.wantConnected) powerOn(r)
                updateUi()
            }
            .setNeutralButton("Gem") { _, _ -> save(); updateUi() }
            .setNegativeButton("Annullér", null)
            .show()
    }

    private fun discover(): String? {
        try {
            DatagramSocket().use { s ->
                s.broadcast = true
                s.soTimeout = 1500
                val msg = "PTTHUB?".toByteArray()
                val bcast = InetAddress.getByName("255.255.255.255")
                repeat(2) { s.send(DatagramPacket(msg, msg.size, bcast, DISCOVERY_PORT)) }
                val buf = ByteArray(256)
                val p = DatagramPacket(buf, buf.size)
                s.receive(p)
                val reply = String(buf, 0, p.length, Charsets.UTF_8)
                if (reply.startsWith("PTTHUB ")) {
                    val port = reply.split(" ").getOrNull(1)?.toIntOrNull() ?: RadioService.DEFAULT_PORT
                    val ip = p.address.hostAddress
                    return if (port == RadioService.DEFAULT_PORT) ip else "$ip:$port"
                }
            }
        } catch (_: Exception) {
        }
        return null
    }

    // ---------------------------------------------------------------- fysiske knapper

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (isPttKey(keyCode) && radio?.wantConnected == true) {
            if (event.repeatCount == 0) onPtt(true)
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onKeyUp(keyCode: Int, event: KeyEvent): Boolean {
        if (isPttKey(keyCode) && radio?.wantConnected == true) {
            onPtt(false)
            return true
        }
        return super.onKeyUp(keyCode, event)
    }

    /**
     * PTT-knapper når appen er på skærmen: lydstyrke ned og headset-/Bluetooth-knappen.
     * USB-C- og Bluetooth-headset sender typisk "play/pause" i stedet for den gamle headset-knap.
     * Når appen er på skærmen, får vi selv knaptrykkene, så hold-for-at-tale virker.
     * (Med slukket skærm tager Android et langt tryk til stemmeassistenten – se RadioService.mediaKey.)
     */
    private fun isPttKey(code: Int): Boolean = when (code) {
        KeyEvent.KEYCODE_VOLUME_DOWN -> true
        KeyEvent.KEYCODE_HEADSETHOOK, KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
        KeyEvent.KEYCODE_MEDIA_PLAY, KeyEvent.KEYCODE_MEDIA_PAUSE ->
            getSharedPreferences("ptt", MODE_PRIVATE).getBoolean("mediaptt", true)
        else -> false
    }

    // ---------------------------------------------------------------- tilladelser

    private fun askPermissions() {
        val need = mutableListOf<String>()
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
            need += Manifest.permission.RECORD_AUDIO
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED
        ) need += "android.permission.POST_NOTIFICATIONS"
        if (need.isNotEmpty()) requestPermissions(need.toTypedArray(), REQ_PERMS)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (requestCode != REQ_PERMS) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            radio?.refreshForeground()
        } else {
            toast("Mikrofon-adgang kræves for at kunne sende")
        }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
