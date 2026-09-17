"""
Seguidor de ritmo del audio del sistema para la velocidad del gato (piso 100%).

FLUJO: captura loopback en estereo (WASAPI en Windows, BlackHole en macOS via
`soundcard`, nunca el microfono; el estereo es a proposito porque el mono
WASAPI puede dar basura) hacia un detector en streaming: envolvente de energia
en graves con filtro pasa-bajos del orden de un kick, umbral adaptativo a una
fraccion del pico reciente, pulsos en flancos con refractario corto, y mediana
robusta de intervalos en segundos plegada a un rango de tempo con puntaje de
confianza. El tempo se publica con histeresis (derivas chicas se siguen, saltos
grandes exigen varios estimados de acuerdo); el tempo publicado mapea una banda
lento->frenetico a energia, que main.py mezcla hacia el tope AUTO.

RESPALDOS: con musica pero sin tempo claro, la densidad de pulsos sustituye
(acotada por debajo del camino de tempo) sobre un suelo del ultimo tempo con
decaimiento lento para que los breaks largos bajen y los cortes no; solo el
silencio sostenido vuelve a velocidad base tras una espera corta. La musica
bajita pasa (solo se descarta silencio digital) y lo rapidisimo no entra al
tempo aunque si cuenta para densidad.

HILOS: EnergyTracker es un hilo daemon captura->detector con acceso
thread-safe a estado y diagnostico; smooth_speed se acerca al objetivo mas
rapido al subir que al bajar.
"""
import math
import sys
import threading
from collections import deque

import numpy as np

SR = 48000
WIN = 1024
HOP = 512
HOPS_PER_SEC = SR / HOP

SILENCE_RMS = 0.012
ENV_BAND_LO, ENV_BAND_HZ = 40.0, 250.0
ENV_TAU = 0.12
ENV_ABS_MIN = 1.0
PULSE_FRAC = 0.8
PEAK_ATTACK = 0.05
PEAK_RELEASE = 2.5
PULSE_REFRACT_SEC = 0.09
TEMPO_WIN_SEC = 5.0
TEMPO_LO, TEMPO_HI = 70.0, 220.0
TEMPO_CONF = 0.5
LOCK_HIST_SEC = 3.0
HYST_TOL = 0.12
HYST_N = 3
HYST_SPAN = 2.0
FLOOR_FRAC = 0.65
FLOOR_TAU = 20.0
IOI_MIN = 0.22
TEMPO_MAP_LO = 90.0
TEMPO_MAP_HI = 170.0
DENSE_WIN_SEC = 3.0
DENSE_NORM = 6.0
DENSE_CAP = 0.6
SILENCE_HOLD_SEC = 2.0


