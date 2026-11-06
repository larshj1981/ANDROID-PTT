"""
Radiolyd-effekter til PTT Radio (modtagesiden).

  digital : efterligner lyden af en MELPe 2400-vocoder (som på taktiske digitale radioer):
            8 kHz, LPC-10 spektrum, puls-/støjeksitation, grov kvantisering, støjgate
            + data-preamble når der trykkes og kort burst når der slippes.
  fm      : analog FM-radio: 300–3000 Hz, let forvrængning, sus og squelch-hale.
  clear   : ren lyd (som før) med roger-bip.

NB: Det er en lyd-efterligning – ikke den rigtige (licenserede) MELPe-codec.
Alle funktioner tager/returnerer 16 kHz mono 16-bit PCM som bytes.
"""
import math

import numpy as np

RATE = 16000
MODES = {"digital": "Digital (MELPe-lignende)", "fm": "Analog FM", "clear": "Klar"}


# ------------------------------------------------------------------ små byggesten
def _lowpass(ntaps, cutoff, rate):
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * cutoff / rate * n) * np.hamming(ntaps)
    return h / h.sum()


def _bandpass(ntaps, lo, hi, rate):
    return _lowpass(ntaps, hi, rate) - _lowpass(ntaps, lo, rate)


class _FIR:
    """FIR-filter med hukommelse på tværs af blokke."""

    def __init__(self, h):
        self.h = h
        self.z = np.zeros(len(h) - 1)

    def __call__(self, x):
        xx = np.concatenate([self.z, x])
        self.z = xx[len(xx) - (len(self.h) - 1):]
        return np.convolve(xx, self.h, mode="valid")

    def reset(self):
        self.z[:] = 0


def _to_float(pcm):
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0


