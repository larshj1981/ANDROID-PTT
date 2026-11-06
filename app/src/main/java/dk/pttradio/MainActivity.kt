package dk.pttradio

import android.Manifest
import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.view.KeyEvent
import android.view.MotionEvent
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

/** Skærmen – al radio-logik ligger i RadioService, så den kører videre i baggrunden. */
class MainActivity : Activity() {

    companion object {
        const val DISCOVERY_PORT = 50001
        const val REQ_PERMS = 1
    }

    private lateinit var hostEdit: EditText
    private lateinit var nameEdit: EditText
    private lateinit var connectBtn: Button
    private lateinit var findBtn: Button
    private lateinit var statusText: TextView
    private lateinit var chNumText: TextView
    private lateinit var chNameText: TextView
    private lateinit var chInfoText: TextView
    private lateinit var rxText: TextView
    private lateinit var pttBtn: Button
    private lateinit var prevBtn: Button
    private lateinit var nextBtn: Button

    private val ui = Handler(Looper.getMainLooper())
    private var radio: RadioService? = null
    private var searchMsg: String? = null

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
        setContentView(R.layout.activity_main)

        hostEdit = findViewById(R.id.hostEdit)
        nameEdit = findViewById(R.id.nameEdit)
        connectBtn = findViewById(R.id.connectBtn)
        findBtn = findViewById(R.id.findBtn)
        statusText = findViewById(R.id.statusText)
        chNumText = findViewById(R.id.chNumText)
        chNameText = findViewById(R.id.chNameText)
        chInfoText = findViewById(R.id.chInfoText)
        rxText = findViewById(R.id.rxText)
        pttBtn = findViewById(R.id.pttBtn)
        prevBtn = findViewById(R.id.prevBtn)
        nextBtn = findViewById(R.id.nextBtn)

        val prefs = getSharedPreferences("ptt", MODE_PRIVATE)
        hostEdit.setText(prefs.getString("host", ""))
        nameEdit.setText(prefs.getString("name", ""))
        volumeControlStream = android.media.AudioManager.STREAM_MUSIC

        connectBtn.setOnClickListener {
            val r = radio ?: return@setOnClickListener
            if (r.wantConnected) r.disconnect() else connect(r)
        }
        findBtn.setOnClickListener { discover() }
        prevBtn.setOnClickListener { radio?.changeChannel(-1) }
        nextBtn.setOnClickListener { radio?.changeChannel(+1) }
        pttBtn.setOnTouchListener { v, ev ->
            when (ev.actionMasked) {
                MotionEvent.ACTION_DOWN -> pttDown()
                MotionEvent.ACTION_UP -> { radio?.pttUp(); v.performClick() }
                MotionEvent.ACTION_CANCEL -> radio?.pttUp()
            }
            true
        }

        askPermissions()
        updateUi()
    }

    override fun onStart() {
        super.onStart()
        bindService(Intent(this, RadioService::class.java), conn, Context.BIND_AUTO_CREATE)
    }

    override fun onStop() {
        radio?.pttUp() // send aldrig videre, når skærmen ikke er synlig
        radio?.onChange = null
        radio = null
        unbindService(conn)
        super.onStop()
    }

    private fun connect(r: RadioService) {
        val host = hostEdit.text.toString().trim()
        if (host.isEmpty()) {
            toast("Skriv hubbens adresse, eller tryk Søg")
            return
        }
        getSharedPreferences("ptt", MODE_PRIVATE).edit()
            .putString("host", host)
            .putString("name", nameEdit.text.toString().trim())
            .apply()
        searchMsg = null
        r.connect(host, nameEdit.text.toString())
    }

    private fun pttDown() {
        val r = radio ?: return
        if (!r.pttDown()) askPermissions()
    }

    // ---------------------------------------------------------------- søgning

    private fun discover() {
        findBtn.isEnabled = false
        searchMsg = "Søger efter hub…"
        updateUi()
        Thread {
            var found: String? = null
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
                        found = if (port == RadioService.DEFAULT_PORT) ip else "$ip:$port"
                    }
                }
            } catch (_: Exception) {
            }
            ui.post {
                val f = found
                if (f != null) {
                    hostEdit.setText(f)
                    searchMsg = "Hub fundet: $f"
                } else {
                    searchMsg = "Ingen hub fundet – skriv adressen"
                }
                updateUi()
            }
        }.start()
    }

    // ---------------------------------------------------------------- UI

    private fun updateUi() {
        val r = radio
        val want = r?.wantConnected == true
        val online = r?.online == true
        val pttHeld = r?.pttHeld == true
        val transmitting = r?.transmitting == true
        val channel = r?.channel ?: 0
        val names = r?.channelNames ?: listOf("Kanal 1")
        val status = r?.channelStatus ?: emptyList()

        connectBtn.text = if (want) "Afbryd" else "Forbind"
        connectBtn.isEnabled = r != null
        hostEdit.isEnabled = !want
        nameEdit.isEnabled = !want
        findBtn.isEnabled = !want && searchMsg != "Søger efter hub…"
        prevBtn.isEnabled = !pttHeld
        nextBtn.isEnabled = !pttHeld

        statusText.text = if (!want && searchMsg != null) searchMsg else (r?.statusMsg ?: "")
        chNumText.text = String.format("CH %02d", channel + 1)
        chNameText.text = names.getOrElse(channel) { "Kanal ${channel + 1}" }

        val st = status.getOrNull(channel)
        val active = status.withIndex()
            .filter { it.index != channel && it.value.talker != null }
            .joinToString(" ") { "CH${it.index + 1}" }
        chInfoText.text = when {
            !online -> "—"
            active.isNotEmpty() -> "${st?.users ?: 0} på kanalen · aktivitet: $active"
            else -> "${st?.users ?: 0} på kanalen"
        }

        val flash = r?.flashMsg
        val rx = r?.receivingFrom
        when {
            flash != null -> setRx(flash, "#FFB347")
            transmitting -> setRx("● SENDER", "#FF5A5F")
            pttHeld -> setRx("Venter på kanal…", "#FFB347")
            rx != null -> setRx("◀ $rx", "#9CFFB0")
            else -> setRx("", "#9CFFB0")
        }

        val busy = st?.talker != null && !transmitting
        val (color, label) = when {
            !online -> "#2A2D2E" to "PTT"
            transmitting -> "#D7263D" to "SENDER"
            pttHeld -> "#C77D00" to "…"
            busy -> "#3A3320" to "OPTAGET"
            else -> "#1F6B3A" to "PTT"
        }
        pttBtn.text = label
        pttBtn.setTextColor(if (online) Color.WHITE else Color.parseColor("#777777"))
        pttBtn.background = GradientDrawable().apply {
            setColor(Color.parseColor(color))
            cornerRadius = 40 * resources.displayMetrics.density
            setStroke((2 * resources.displayMetrics.density).toInt(), Color.parseColor("#44FFFFFF"))
        }
    }

    private fun setRx(text: String, color: String) {
        rxText.text = text
        rxText.setTextColor(Color.parseColor(color))
    }

    // ---------------------------------------------------------------- fysiske knapper

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (isPttKey(keyCode) && radio?.wantConnected == true) {
            if (event.repeatCount == 0) pttDown()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onKeyUp(keyCode: Int, event: KeyEvent): Boolean {
        if (isPttKey(keyCode) && radio?.wantConnected == true) {
            radio?.pttUp()
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
