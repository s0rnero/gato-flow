"""
Seguidor de ritmo del audio del sistema para sincronizar el baile del gato.

FLUJO: captura loopback en estereo (WASAPI en Windows, BlackHole en macOS via
soundcard; nunca el microfono) en bloques de HOP muestras con marca de tiempo
monotonic, hacia un detector en streaming. Cada bloque produce un frame STFT
(ventana Hann de WIN) agrupado en bandas logaritmicas; la funcion de onsets es
el flujo espectral en dB con piso adaptativo, asi que no depende del volumen
(el loopback de Windows llega ya afectado por el volumen maestro). Las bandas
graves y medias (bombo, caja, bajo) pesan mas que las agudas (hi-hats), que
solo marcan subdivisiones y confunden la octava.

TEMPO: autocorrelacion de la funcion de onsets de los ultimos ACF_SEC segundos
sin tendencia, puntuada con la suma de armonicos (lag, 2lag, 3lag, lag/2) y un
prior log-normal centrado en PRIOR_BPM para elegir la octava; interpolacion
parabolica para fraccion de frame. La confianza es la suma armonica del pico;
para enganchar el candidato debe repetirse STABLE_N actualizaciones seguidas
(voz o ambient sin pulso no lo logran). Derivas chicas se siguen al instante y
un tempo lejano exige la misma estabilidad; si la confianza cae varias veces el
tempo se suelta.

FASE: peine de COMB_BEATS pulsos separados por el periodo sobre la funcion de
onsets reciente; el desplazamiento que maximiza la suma ubica el ultimo beat y
con el periodo se predice cualquier beat futuro (reloj: t0 + n * periodo). El
reloj se corrige a medias hacia cada medicion para no saltar con ruido.

ESTADO: get_state() entrega bpm, periodo, t0 del reloj (monotonic), confianza,
nivel RMS y lock; el silencio o la falta de datos apagan el lock y vacian el
historial para que la siguiente cancion arranque limpia. El hilo reabre la
captura si cambia el altavoz por defecto o si falla el dispositivo.
"""
import math
import sys
import threading
import time
from collections import deque

import numpy as np

SR = 48000
HOP = 512
WIN = 2048
FR = SR / HOP

BPM_LO, BPM_HI = 60.0, 200.0
PRIOR_BPM, PRIOR_OCT = 100.0, 0.7
ACF_SEC = 8.0
ACF_DETREND_SEC = 4.0
MIN_SEC = 3.0
DETREND_SEC = 1.0
COMB_BEATS = 8
COMB_DECAY = 0.85
CLARITY_BEATS = 16
CLAR_LO, CLAR_SPAN = 4.0, 4.0
LM_TWO, LM_HI = 0, 1
TEMPO_EVERY = int(round(0.5 * FR))
PHASE_EVERY = int(round(0.25 * FR))
STABLE_TOL = 0.05
STABLE_N = 3
SWITCH_TOL = 0.08
CONF_LOCK = 0.4
CONF_DROP = 0.25
WEAK_N = 3
SILENCE_RMS = 5e-4
SILENCE_HOLD = 2.0
STALE_SEC = 1.0
CAPTURE_BUF_SEC = 0.25
DB_FLOOR = 70.0
BAND_LO, BAND_HI, N_BANDS = 40.0, 8000.0, 32
BAND_W = ((200.0, 1.5), (3000.0, 1.0), (1e9, 0.5))
LATENCY = 0.0


def _moving_mean(x, n):
    n = max(1, min(int(n), len(x)))
    c = np.cumsum(np.concatenate(([0.0], x)))
    i = np.arange(len(x))
    a = np.maximum(0, i - n + 1)
    return (c[i + 1] - c[a]) / (i + 1 - a)


def _local_max(a, r):
    out = a.copy()
    for s in range(1, r + 1):
        out[s:] = np.maximum(out[s:], a[:-s])
        out[:-s] = np.maximum(out[:-s], a[s:])
    return out


def _parabolic(y, i):
    if i <= 0 or i >= len(y) - 1:
        return float(i), float(y[i])
    a, b, c = y[i - 1], y[i], y[i + 1]
    d = a - 2 * b + c
    if abs(d) < 1e-12:
        return float(i), float(b)
    off = max(-1.0, min(1.0, 0.5 * (a - c) / d))
    return i + off, b - 0.25 * (a - c) * off


