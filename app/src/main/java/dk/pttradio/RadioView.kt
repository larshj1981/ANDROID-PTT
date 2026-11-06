package dk.pttradio

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

/** Tegner en taktisk håndradio (samme design som Windows-klienten) og håndterer berøring. */
class RadioView(ctx: Context) : View(ctx) {

    interface Listener {
        fun onPtt(down: Boolean)
        fun onKey(name: String)
        fun onChannelStep(d: Int)
        fun onVolumeStep(d: Int)
    }

    data class State(
        val power: Boolean = false,
        val online: Boolean = false,
        val tx: Boolean = false,
        val rx: Boolean = false,
        val backlight: Int = 0,          // 0 slukket, 1 dæmpet, 2 tændt
        val txrx: String = "",
        val mode: String = "",
        val clock: String = "",
        val big: String = "OFF",
        val name: String = "",
        val status: String = "",
        val level: Float = 0f,
        val showLevel: Boolean = false,
        val chFrac: Float = 0f,
        val volFrac: Float = 0.5f,
    )

    var listener: Listener? = null
    var state = State()
        set(v) {
            if (v != field) { field = v; invalidate() }
        }

    companion object {
        const val W = 400f
        const val H = 800f
        val KEY_ROWS = listOf(
            listOf("1", "2", "3", "UP"), listOf("4", "5", "6", "DN"),
            listOf("7", "8", "9", "MENU"), listOf("CLR", "0", "ENT", "PWR")
        )
        val KEY_LABEL = mapOf("UP" to "▲", "DN" to "▼")
        val KEY_COLOR = mapOf(
            "MENU" to Color.parseColor("#e0b050"), "CLR" to Color.parseColor("#e0b050"),
            "ENT" to Color.parseColor("#8fd08f"), "PWR" to Color.parseColor("#ff6a5a")
        )
        private val CH_KNOB = floatArrayOf(196f, 138f, 31f)
        private val VOL_KNOB = floatArrayOf(306f, 140f, 25f)
        private const val LCD_INK = 0xFF16200E.toInt()
    }