def _to_pcm(x):
    return (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _fade(x, ms=4):
    n = min(len(x) // 2, int(RATE * ms / 1000))
    if n > 0:
        r = np.linspace(0, 1, n)
        x[:n] *= r
        x[-n:] *= r[::-1]
    return x


def _tone(freq, ms, vol):
    t = np.arange(int(RATE * ms / 1000)) / RATE
    return _fade(vol * np.sin(2 * math.pi * freq * t))


def _data_burst(ms, vol=0.16, seed=None):
    """Lyden af et digitalt modem-burst (PSK på 1800 Hz bærebølge, 2400 baud)."""
    rng = np.random.default_rng(seed)
    n = int(RATE * ms / 1000)
    sym = max(1, RATE // 2400)
    phases = rng.integers(0, 8, size=n // sym + 1) * (math.pi / 4)  # 8-PSK
    ph = np.repeat(phases, sym)[:n]
    t = np.arange(n) / RATE
    x = np.sin(2 * math.pi * 1800 * t + ph)
    x = _FIR(_bandpass(63, 500, 3000, RATE))(x)
    return _fade(vol * x / (np.max(np.abs(x)) + 1e-9), 6)


def _noise_burst(ms, vol, decay=True, seed=None):
    rng = np.random.default_rng(seed)
    n = int(RATE * ms / 1000)
    x = rng.standard_normal(n)
    x = _FIR(_bandpass(63, 400, 3800, RATE))(x)
    x = x / (np.max(np.abs(x)) + 1e-9) * vol
    if decay:
        x *= np.linspace(1, 0, n) ** 1.5
    return _fade(x, 3)


# ------------------------------------------------------------------ MELPe-lignende vocoder
class _MelpLike:
    FRAME = 180      # 22,5 ms ved 8 kHz (som MELPe)
    HIST = 60
    ORDER = 10
    PRE = 0.9375

    def __init__(self):
        self.down = _FIR(_lowpass(63, 3600, RATE))
        self.up = _FIR(_lowpass(63, 3600, RATE))
        self.hp = _FIR(_bandpass(101, 180, 3800, RATE))
        self.win = np.hamming(self.FRAME + self.HIST - 1)
        self.rng = np.random.default_rng()
        self.reset()

    def reset(self):
        self.down.reset()
        self.up.reset()
        self.hp.reset()
        self.phase = 0
        self.buf8 = np.zeros(0)
        self.hist = np.zeros(self.HIST)
        self.mem = [0.0] * self.ORDER
        self.de = 0.0
        self.next_pulse = 0
        self.prev_scale = 0.0
        self.prev_lag = 0

    def process(self, x16):
        y = self.down(x16)
        y8 = y[self.phase::2]
        self.phase = (self.phase + len(y)) % 2
        self.buf8 = np.concatenate([self.buf8, y8])
        out = []
        while len(self.buf8) >= self.FRAME:
            f = self.buf8[:self.FRAME]
            self.buf8 = self.buf8[self.FRAME:]
            out.append(self._frame(f))
        if not out:
            return np.zeros(0)
        o8 = np.concatenate(out)
        up = np.zeros(len(o8) * 2)
        up[::2] = o8
        return self.hp(2.0 * self.up(up))

    def flush(self):
        pad = self.FRAME - len(self.buf8) if len(self.buf8) else 0
        y = self.process(np.zeros(pad * 2)) if pad else np.zeros(0)
        return np.concatenate([y, self.hp(self.up(np.zeros(80)))])

    # -- én ramme: analyse -> kvantisering -> syntese
    def _frame(self, f):
        w = np.concatenate([self.hist, f])
        self.hist = f[-self.HIST:].copy()
        rms = math.sqrt(float(np.mean(f * f)))

        if rms < 0.0035:  # støjgate: digital radio er helt stille mellem ordene
            self.prev_scale = 0.0
            self.mem = [m * 0.5 for m in self.mem]
            return np.zeros(self.FRAME)

        # LPC (autokorrelation + Levinson) på præ-emphasiseret, vinduet signal
        p = (w[1:] - self.PRE * w[:-1]) * self.win
        r = np.array([np.dot(p[:len(p) - k], p[k:]) for k in range(self.ORDER + 1)])
        r[0] = r[0] * 1.0001 + 1e-12
        r *= np.exp(-0.5 * (2 * math.pi * 60 / 8000 * np.arange(self.ORDER + 1)) ** 2)  # båndbredde-udvidelse
        k = self._levinson(r)
        # grov kvantisering af refleksionskoefficienter (giver "vocoder"-præget)
        steps = np.array([40, 32, 16, 16, 12, 12, 10, 10, 8, 8], dtype=float)
        k = np.clip(np.round(k * steps) / steps, -0.97, 0.97)
        a = self._step_up(k)

        # tonehøjde (pitch) og stemthed
        x = w - np.mean(w)
        best, lag = 0.0, 0
        e0 = np.dot(x, x) + 1e-12
        for L in range(20, 148):  # 54–400 Hz
            c = np.dot(x[:-L], x[L:]) / math.sqrt(np.dot(x[:-L], x[:-L]) * np.dot(x[L:], x[L:]) + 1e-12)
            if c > best:
                best, lag = c, L
        voiced = best > 0.42 and e0 > 1e-6
        if voiced and self.prev_lag and abs(lag - self.prev_lag) > 0.25 * self.prev_lag:
            for m in (2, 3):  # ret oktavfejl
                if abs(lag / m - self.prev_lag) < 0.15 * self.prev_lag:
                    lag = int(round(lag / m))
        self.prev_lag = lag if voiced else 0

        # eksitation: pulstog (+ lidt støj) eller ren støj
        n = self.FRAME
        if voiced:
            lag = int(round(lag * 2) / 2) or lag  # kvantiseret pitch
            e = 0.22 * self.rng.standard_normal(n)
            for i in range(n):
                if self.next_pulse <= 0:
                    e[i] += math.sqrt(lag)
                    self.next_pulse += lag
                self.next_pulse -= 1
        else:
            e = self.rng.standard_normal(n)
            self.next_pulse = 0

        # syntese 1/A(z) + de-emphasis
        a1 = [float(v) for v in a[1:]]
        mem = self.mem
        de, pre = self.de, self.PRE
        y = np.empty(n)
        for i in range(n):
            s = float(e[i])
            for j in range(self.ORDER):
                s -= a1[j] * mem[j]
            mem.insert(0, s)
            mem.pop()
            de = s + pre * de
            y[i] = de
        self.mem, self.de = mem, de

        # forstærkning: kvantiseret i 2 dB trin, glidende overgang
        q = 10 ** (round(20 * math.log10(rms) / 2) * 2 / 20)
        scale = q / (math.sqrt(float(np.mean(y * y))) + 1e-9)
        ramp = np.linspace(self.prev_scale if self.prev_scale else scale, scale, n)
        self.prev_scale = scale
        return np.tanh(1.3 * y * ramp) / 1.3

    def _levinson(self, r):
        order = self.ORDER
        a = np.zeros(order + 1)
        a[0] = 1.0
        k = np.zeros(order)
        err = r[0]
        for i in range(1, order + 1):
            acc = r[i] + np.dot(a[1:i], r[i - 1:0:-1])
            ki = -acc / err
            k[i - 1] = ki
            a_prev = a.copy()
            for j in range(1, i):
                a[j] = a_prev[j] + ki * a_prev[i - j]
            a[i] = ki
            err *= (1 - ki * ki)
            if err <= 0:
                break
        return k

    def _step_up(self, k):
        a = np.zeros(self.ORDER + 1)
        a[0] = 1.0
        for i, ki in enumerate(k, start=1):
            a_prev = a.copy()
            for j in range(1, i):
                a[j] = a_prev[j] + ki * a_prev[i - j]
            a[i] = ki
        return a


# ------------------------------------------------------------------ analog FM
class _FM:
    def __init__(self):
        self.bp = _FIR(_bandpass(101, 300, 3000, RATE))
        self.hiss = _FIR(_bandpass(63, 1500, 6000, RATE))
        self.rng = np.random.default_rng()

    def reset(self):
        self.bp.reset()
        self.hiss.reset()

    def process(self, x):
        y = self.bp(x) * 1.6
        y = np.tanh(1.8 * y) / 1.8
        y += 0.035 * self.hiss(self.rng.standard_normal(len(x)))
        return y

    def flush(self):
        return np.zeros(0)


# ------------------------------------------------------------------ offentlig klasse
class VoiceFX:
    def __init__(self, mode="digital"):
        self.mode = mode if mode in MODES else "digital"
        self._melp = _MelpLike()
        self._fm = _FM()

    def set_mode(self, mode):
        if mode in MODES:
            self.mode = mode
            self.reset()

    def reset(self):
        self._melp.reset()
        self._fm.reset()

    def _engine(self):
        return {"digital": self._melp, "fm": self._fm}.get(self.mode)

    def process(self, pcm):
        eng = self._engine()
        if eng is None:
            return pcm
        y = eng.process(_to_float(pcm))
        return _to_pcm(y) if len(y) else b""

    def start_sound(self):
        """Spilles når en anden begynder at sende."""
        self.reset()
        if self.mode == "digital":
            return _to_pcm(np.concatenate([_data_burst(240), np.zeros(int(RATE * 0.03))]))
        if self.mode == "fm":
            return _to_pcm(_noise_burst(35, 0.12, decay=False))
        return b""

    def end_sound(self):
        """Spilles når den anden slipper PTT."""
        eng = self._engine()
        tail = eng.flush() if eng else np.zeros(0)
        if self.mode == "digital":
            s = np.concatenate([tail, np.zeros(int(RATE * 0.04)), _data_burst(70, 0.13)])
        elif self.mode == "fm":
            s = np.concatenate([tail, _noise_burst(170, 0.2)])
        else:
            s = _tone(1400, 70, 0.22)  # roger-bip
        return _to_pcm(s)