class RhythmFollower:

    def __init__(self):
        self._buf = np.zeros(0, dtype=np.float32)
        self._win_fn = np.hanning(WIN).astype(np.float32)
        freqs = np.fft.rfftfreq(WIN, 1.0 / SR)
        self._lbins = np.where((freqs >= ENV_BAND_LO) & (freqs <= ENV_BAND_HZ))[0]
        self._env = 0.0
        self._peak = 0.0
        self._above = False
        self._pulses = deque(maxlen=96)
        self._last_pulse = -1e9
        self._rms_win = deque(maxlen=100)
        self._samples = 0
        self._tempo = None
        self._conf = 0.0
        self._est_hist = deque(maxlen=32)
        self._lock_e = None
        self._lock_t = -1e9
        self._rate = 0.0
        self._e = 0.0
        self._silence_since = None

    def feed(self, x):
        x = np.asarray(x, dtype=np.float32).ravel()
        if x.size == 0:
            return
        self._rms_win.append(float(np.sqrt(np.mean(x ** 2))))
        self._buf = np.concatenate((self._buf, x))
        while self._buf.size >= WIN:
            frame = self._buf[:WIN]
            self._buf = self._buf[HOP:]
            self._samples += HOP
            self._step(frame)

    def _step(self, frame):
        now = self._samples / SR
        dt = HOP / SR
        frame_rms = float(np.sqrt(np.mean(frame ** 2)))
        mag = np.abs(np.fft.rfft(frame * self._win_fn))
        e_low = float((mag[self._lbins] ** 2).sum())
        k = 1.0 - math.exp(-dt / ENV_TAU)
        self._env += (e_low - self._env) * k
        kp = 1.0 - math.exp(-dt / (PEAK_ATTACK if self._env > self._peak else PEAK_RELEASE))
        self._peak += (self._env - self._peak) * kp
        thr = max(PULSE_FRAC * self._peak, ENV_ABS_MIN)
        if self._env > thr and not self._above\
                and (now - self._last_pulse) >= PULSE_REFRACT_SEC\
                and frame_rms > 0.002:
            self._above = True
            self._last_pulse = now
            self._pulses.append(now)
            self._update_tempo(now)
        elif self._env <= thr:
            self._above = False


        recent = [t for t in self._pulses if now - t <= DENSE_WIN_SEC]
        self._rate = len(recent) / DENSE_WIN_SEC

        pub = self._published(now)
        if pub is not None:
            e = min(max((pub - TEMPO_MAP_LO) / (TEMPO_MAP_HI - TEMPO_MAP_LO), 0.0), 1.0)
            self._lock_e = e
            self._lock_t = now
        else:
            e = min(self._rate / DENSE_NORM, 1.0) * DENSE_CAP
            if self._lock_e is not None:
                e = max(e, FLOOR_FRAC * self._lock_e
                        * math.exp(-max(0.0, now - self._lock_t) / FLOOR_TAU))

        lvl = max(self._rms_win) if self._rms_win else 0.0

        if lvl < SILENCE_RMS and self._rate < 1.0:
            e = 0.0
        self._e = e

    def _published(self, now):
        ests = [(t, b) for (t, b) in self._est_hist if now - t <= LOCK_HIST_SEC]
        if not ests:
            self._tempo = None
            return None
        if len(ests) < 2:
            return self._tempo
        vals = [b for (_, b) in ests]
        med = float(np.median(vals))
        if self._tempo is None:
            self._tempo, self._conf = med, 1.0
            return med
        if abs(med - self._tempo) <= HYST_TOL * self._tempo:
            self._tempo = med
            return med
        fresh = [(t, b) for (t, b) in ests if now - t <= HYST_SPAN]
        if len(fresh) >= HYST_N:
            fvals = [b for (_, b) in fresh[-HYST_N:]]
            fmed = float(np.median(fvals))
            if all(abs(x - self._tempo) > HYST_TOL * self._tempo for x in fvals)\
                    and all(abs(x - fmed) <= 0.1 * fmed for x in fvals):
                self._tempo = fmed
                return fmed
        return self._tempo

    def _update_tempo(self, now):
        ts = [t for t in self._pulses if now - t <= TEMPO_WIN_SEC]
        if len(ts) < 4:
            return
        iois = np.diff(np.asarray(ts))
        iois = iois[(iois >= IOI_MIN) & (iois <= 1.0)]
        if iois.size < 3:
            return
        bpms = 60.0 / iois
        for i in range(bpms.size):
            while bpms[i] < TEMPO_LO:
                bpms[i] *= 2.0
            while bpms[i] > TEMPO_HI:
                bpms[i] /= 2.0
        med = float(np.median(bpms))
        conf = float(np.mean(np.abs(bpms - med) <= 0.1 * med))
        conf *= min(1.0, iois.size / 5.0)
        if conf >= TEMPO_CONF:
            self._est_hist.append((now, med))
            self._conf = conf

    def diag(self):
        return {"rate": round(self._rate, 2),
                "bpm": round(self._tempo, 1) if self._tempo else 0,
                "conf": round(self._conf, 2),
                "e": round(min(max(self._e, 0.0), 1.0), 3)}

    def read(self):
        level = max(self._rms_win) if self._rms_win else 0.0
        now = self._samples / SR
        if level < SILENCE_RMS and self._rate < 1.0:
            if self._silence_since is None:
                self._silence_since = now
            if now - self._silence_since <= SILENCE_HOLD_SEC:
                return min(max(self._e, 0.0), 1.0), level
            return 0.0, level
        self._silence_since = None
        return min(max(self._e, 0.0), 1.0), level


def energy_to_speed(e, auto_max):
    return 1.0 + min(max(float(e), 0.0), 1.0) * (float(auto_max) - 1.0)


def smooth_speed(cur, tgt, dt, up_tau=0.2, down_tau=0.6):
    tau = up_tau if tgt > cur else down_tau
    k = 1.0 - math.exp(-max(1e-3, dt) / tau)
    return cur + (tgt - cur) * k


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


class EnergyTracker(threading.Thread):

    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._stop_ev = threading.Event()
        self._energy = 0.0
        self._level = 0.0
        self._diag = {"rate": 0.0, "loud": 0.0, "drive": 0.0}
        self._device = ""
        self.ok = False
        self.error = "sin iniciar"

    def run(self):
        try:
            import soundcard as sc
        except Exception as e:
            with self._lock:
                self.error = f"falta libreria de audio: {e}"
            return
        mic, err = pick_loopback_device()
        if mic is None:
            with self._lock:
                self.error = err
            return
        det = RhythmFollower()
        try:

            with mic.recorder(samplerate=SR, channels=[0, 1]) as rec:
                with self._lock:
                    self.ok = True
                    self.error = ""
                    self._device = mic.name
                while not self._stop_ev.is_set():
                    try:
                        data = rec.record(numframes=HOP)
                    except Exception as e:
                        with self._lock:
                            self.ok = False
                            self.error = f"error capturando: {e}"
                        return
                    mono = np.asarray(data).mean(axis=1).astype(np.float32)
                    det.feed(mono)
                    e, level = det.read()
                    with self._lock:
                        self._energy = e
                        self._level = level
                        self._diag = det.diag()
        except Exception as e:
            with self._lock:
                self.ok = False
                self.error = f"no pude abrir loopback ({mic.name}): {e}"

    def stop(self):
        self._stop_ev.set()

    def get_state(self):
        with self._lock:
            return self._energy, self._level, self.ok, self.error

    def get_diag(self):
        with self._lock:
            return dict(self._diag, device=self._device, ok=self.ok, error=self.error)
