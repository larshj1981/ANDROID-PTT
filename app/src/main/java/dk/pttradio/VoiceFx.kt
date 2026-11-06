package dk.pttradio

import java.util.Random
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt
import kotlin.math.tanh

/**
 * Radiolyd på modtagesiden (samme algoritmer som windows/radio_fx.py).
 *  digital – efterligner en MELPe 2400-vocoder (8 kHz, LPC-10, puls/støj-eksitation, støjgate, data-preamble)
 *  fm      – analog FM: 300–3000 Hz, let forvrængning, sus og squelch-hale
 *  clear   – ren lyd med roger-bip
 * NB: lyd-efterligning, ikke den rigtige MELPe-codec. Ind/ud: 16 kHz mono 16-bit PCM (little-endian).
 */
class VoiceFx(mode: String = "digital") {

    companion object {
        const val RATE = 16000
        val MODES = linkedMapOf("digital" to "Digital (MELPe-lignende)", "fm" to "Analog FM", "clear" to "Klar")

        fun lowpass(ntaps: Int, cutoff: Double, rate: Int): DoubleArray {
            val h = DoubleArray(ntaps)
            var sum = 0.0
            for (i in 0 until ntaps) {
                val n = i - (ntaps - 1) / 2.0
                val x = 2 * cutoff / rate * n
                val sinc = if (abs(x) < 1e-12) 1.0 else sin(PI * x) / (PI * x)
                val w = 0.54 - 0.46 * cos(2 * PI * i / (ntaps - 1))
                h[i] = sinc * w
                sum += h[i]
            }
            for (i in h.indices) h[i] /= sum
            return h
        }

        fun bandpass(ntaps: Int, lo: Double, hi: Double, rate: Int): DoubleArray {
            val a = lowpass(ntaps, hi, rate)
            val b = lowpass(ntaps, lo, rate)
            return DoubleArray(ntaps) { a[it] - b[it] }
        }

        fun toDouble(pcm: ByteArray): DoubleArray {
            val n = pcm.size / 2
            return DoubleArray(n) {
                val v = (pcm[2 * it].toInt() and 0xff) or (pcm[2 * it + 1].toInt() shl 8)
                v.toShort() / 32768.0
            }
        }

        fun toPcm(x: DoubleArray): ByteArray {
            val out = ByteArray(x.size * 2)
            for (i in x.indices) {
                val v = (x[i].coerceIn(-1.0, 1.0) * 32767).toInt()
                out[2 * i] = (v and 0xff).toByte()
                out[2 * i + 1] = ((v shr 8) and 0xff).toByte()
            }
            return out
        }

        private fun fade(x: DoubleArray, ms: Int = 4): DoubleArray {
            val n = min(x.size / 2, RATE * ms / 1000)
            for (i in 0 until n) {
                val r = i.toDouble() / n
                x[i] *= r
                x[x.size - 1 - i] *= r
            }
            return x
        }

        private fun tone(freq: Double, ms: Int, vol: Double): DoubleArray =
            fade(DoubleArray(RATE * ms / 1000) { vol * sin(2 * PI * freq * it / RATE) })

        private fun normalize(x: DoubleArray, vol: Double): DoubleArray {
            var peak = 1e-9
            for (v in x) peak = max(peak, abs(v))
            for (i in x.indices) x[i] = x[i] / peak * vol
            return x
        }

        /** Digitalt modem-burst: 8-PSK på 1800 Hz, 2400 baud. */
        private fun dataBurst(ms: Int, vol: Double = 0.16): DoubleArray {
            val rnd = Random()
            val n = RATE * ms / 1000
            val sym = max(1, RATE / 2400)
            var ph = 0.0
            val x = DoubleArray(n) {
                if (it % sym == 0) ph = rnd.nextInt(8) * (PI / 4)
                sin(2 * PI * 1800 * it / RATE + ph)
            }
            return fade(normalize(Fir(bandpass(63, 500.0, 3000.0, RATE)).process(x), vol), 6)
        }

        private fun noiseBurst(ms: Int, vol: Double, decay: Boolean): DoubleArray {
            val rnd = Random()
            val n = RATE * ms / 1000
            val x = Fir(bandpass(63, 400.0, 3800.0, RATE)).process(DoubleArray(n) { rnd.nextGaussian() })
            normalize(x, vol)
            if (decay) for (i in x.indices) x[i] *= (1 - i.toDouble() / n).pow(1.5)
            return fade(x, 3)
        }

        private fun concat(vararg parts: DoubleArray): DoubleArray {
            val out = DoubleArray(parts.sumOf { it.size })
            var p = 0
            for (a in parts) {
                System.arraycopy(a, 0, out, p, a.size)
                p += a.size
            }
            return out
        }
    }

    /** FIR-filter med hukommelse på tværs af blokke (symmetriske koefficienter). */
    class Fir(private val h: DoubleArray) {
        private val m = h.size - 1
        private var z = DoubleArray(m)

