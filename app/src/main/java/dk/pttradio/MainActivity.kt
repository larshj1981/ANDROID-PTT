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
    private var entry = ""
    private var entryTime = 0L
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
        radio?.pttUp()          // send aldrig videre, når skærmen ikke er synlig
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
        entry = ""
        radio?.changeChannel(d)
    }

    override fun onVolumeStep(d: Int) {
        touch()
        am.adjustStreamVolume(
            AudioManager.STREAM_MUSIC,
            if (d > 0) AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER, 0
        )
        val v = am.getStreamVolume(AudioManager.STREAM_MUSIC)
        val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC).coerceAtLeast(1)
        radio?.flash("VOL ${(v * 10 + max / 2) / max}")
        updateUi()
    }

    override fun onKey(name: String) {
        touch()
        val r = radio
        when {
            name.length == 1 && name[0].isDigit() -> {
                entry = (entry + name).takeLast(2)
                entryTime = System.currentTimeMillis()
            }
            name == "ENT" -> if (entry.isNotEmpty()) {
                val n = entry.toInt()
                entry = ""
                if (r != null && !r.setChannel(n - 1)) r.flash("UGYLDIG KANAL")
            }
            name == "CLR" -> entry = ""
            name == "UP" -> onChannelStep(1)
            name == "DN" -> onChannelStep(-1)
            name == "MENU" -> openMenu()
            name == "PWR" -> if (r != null) {
                if (r.wantConnected) r.disconnect() else powerOn(r)
            }
        }
        updateUi()
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

    private fun updateUi() {
        if (intent?.getBooleanExtra("demo", false) == true) {  // bruges kun til skærmbilleder i bygningen
            view.state = RadioView.State(
                power = true, online = true, rx = true, backlight = 2, txrx = "RX", mode = "DIG",
                clock = clockFmt.format(Date()), big = "CH 03", name = "CHARLIE", status = "RX ANNA",
                chFrac = 2f / 7, volFrac = 0.7f
            )
            return
        }
        val r = radio
        val now = System.currentTimeMillis()
        if (entry.isNotEmpty() && now - entryTime > 6000) entry = ""
        val power = r?.wantConnected == true
        val online = r?.online == true
        val tx = r?.transmitting == true
        val held = r?.pttHeld == true
        val rx = r?.receivingFrom
        if (tx || rx != null) lastActivity = now
        val ch = r?.channel ?: 0
        val names = r?.channelNames ?: listOf("Kanal 1")
        val status = r?.channelStatus ?: emptyList()
        val vol = am.getStreamVolume(AudioManager.STREAM_MUSIC).toFloat() /
            am.getStreamMaxVolume(AudioManager.STREAM_MUSIC).coerceAtLeast(1)
        val chFrac = if (names.size > 1) ch.toFloat() / (names.size - 1) else 0f

        if (!power) {
            val hasHost = !getSharedPreferences("ptt", MODE_PRIVATE).getString("host", "").isNullOrBlank()
            view.state = RadioView.State(
                big = "OFF", name = if (hasHost) "TRYK PWR" else "TRYK MENU",
                chFrac = chFrac, volFrac = vol
            )
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            return
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val name = names.getOrElse(ch) { "Kanal ${ch + 1}" }.uppercase().take(16)
        val here = status.getOrNull(ch)
        val flash = r?.flashMsg
        val line = when {
            !online -> "SØGER HUB" + ".".repeat(((now / 500) % 4).toInt())
            flash != null -> flash.uppercase().take(22)
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
            rx = rx != null,
            backlight = if (now - lastActivity < 8000) 2 else 1,
            txrx = if (tx) "TX" else if (rx != null) "RX" else "",
            mode = when (r?.fx?.mode) { "fm" -> "FM"; "clear" -> "CLR"; else -> "DIG" },
            clock = clockFmt.format(Date(now)),
            big = if (entry.isNotEmpty()) "CH " + entry.padEnd(2, '_') else String.format("CH %02d", ch + 1),
            name = name,
            status = line,
            level = r?.micLevel ?: 0f,
            showLevel = tx && flash == null,
            chFrac = chFrac,
            volFrac = vol,
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
            text = "PTT: side-knappen eller lydstyrke ned.  Kanal: CH-knap, ▲▼ eller tal + ENT.  " +
                "Lydstyrke: VOL-knap.  PWR: tænd/sluk.  Radioen lytter videre i baggrunden."
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
            prefs.edit().putString("host", h).putString("name", n).apply()
            radio?.setMode(modeKeys[mode.selectedItemPosition.coerceIn(0, modeKeys.size - 1)])
            return h != oldHost || n != oldName
        }

        AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
            .setTitle("Indstillinger")
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

    private fun isPttKey(code: Int) =
        code == KeyEvent.KEYCODE_VOLUME_DOWN || code == KeyEvent.KEYCODE_HEADSETHOOK

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