class RhythmFollower:

    def __init__(self):
        self._buf = np.zeros(0, dtype=np.float32)
        self._win = np.hanning(WIN).astype(np.float32)
        freqs = np.fft.rfftfreq(WIN, 1.0 / SR)
        edges = np.geomspace(BAND_LO, BAND_HI, N_BANDS + 1)
        bins = np.unique(np.searchsorted(freqs, edges))
        M = np.zeros((len(bins) - 1, len(freqs)), dtype=np.float32)
        w = np.zeros(len(bins) - 1, dtype=np.float64)
        for b in range(len(bins) - 1):
            M[b, bins[b]:bins[b + 1]] = 1.0 / (bins[b + 1] - bins[b])
            fc = math.sqrt(freqs[bins[b]] * freqs[bins[b + 1] - 1]) if bins[b + 1] - 1 > bins[b] else freqs[bins[b]]
            w[b] = next(wt for lim, wt in BAND_W if fc < lim)
        self._M = M
        self._bandw = w / w.sum()
        self._prev_db = None
        self._gmax = -120.0
        self._odf = np.zeros(int(ACF_SEC * FR) + int(2 * FR), dtype=np.float32)
        self._n = 0
        self._since_reset = 0
        self._total = 0
        self._t_end = None
        self._rms = deque(maxlen=int(FR))
        self.level = 0.0
        self.bpm = 0.0
        self.period = 0.0
        self.conf = 0.0
        self.clarity = 0.0
        self.t0 = None
        self.locked = False
        self._cand = 0.0
        self._cand_n = 0
        self._weak = 0
        self._silent_since = None
        lag_min = int(math.floor(60.0 * FR / BPM_HI))
        lag_max = int(math.ceil(60.0 * FR / BPM_LO))
        self._lags = np.arange(lag_min, lag_max + 1)
        bpms = 60.0 * FR / self._lags
        self._prior = np.exp(-0.5 * (np.log2(bpms / PRIOR_BPM) / PRIOR_OCT) ** 2)

    def feed(self, x, t_end):
        x = np.asarray(x, dtype=np.float32).ravel()
        if x.size == 0:
            return
        self._t_end = t_end
        self._total += x.size
        self.level = float(np.sqrt(np.mean(x * x)))
        self._rms.append(self.level)
        self._buf = np.concatenate((self._buf, x))
        while self._buf.size >= WIN:
            frame = self._buf[:WIN]
            self._buf = self._buf[HOP:]
            self._step(frame)
        self._check_silence()

    def _frame_time(self, k):
        center = k * HOP + WIN / 2.0
        return self._t_end - (self._total - center) / SR

    def _step(self, frame):
        spec = np.fft.rfft(frame * self._win)
        p = spec.real ** 2 + spec.imag ** 2
        db = 10.0 * np.log10(self._M @ p + 1e-12)
        self._gmax = max(float(db.max()), self._gmax - 0.05)
        db = np.maximum(db, self._gmax - DB_FLOOR)
        if self._prev_db is None:
            flux = 0.0
        else:
            flux = float(np.dot(np.maximum(db - self._prev_db, 0.0), self._bandw))
        self._prev_db = db
        self._odf = np.roll(self._odf, -1)
        self._odf[-1] = flux
        self._n += 1
        self._since_reset += 1
        if self._n % TEMPO_EVERY == 0:
            self._update_tempo()
        if self.bpm > 0 and self._n % PHASE_EVERY == 0:
            self._update_phase()

    def _check_silence(self):
        loud = max(self._rms) if self._rms else 0.0
        now = self._t_end
        if loud < SILENCE_RMS:
            if self._silent_since is None:
                self._silent_since = now
            elif now - self._silent_since > SILENCE_HOLD and self._since_reset > 0:
                self._reset_tempo(clear=True)
        else:
            self._silent_since = None

    def _reset_tempo(self, clear=False):
        self.bpm = 0.0
        self.period = 0.0
        self.conf = 0.0
        self.t0 = None
        self.locked = False
        self._cand = 0.0
        self._cand_n = 0
        self._weak = 0
        if clear:
            self._odf[:] = 0.0
            self._since_reset = 0

    def _recent(self, n):
        n = min(n, self._since_reset, len(self._odf))
        return self._odf[len(self._odf) - n:]

    def _update_tempo(self):
        if self._since_reset < MIN_SEC * FR:
            return
        x = self._recent(int(ACF_SEC * FR)).astype(np.float64)
        x = x - _moving_mean(x, ACF_DETREND_SEC * FR)
        if np.std(x) < 1e-9:
            self._weaken()
            return
        L = len(x)
        f = np.fft.rfft(x, 2 * L)
        acf = np.fft.irfft(f * np.conj(f))[:L]
        acf = acf / (acf[0] + 1e-12)
        m1 = _local_max(acf, LM_TWO) if LM_TWO > 0 else acf
        m2 = _local_max(acf, LM_HI) if LM_HI > 0 else acf
        lags = self._lags[self._lags < L // 3]
        half = np.interp(lags / 2.0, np.arange(L), acf)
        two = m1[np.minimum(2 * lags, L - 1)]
        three = m2[np.minimum(3 * lags, L - 1)]
        four = np.where(4 * lags < 0.6 * L, m2[np.minimum(4 * lags, L - 1)], 0.0)
        hsum = acf[lags] + 0.5 * two + 0.33 * three + 0.5 * half + 0.5 * four
        i0 = int(np.argmax(hsum))
        best, best_j = -1.0, i0
        for l in (lags[i0] / 2.0, float(lags[i0]), 2.0 * lags[i0]):
            if l < lags[0] or l > lags[-1]:
                continue
            li = int(round(l)) - lags[0]
            lo, hi = max(0, li - 2), min(len(hsum), li + 3)
            j = lo + int(np.argmax(hsum[lo:hi]))
            if hsum[j] * self._prior[j] > best:
                best, best_j = hsum[j] * self._prior[j], j
        li, hpk = _parabolic(hsum, best_j)
        lag = lags[0] + li
        cand = 60.0 * FR / lag
        S = self._comb(lag, CLARITY_BEATS, 1.0)
        self.clarity = float(S.max() / (S.mean() + 1e-12)) if S is not None else 0.0
        conf = min(1.0, max(0.0, hpk) / 0.6) * max(0.0, min(1.0, (self.clarity - CLAR_LO) / CLAR_SPAN))
        if self._cand > 0 and abs(cand / self._cand - 1.0) < STABLE_TOL:
            self._cand_n += 1
        else:
            self._cand, self._cand_n = cand, 1
        self.conf = 0.5 * self.conf + 0.5 * conf
        stable = self._cand_n >= STABLE_N and self.conf >= CONF_LOCK
        if self.bpm <= 0:
            if stable:
                self.bpm = cand
                self.t0 = None
                self._weak = 0
        elif abs(cand / self.bpm - 1.0) < SWITCH_TOL:
            self.bpm = 0.6 * self.bpm + 0.4 * cand
        elif stable:
            self.bpm = cand
            self.t0 = None
        if self.bpm > 0:
            self.period = 60.0 / self.bpm
            if self.conf < CONF_DROP:
                self._weaken()
            else:
                self._weak = 0
        self.locked = self.bpm > 0 and self.t0 is not None and self.conf >= CONF_DROP

    def _weaken(self):
        self._weak += 1
        self.locked = False
        if self._weak >= WEAK_N:
            self._reset_tempo()

    def _comb(self, P, K, decay):
        need = int(math.ceil(K * P + P)) + 2
        y = self._recent(need).astype(np.float64)
        if len(y) < 2 * P + 2:
            return None
        y = np.maximum(y - _moving_mean(y, DETREND_SEC * FR), 0.0)
        k = min(K, int((len(y) - 1) / P))
        now = len(y) - 1
        phis = np.arange(int(math.ceil(P)))
        ks = np.arange(k)
        idx = np.clip(now - phis[:, None] - ks[None, :] * P, 0, now)
        vals = np.interp(idx.ravel(), np.arange(len(y)), y).reshape(idx.shape)
        return (vals * (decay ** ks)[None, :]).sum(axis=1)

    def _update_phase(self):
        P = FR * self.period
        S = self._comb(P, COMB_BEATS, COMB_DECAY)
        if S is None:
            return
        j = int(np.argmax(S))
        phi, top = _parabolic(S, j)
        if top <= 0:
            return
        t_beat = self._frame_time(self._n - 1) - phi / FR - LATENCY
        if self.t0 is None:
            self.t0 = t_beat
        else:
            e = ((t_beat - self.t0) / self.period + 0.5) % 1.0 - 0.5
            self.t0 += 0.5 * e * self.period
        self.t0 += math.floor((self._t_end - self.t0) / self.period) * self.period
        self.locked = self.conf >= CONF_DROP

    def next_beat(self, t):
        if self.t0 is None or self.period <= 0:
            return None
        return self.t0 + math.ceil((t - self.t0) / self.period) * self.period

    def diag(self):
        return {"bpm": round(self.bpm, 1), "conf": round(self.conf, 2),
                "lock": self.locked, "lvl": round(self.level, 4)}


def pick_loopback_device():
    import soundcard as sc
    want = "blackhole" if sys.platform == "darwin" else None
    try:
        base = {m.name for m in sc.all_microphones(include_loopback=False)}
        cands = [m for m in sc.all_microphones(include_loopback=True)
                 if m.name not in base]
    except Exception as e:
        return None, f"no pude listar audio: {e}"
    if want:
        cands = [m for m in cands if want in m.name.lower()] or\
                [m for m in sc.all_microphones(include_loopback=True)
                 if want in m.name.lower()]
        if not cands:
            return None, ("instala BlackHole (gratuito) y elígelo como salida "
                          "para que AUTO pueda escuchar la música")
    if not cands:
        return None, "no hay dispositivo loopback (revisa tu salida de audio)"
    try:
        spk = sc.default_speaker().name
        for m in cands:
            if m.name == spk:
                return m, ""
    except Exception:
        pass
    return cands[0], ""


class BeatTracker(threading.Thread):

    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._stop_ev = threading.Event()
        self._state = {"ok": False, "error": "sin iniciar", "device": "",
                       "level": 0.0, "bpm": 0.0, "period": 0.0, "t0": None,
                       "conf": 0.0, "clarity": 0.0, "locked": False, "updated": 0.0}

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    def run(self):
        try:
            import soundcard as sc
        except Exception as e:
            self._set(ok=False, error=f"falta libreria de audio: {e}")
            return
        while not self._stop_ev.is_set():
            mic, err = pick_loopback_device()
            if mic is None:
                self._set(ok=False, error=err, locked=False)
                self._stop_ev.wait(5.0)
                continue
            try:
                self._capture(sc, mic)
            except Exception as e:
                self._set(ok=False, locked=False,
                          error=f"no pude abrir loopback ({mic.name}): {e}")
                self._stop_ev.wait(3.0)

    def _capture(self, sc, mic):
        det = RhythmFollower()
        last_check = time.monotonic()
        t_end = None
        with mic.recorder(samplerate=SR, channels=[0, 1],
                          blocksize=int(SR * CAPTURE_BUF_SEC)) as rec:
            self._set(ok=True, error="", device=mic.name)
            while not self._stop_ev.is_set():
                data = rec.record(numframes=HOP)
                t_ret = time.monotonic()
                mono = np.asarray(data, dtype=np.float32).mean(axis=1)
                t_end = t_ret if t_end is None else min(t_ret, t_end + len(mono) / SR)
                det.feed(mono, t_end)
                self._set(level=det.level, bpm=det.bpm, period=det.period,
                          t0=det.t0, conf=det.conf, clarity=det.clarity,
                          locked=det.locked, updated=t_ret)
                if t_ret - last_check > 3.0:
                    last_check = t_ret
                    if sys.platform != "darwin":
                        try:
                            if sc.default_speaker().name != mic.name:
                                return
                        except Exception:
                            pass

    def stop(self):
        self._stop_ev.set()

    def get_state(self):
        with self._lock:
            st = dict(self._state)
        if st["updated"] and time.monotonic() - st["updated"] > STALE_SEC:
            st["locked"] = False
        return st