        fun process(x: DoubleArray): DoubleArray {
            val xx = DoubleArray(m + x.size)
            System.arraycopy(z, 0, xx, 0, m)
            System.arraycopy(x, 0, xx, m, x.size)
            val y = DoubleArray(x.size)
            for (i in x.indices) {
                var s = 0.0
                for (k in h.indices) s += h[k] * xx[i + k]
                y[i] = s
            }
            z = xx.copyOfRange(xx.size - m, xx.size)
            return y
        }

        fun reset() = z.fill(0.0)
    }

    private interface Engine {
        fun process(x: DoubleArray): DoubleArray
        fun flush(): DoubleArray
        fun reset()
    }

    // ------------------------------------------------------------ MELPe-lignende vocoder
    private class MelpLike : Engine {
        val FRAME = 180
        val HIST = 60
        val ORDER = 10
        val PRE = 0.9375
        val down = Fir(lowpass(63, 3600.0, RATE))
        val up = Fir(lowpass(63, 3600.0, RATE))
        val hp = Fir(bandpass(101, 180.0, 3800.0, RATE))
        val win = DoubleArray(FRAME + HIST - 1) { 0.54 - 0.46 * cos(2 * PI * it / (FRAME + HIST - 2)) }
        val steps = doubleArrayOf(40.0, 32.0, 16.0, 16.0, 12.0, 12.0, 10.0, 10.0, 8.0, 8.0)
        val lagWin = DoubleArray(ORDER + 1) { exp(-0.5 * (2 * PI * 60 / 8000 * it).pow(2)) }
        val rnd = Random()

        var phase = 0
        var buf8 = DoubleArray(0)
        var hist = DoubleArray(HIST)
        val mem = DoubleArray(ORDER)
        var de = 0.0
        var nextPulse = 0
        var prevScale = 0.0
        var prevLag = 0

        override fun reset() {
            down.reset(); up.reset(); hp.reset()
            phase = 0; buf8 = DoubleArray(0); hist = DoubleArray(HIST)
            mem.fill(0.0); de = 0.0; nextPulse = 0; prevScale = 0.0; prevLag = 0
        }

        override fun process(x: DoubleArray): DoubleArray {
            val y = down.process(x)
            val y8 = ArrayList<Double>(y.size / 2 + 1)
            var i = phase
            while (i < y.size) { y8.add(y[i]); i += 2 }
            phase = (phase + y.size) % 2
            val nb = DoubleArray(buf8.size + y8.size)
            System.arraycopy(buf8, 0, nb, 0, buf8.size)
            for (k in y8.indices) nb[buf8.size + k] = y8[k]
            buf8 = nb

            val frames = buf8.size / FRAME
            if (frames == 0) return DoubleArray(0)
            val o8 = DoubleArray(frames * FRAME)
            for (f in 0 until frames) {
                val out = frame(buf8.copyOfRange(f * FRAME, (f + 1) * FRAME))
                System.arraycopy(out, 0, o8, f * FRAME, FRAME)
            }
            buf8 = buf8.copyOfRange(frames * FRAME, buf8.size)
            val upIn = DoubleArray(o8.size * 2)
            for (k in o8.indices) upIn[2 * k] = o8[k]
            val u = up.process(upIn)
            for (k in u.indices) u[k] *= 2.0
            return hp.process(u)
        }

        override fun flush(): DoubleArray {
            val pad = if (buf8.isNotEmpty()) FRAME - buf8.size else 0
            val a = if (pad > 0) process(DoubleArray(pad * 2)) else DoubleArray(0)
            return concat(a, hp.process(up.process(DoubleArray(80))))
        }