    private var sc = 1f
    private var ox = 0f
    private var oy = 0f
    private var pressedKey: String? = null
    private var pttPointer = -1

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
    private val txt = Paint(Paint.ANTI_ALIAS_FLAG).apply { textAlign = Paint.Align.CENTER }
    private val mono = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
    private val sans = Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD)
    private val r = RectF()

    init {
        setBackgroundColor(Color.parseColor("#121412"))
        isHapticFeedbackEnabled = true
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        sc = min(w / W, h / H)
        ox = (w - W * sc) / 2
        oy = (h - H * sc) / 2
    }

    // ------------------------------------------------------------ tegning
    private fun rr(x1: Float, y1: Float, x2: Float, y2: Float, rad: Float, c: Canvas, color: Int, edge: Int? = null, ew: Float = 2f) {
        r.set(x1, y1, x2, y2)
        fill.color = color
        c.drawRoundRect(r, rad, rad, fill)
        if (edge != null) {
            stroke.color = edge
            stroke.strokeWidth = ew
            c.drawRoundRect(r, rad, rad, stroke)
        }
    }

    private fun text(c: Canvas, s: String, x: Float, y: Float, size: Float, color: Int, tf: Typeface = sans) {
        txt.textSize = size
        txt.color = color
        txt.typeface = tf
        c.drawText(s, x, y + size * 0.36f, txt)
    }

    private fun col(s: String) = Color.parseColor(s)

    override fun onDraw(canvas: Canvas) {
        val st = state
        canvas.save()
        canvas.translate(ox, oy)
        canvas.scale(sc, sc)
        val c = canvas

        // antenne
        rr(72f, 6f, 108f, 200f, 16f, c, col("#191a18"), col("#2c2e2a"))
        stroke.color = col("#232522"); stroke.strokeWidth = 2f
        var y = 30f
        while (y < 180f) { c.drawLine(78f, y, 102f, y, stroke); y += 12f }
        rr(62f, 150f, 118f, 205f, 8f, c, col("#202220"), col("#30332e"), 1f)

        // topplade
        rr(140f, 100f, 356f, 205f, 14f, c, col("#22241f"), col("#363a33"))
        knob(c, CH_KNOB, st.chFrac, "CH")
        knob(c, VOL_KNOB, st.volFrac, "VOL")
        fill.color = when { st.tx -> col("#ff3030"); st.rx -> col("#30e050"); else -> col("#3a2a2a") }
        c.drawCircle(252f, 124f, 6f, fill)
        text(c, "TX/RX", 252f, 139f, 8f, col("#8a8f86"))

        // krop
        rr(38f, 190f, 362f, 790f, 38f, c, col("#2b2e29"), col("#3d4239"), 3f)
        rr(56f, 208f, 344f, 774f, 26f, c, col("#24261f"), col("#30332c"), 1f)
        rr(42f, 240f, 50f, 740f, 4f, c, col("#232520"))
        rr(350f, 240f, 358f, 740f, 4f, c, col("#232520"))

        // side-PTT
        rr(10f, 300f, 44f, 480f, 12f, c, if (pttPointer >= 0) col("#0f100e") else col("#1b1d19"), col("#3a3e36"))
        stroke.color = col("#2c2f29"); stroke.strokeWidth = 2f
        y = 318f
        while (y < 466f) { c.drawLine(17f, y, 37f, y, stroke); y += 10f }
        c.save(); c.rotate(-90f, 27f, 390f); text(c, "PTT", 27f, 390f, 12f, col("#c9cbc2")); c.restore()

        // mærkat + gitter
        text(c, "TAC-PTT  R-1", 200f, 228f, 12f, col("#9a9f94"))
        fill.color = col("#151613")
        for (row in 0 until 5) for (cl in 0 until 16) {
            val x = 95f + cl * 14 + if (row % 2 == 1) 7 else 0
            if (x <= 310f) c.drawCircle(x, 250f + row * 12, 3f, fill)
        }

        // LCD
        rr(70f, 320f, 330f, 482f, 14f, c, col("#121311"), col("#3a3e36"))
        fill.color = when (st.backlight) { 2 -> col("#a9c98a"); 1 -> col("#7c8f6a"); else -> col("#2f362b") }
        c.drawRect(84f, 333f, 316f, 469f, fill)
        stroke.color = col("#0c0d0b"); stroke.strokeWidth = 2f
        c.drawRect(84f, 333f, 316f, 469f, stroke)
        val ink = if (st.power) LCD_INK else col("#3c4636")
        if (st.power) {
            fill.color = ink
            if (st.online) for (i in 0 until 4) c.drawRect(92f + i * 7, 352f - (i + 1) * 3.5f, 97f + i * 7, 352f, fill)
            text(c, st.txrx, 140f, 345f, 13f, ink, mono)
            text(c, st.mode, 198f, 345f, 12f, ink, mono)
            text(c, st.clock, 282f, 345f, 12f, ink, mono)
        }
        stroke.color = ink; stroke.strokeWidth = 1f
        c.drawLine(88f, 357f, 312f, 357f, stroke)
        text(c, st.big, 200f, 386f, 34f, ink, mono)
        text(c, st.name, 200f, 420f, 15f, ink, mono)
        if (st.showLevel) {
            text(c, "TX", 120f, 448f, 13f, ink, mono)
            val n = min(10, (st.level * 60 + 0.5f).toInt())
            fill.color = ink
            for (i in 0 until n) c.drawRect(150f + i * 10, 441f, 157f + i * 10, 455f, fill)
        } else {
            text(c, st.status, 200f, 448f, 13f, ink, mono)
        }

        // tastatur
        for ((ri, row) in KEY_ROWS.withIndex()) for ((ci, name) in row.withIndex()) {
            val x1 = 70f + ci * 66
            val y1 = 500f + ri * 66
            rr(x1, y1, x1 + 58, y1 + 56, 12f, c, if (pressedKey == name) col("#1f221d") else col("#3b3f38"), col("#4a4f46"))
            val label = KEY_LABEL[name] ?: name
            text(c, label, x1 + 29, y1 + 28, if (label.length == 1) 22f else 12f, KEY_COLOR[name] ?: col("#e8e8e0"))
        }
        canvas.restore()
    }

    private fun knob(c: Canvas, k: FloatArray, frac: Float, label: String) {
        val (cx, cy, rad) = Triple(k[0], k[1], k[2])
        fill.color = col("#141513"); c.drawCircle(cx, cy, rad + 4, fill)
        fill.color = col("#2c2e2a"); c.drawCircle(cx, cy, rad, fill)
        stroke.color = col("#474b43"); stroke.strokeWidth = 2f; c.drawCircle(cx, cy, rad, stroke)
        stroke.color = col("#1a1b18")
        for (i in 0 until 20) {
            val a = 2 * PI * i / 20
            c.drawLine(cx + (rad - 6) * cos(a).toFloat(), cy + (rad - 6) * sin(a).toFloat(),
                cx + rad * cos(a).toFloat(), cy + rad * sin(a).toFloat(), stroke)
        }
        fill.color = col("#33362f"); c.drawCircle(cx, cy, rad * 0.55f, fill)
        val a = Math.toRadians(-225.0 + 270 * frac.coerceIn(0f, 1f))
        stroke.color = col("#f0f0e0"); stroke.strokeWidth = 3f; stroke.strokeCap = Paint.Cap.ROUND
        c.drawLine(cx + rad * 0.2f * cos(a).toFloat(), cy + rad * 0.2f * sin(a).toFloat(),
            cx + (rad - 8) * cos(a).toFloat(), cy + (rad - 8) * sin(a).toFloat(), stroke)
        stroke.strokeCap = Paint.Cap.BUTT
        text(c, label, cx, cy + rad + 11, 9f, col("#8a8f86"))
    }

    // ------------------------------------------------------------ berøring
    private fun hitKey(x: Float, y: Float): String? {
        for ((ri, row) in KEY_ROWS.withIndex()) for ((ci, name) in row.withIndex()) {
            val x1 = 70f + ci * 66
            val y1 = 500f + ri * 66
            if (x in (x1 - 4)..(x1 + 62) && y in (y1 - 4)..(y1 + 60)) return name
        }
        return null
    }

    private fun inKnob(x: Float, y: Float, k: FloatArray): Boolean {
        val dx = x - k[0]; val dy = y - k[1]
        return dx * dx + dy * dy <= (k[2] + 10) * (k[2] + 10)
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(ev: MotionEvent): Boolean {
        val idx = ev.actionIndex
        val x = (ev.getX(idx) - ox) / sc
        val y = (ev.getY(idx) - oy) / sc
        val id = ev.getPointerId(idx)
        when (ev.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN -> {
                when {
                    x < 58f && y in 285f..495f -> {
                        if (pttPointer < 0) {
                            pttPointer = id
                            performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
                            listener?.onPtt(true)
                        }
                    }
                    inKnob(x, y, CH_KNOB) -> {
                        performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                        listener?.onChannelStep(if (x < CH_KNOB[0]) -1 else 1)
                    }
                    inKnob(x, y, VOL_KNOB) -> {
                        performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                        listener?.onVolumeStep(if (x < VOL_KNOB[0]) -1 else 1)
                    }
                    else -> hitKey(x, y)?.let {
                        pressedKey = it
                        performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                        listener?.onKey(it)
                    }
                }
                invalidate()
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP -> {
                if (id == pttPointer) {
                    pttPointer = -1
                    listener?.onPtt(false)
                }
                if (ev.actionMasked == MotionEvent.ACTION_UP) pressedKey = null
                invalidate()
            }
            MotionEvent.ACTION_CANCEL -> {
                if (pttPointer >= 0) { pttPointer = -1; listener?.onPtt(false) }
                pressedKey = null
                invalidate()
            }
        }
        return true
    }

    /** Kaldes hvis PTT slippes udefra (fx når appen går i baggrunden). */
    fun releasePtt() {
        if (pttPointer >= 0) { pttPointer = -1; invalidate() }
    }
}
