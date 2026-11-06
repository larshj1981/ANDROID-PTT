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
import kotlin.math.min

/**
 * Radioens skærm (layout efter skitsen):
 *   display (kanal og navn) øverst · stor PTT til venstre · MUTE og kanal op/ned til højre
 *   · indstillinger · lydstyrke op/ned nederst.
 */
class RadioView(ctx: Context) : View(ctx) {

    interface Listener {
        fun onPtt(down: Boolean)
        fun onMute()
        fun onChannelStep(d: Int)
        fun onVolumeStep(d: Int)
        fun onSettings()
        fun onDisplay()
    }

    data class State(
        val power: Boolean = false,
        val online: Boolean = false,
        val tx: Boolean = false,
        val held: Boolean = false,
        val backlight: Int = 0,          // 0 slukket, 1 dæmpet, 2 tændt
        val txrx: String = "",
        val mode: String = "",
        val muted: Boolean = false,
        val clock: String = "",
        val big: String = "OFF",
        val name: String = "",
        val status: String = "",
        val level: Float = 0f,
        val showLevel: Boolean = false,
        val volume: Int = 5,             // 0–10
    )

    var listener: Listener? = null
    var state = State()
        set(v) {
            if (v != field) { field = v; invalidate() }
        }

    companion object {
        const val W = 400f
        const val H = 800f
        private val DISPLAY = RectF(24f, 24f, 376f, 264f)
        private val PTT = RectF(24f, 284f, 256f, 604f)
        private val MUTE = RectF(272f, 284f, 376f, 392f)
        private val UP = RectF(272f, 404f, 376f, 500f)
        private val DOWN = RectF(272f, 508f, 376f, 604f)
        private val SETTINGS = RectF(24f, 624f, 376f, 700f)
        private val VDOWN = RectF(24f, 712f, 112f, 784f)
        private val VBAR = RectF(120f, 712f, 280f, 784f)
        private val VUP = RectF(288f, 712f, 376f, 784f)
        private const val INK = 0xFF16200E.toInt()
    }