        private fun frame(f: DoubleArray): DoubleArray {
            val w = concat(hist, f)
            hist = f.copyOfRange(FRAME - HIST, FRAME)
            var ss = 0.0
            for (v in f) ss += v * v
            val rms = sqrt(ss / FRAME)
            if (rms < 0.0035) {
                prevScale = 0.0
                for (j in mem.indices) mem[j] *= 0.5
                return DoubleArray(FRAME)
            }

            // LPC
            val p = DoubleArray(w.size - 1) { (w[it + 1] - PRE * w[it]) * win[it] }
            val r = DoubleArray(ORDER + 1) { k ->
                var s = 0.0
                for (n in 0 until p.size - k) s += p[n] * p[n + k]
                s
            }
            r[0] = r[0] * 1.0001 + 1e-12
            for (k in r.indices) r[k] *= lagWin[k]
            val kc = levinson(r)
            for (k in kc.indices) kc[k] = ((kc[k] * steps[k]).roundToInt() / steps[k]).coerceIn(-0.97, 0.97)
            val a = stepUp(kc)

            // pitch
            var mean = 0.0
            for (v in w) mean += v
            mean /= w.size
            val x = DoubleArray(w.size) { w[it] - mean }
            var best = 0.0
            var lag = 0
            var e0 = 0.0
            for (v in x) e0 += v * v
            for (L in 20 until 148) {
                var c = 0.0; var e1 = 0.0; var e2 = 0.0
                for (n in 0 until x.size - L) {
                    c += x[n] * x[n + L]; e1 += x[n] * x[n]; e2 += x[n + L] * x[n + L]
                }
                val cn = c / sqrt(e1 * e2 + 1e-12)
                if (cn > best) { best = cn; lag = L }
            }
            val voiced = best > 0.42 && e0 > 1e-6
            if (voiced && prevLag > 0 && abs(lag - prevLag) > 0.25 * prevLag) {
                for (m in 2..3) if (abs(lag.toDouble() / m - prevLag) < 0.15 * prevLag) lag = (lag.toDouble() / m).roundToInt()
            }
            prevLag = if (voiced) lag else 0

            // eksitation
            val e = DoubleArray(FRAME)
            if (voiced) {
                val amp = sqrt(lag.toDouble())
                for (i in 0 until FRAME) {
                    e[i] = 0.22 * rnd.nextGaussian()
                    if (nextPulse <= 0) { e[i] += amp; nextPulse += lag }
                    nextPulse--
                }
            } else {
                for (i in 0 until FRAME) e[i] = rnd.nextGaussian()
                nextPulse = 0
            }

            // syntese
            val y = DoubleArray(FRAME)
            var yy = 0.0
            for (i in 0 until FRAME) {
                var s = e[i]
                for (j in 0 until ORDER) s -= a[j + 1] * mem[j]
                for (j in ORDER - 1 downTo 1) mem[j] = mem[j - 1]
                mem[0] = s
                de = s + PRE * de
                y[i] = de
                yy += de * de
            }

            val q = 10.0.pow((20 * log10(rms) / 2).roundToInt() * 2 / 20.0)
            val scale = q / (sqrt(yy / FRAME) + 1e-9)
            val start = if (prevScale > 0) prevScale else scale
            for (i in 0 until FRAME) {
                val g = start + (scale - start) * i / (FRAME - 1)
                y[i] = tanh(1.3 * y[i] * g) / 1.3
            }
            prevScale = scale
            return y
        }

        private fun levinson(r: DoubleArray): DoubleArray {
            val a = DoubleArray(ORDER + 1); a[0] = 1.0
            val k = DoubleArray(ORDER)
            var err = r[0]
            for (i in 1..ORDER) {
                var acc = r[i]
                for (j in 1 until i) acc += a[j] * r[i - j]
                val ki = -acc / err
                k[i - 1] = ki
                val prev = a.copyOf()
                for (j in 1 until i) a[j] = prev[j] + ki * prev[i - j]
                a[i] = ki
                err *= (1 - ki * ki)
                if (err <= 0) break
            }
            return k
        }

        private fun stepUp(k: DoubleArray): DoubleArray {
            val a = DoubleArray(ORDER + 1); a[0] = 1.0
            for (i in 1..ORDER) {
                val ki = k[i - 1]
                val prev = a.copyOf()
                for (j in 1 until i) a[j] = prev[j] + ki * prev[i - j]
                a[i] = ki
            }
            return a
        }
    }

    // ------------------------------------------------------------ analog FM
    private class Fm : Engine {
        val bp = Fir(bandpass(101, 300.0, 3000.0, RATE))
        val hiss = Fir(bandpass(63, 1500.0, 6000.0, RATE))
        val rnd = Random()

        override fun reset() { bp.reset(); hiss.reset() }

        override fun process(x: DoubleArray): DoubleArray {
            val y = bp.process(x)
            val n = hiss.process(DoubleArray(x.size) { rnd.nextGaussian() })
            for (i in y.indices) y[i] = tanh(1.8 * y[i] * 1.6) / 1.8 + 0.035 * n[i]
            return y
        }

        override fun flush() = DoubleArray(0)
    }

    // ------------------------------------------------------------ offentligt API
    var mode: String = if (mode in MODES) mode else "digital"
        private set
    private val melp = MelpLike()
    private val fm = Fm()

    private fun engine(): Engine? = when (mode) {
        "digital" -> melp
        "fm" -> fm
        else -> null
    }

    @Synchronized
    fun setMode(m: String) {
        if (m in MODES) { mode = m; melp.reset(); fm.reset() }
    }

    @Synchronized
    fun process(pcm: ByteArray): ByteArray {
        val e = engine() ?: return pcm
        val y = e.process(toDouble(pcm))
        return if (y.isEmpty()) ByteArray(0) else toPcm(y)
    }

    /** Når en anden begynder at sende. */
    @Synchronized
    fun startSound(): ByteArray {
        melp.reset(); fm.reset()
        return when (mode) {
            "digital" -> toPcm(concat(dataBurst(240), DoubleArray(RATE * 30 / 1000)))
            "fm" -> toPcm(noiseBurst(35, 0.12, false))
            else -> ByteArray(0)
        }
    }

    /** Når den anden slipper PTT. */
    @Synchronized
    fun endSound(): ByteArray {
        val tail = engine()?.flush() ?: DoubleArray(0)
        return toPcm(
            when (mode) {
                "digital" -> concat(tail, DoubleArray(RATE * 40 / 1000), dataBurst(70, 0.13))
                "fm" -> concat(tail, noiseBurst(170, 0.2, true))
                else -> tone(1400.0, 70, 0.22)
            }
        )
    }
}