    private var sc = 1f
    private var ox = 0f
    private var oy = 0f
    private var pressed: String? = null
    private var pttPointer = -1

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
    private val txt = Paint(Paint.ANTI_ALIAS_FLAG).apply { textAlign = Paint.Align.CENTER }
    private val mono = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
    private val sans = Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD)
    private val tmp = RectF()

    init {
        setBackgroundColor(Color.parseColor("#121412"))
        isHapticFeedbackEnabled = true
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        sc = min(w / W, h / H)
        ox = (w - W * sc) / 2
        oy = (h - H * sc) / 2
    }

    private fun col(s: String) = Color.parseColor(s)

    private fun box(c: Canvas, r: RectF, color: Int, edge: Int, rad: Float = 14f, ew: Float = 2f) {
        fill.color = color
        c.drawRoundRect(r, rad, rad, fill)
        stroke.color = edge
        stroke.strokeWidth = ew
        c.drawRoundRect(r, rad, rad, stroke)
    }

    private fun text(c: Canvas, s: String, x: Float, y: Float, size: Float, color: Int, tf: Typeface = sans) {
        txt.textSize = size
        txt.color = color
        txt.typeface = tf
        c.drawText(s, x, y + size * 0.36f, txt)
    }

    private fun button(c: Canvas, id: String, r: RectF, label: String, size: Float, color: Int = col("#e8e8e0"),
                       bg: Int = col("#3b3f38")) {
        box(c, r, if (pressed == id) col("#1f221d") else bg, col("#4f554a"))
        text(c, label, r.centerX(), r.centerY(), size, color)
    }

    override fun onDraw(canvas: Canvas) {
        val st = state
        val c = canvas
        c.save()
        c.translate(ox, oy)
        c.scale(sc, sc)

        tmp.set(8f, 8f, W - 8, H - 8)
        box(c, tmp, col("#2b2e29"), col("#3d4239"), 26f, 3f)

        // ---- display
        tmp.set(DISPLAY.left - 4, DISPLAY.top - 4, DISPLAY.right + 4, DISPLAY.bottom + 4)
        box(c, tmp, col("#121311"), col("#3a3e36"), 12f)
        fill.color = when (st.backlight) { 2 -> col("#a9c98a"); 1 -> col("#7c8f6a"); else -> col("#2f362b") }
        c.drawRect(DISPLAY.left + 8, DISPLAY.top + 8, DISPLAY.right - 8, DISPLAY.bottom - 8, fill)
        val ink = if (st.power) INK else col("#5a6a50")
        if (st.power) {
            fill.color = ink
            if (st.online) for (i in 0 until 4) c.drawRect(44f + i * 8, 54f - (i + 1) * 4, 50f + i * 8, 54f, fill)
            text(c, st.txrx, 112f, 46f, 15f, ink, mono)
            text(c, st.mode, 168f, 46f, 14f, ink, mono)
            text(c, if (st.muted) "MUTE" else "", 236f, 46f, 14f, ink, mono)
            text(c, st.clock, 330f, 46f, 14f, ink, mono)
        }
        stroke.color = ink; stroke.strokeWidth = 1.5f
        c.drawLine(40f, 64f, 360f, 64f, stroke)
        text(c, st.big, 200f, 118f, 52f, ink, mono)
        text(c, st.name, 200f, 178f, 22f, ink, mono)
        if (st.showLevel) {
            text(c, "TX", 110f, 226f, 17f, ink, mono)
            val n = min(12, (st.level * 70 + 0.5f).toInt())
            fill.color = ink
            for (i in 0 until n) c.drawRect(140f + i * 16, 216f, 152f + i * 16, 236f, fill)
        } else {
            text(c, st.status, 200f, 226f, 17f, ink, mono)
        }

        // ---- PTT
        val pttColor = when { st.tx -> col("#b8202f"); st.held -> col("#8a5a00"); else -> col("#3b3f38") }
        box(c, PTT, if (pttPointer >= 0 && !st.tx && !st.held) col("#1f221d") else pttColor, col("#4f554a"), 18f, 3f)
        text(c, "PTT", PTT.centerX(), PTT.centerY() - 12, 46f, col("#e8e8e0"))
        text(c, when { st.tx -> "SENDER"; st.held -> "VENTER"; else -> "hold inde" }, PTT.centerX(), PTT.centerY() + 40, 15f, col("#b0b4aa"))

        // ---- mute og kanal
        button(c, "mute", MUTE, if (st.muted) "MUTE ●" else "MUTE", 18f,
            if (st.muted) col("#e0b050") else col("#e8e8e0"), if (st.muted) col("#6a5418") else col("#3b3f38"))
        button(c, "up", UP, "▲", 30f)
        text(c, "KANAL", UP.centerX(), UP.bottom - 14, 11f, col("#8a8f86"))
        button(c, "down", DOWN, "▼", 30f)
        text(c, "KANAL", DOWN.centerX(), DOWN.bottom - 14, 11f, col("#8a8f86"))

        // ---- indstillinger og lydstyrke
        button(c, "settings", SETTINGS, "⚙  INDSTILLINGER", 20f, col("#e0b050"))
        button(c, "vdown", VDOWN, "−", 34f)
        button(c, "vup", VUP, "+", 34f)
        box(c, VBAR, col("#1c1e1a"), col("#3a3e36"), 10f)
        text(c, "VOL", VBAR.left + 26, VBAR.centerY(), 14f, col("#8a8f86"))
        for (i in 0 until 10) {
            val x = VBAR.left + 50 + i * 10.5f
            tmp.set(x, VBAR.centerY() - 12, x + 7, VBAR.centerY() + 12)
            if (i < st.volume) { fill.color = col("#a9c98a"); c.drawRect(tmp, fill) }
            stroke.color = col("#3a3e36"); stroke.strokeWidth = 1f; c.drawRect(tmp, stroke)
        }
        c.restore()
    }

    // ------------------------------------------------------------ berøring
    private fun hit(x: Float, y: Float): String? = when {
        PTT.contains(x, y) -> "ptt"
        MUTE.contains(x, y) -> "mute"
        UP.contains(x, y) -> "up"
        DOWN.contains(x, y) -> "down"
        SETTINGS.contains(x, y) -> "settings"
        VDOWN.contains(x, y) -> "vdown"
        VUP.contains(x, y) -> "vup"
        DISPLAY.contains(x, y) -> "display"
        else -> null
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(ev: MotionEvent): Boolean {
        val idx = ev.actionIndex
        val x = (ev.getX(idx) - ox) / sc
        val y = (ev.getY(idx) - oy) / sc
        val id = ev.getPointerId(idx)
        when (ev.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN -> {
                when (val h = hit(x, y)) {
                    "ptt" -> if (pttPointer < 0) {
                        pttPointer = id
                        performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
                        listener?.onPtt(true)
                    }
                    null -> Unit
                    else -> {
                        pressed = h
                        performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                        when (h) {
                            "mute" -> listener?.onMute()
                            "up" -> listener?.onChannelStep(1)
                            "down" -> listener?.onChannelStep(-1)
                            "vdown" -> listener?.onVolumeStep(-1)
                            "vup" -> listener?.onVolumeStep(1)
                        }
                    }
                }
                invalidate()
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP -> {
                if (id == pttPointer) {
                    pttPointer = -1
                    listener?.onPtt(false)
                }
                if (ev.actionMasked == MotionEvent.ACTION_UP) {
                    // indstillinger/display åbnes ved slip, så dialogen ikke "stjæler" berøringen
                    when (pressed) {
                        "settings" -> if (hit(x, y) == "settings") listener?.onSettings()
                        "display" -> if (hit(x, y) == "display") listener?.onDisplay()
                    }
                    pressed = null
                }
                invalidate()
            }
            MotionEvent.ACTION_CANCEL -> {
                if (pttPointer >= 0) { pttPointer = -1; listener?.onPtt(false) }
                pressed = null
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
