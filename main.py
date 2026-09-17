"""
GatoFlow - widget flotante siempre encima con gato bailando y Pomodoro.

QUE ES: ventana sin marcos y translucida con el loop MP4 del gato sobre un
timer flotante. El gato se ve en foco/reposo inicial; en descansos solo queda
el timer. Una sola instancia (QLockFile).

VIDEO: el MP4 se decodifica frame a frame con OpenCV hacia un QLabel (un
QVideoWidget sale en negro en ventanas frameless/translucidas y en el .exe;
con OpenCV el .exe tampoco depende de codecs del sistema). El timer de frames
usa temporizador preciso y un reloj de video sin deriva: el avance atrasado se
acota por tick y el reloj se resincroniza tras pausas o al volver de oculto.

MOUSE: Windows no manda eventos a pixeles 100% transparentes, asi que la
ventana lleva un fondo casi invisible, un scrim que captura clicks sobre el
area del video en descansos y una mascara del rect completo: hover, botones y
resize funcionan aunque solo haya texto flotante. El timer vive en el layout
debajo del video (centrado estructural, sin aritmetica DPI); los botones
flotan sobre el video. Todos los topes de ancho estan en un solo lugar y el
resize los respeta; un press con controles ocultos los revela y sintetiza el
click si cayo sobre un boton.

POMODORO: maquina de estados foco / descanso corto / largo, con duraciones
configurables, descansos en 0 que se saltan solos, meta diaria, auto-inicio y
sonido al cambiar de fase. El sonido suena en el hilo UI (MCI falla desde
hilos secundarios); cada beep corta al anterior para que nunca se encimen, y
el fallback bloqueante solo se usa si falta el mp3.

VELOCIDAD: porcentaje manual o AUTO por ritmo desde bpm.py, suavizada y
aplicada al intervalo de frames; en AUTO el aviso de "sin audio" sale una sola
vez por cambio de ajustes y el hilo de captura se reintenta si muere.

CONFIG: carpeta estandar de cada SO cuando va compilado (con migracion unica
del sidecar viejo junto al binario), config.json junto a main.py desde codigo.
Posicion y tamano persisten al cerrar o redimensionar. Subir APP_VERSION (se ve
en el titulo de ajustes) en cada build.
"""
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import bpm

BASE_DIR = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    _MEI = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = BASE_DIR
    _MEI = BASE_DIR
ASSETS = _MEI / "assets"
MP4_PATH = ASSETS / "gato_loop.mp4"
ICON_PATH = ASSETS / "icon.png"
MP3_PATH = ASSETS / "si.mp3"
APP_VERSION = "13"


def _config_path():


    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "GatoFlow"
        elif sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "GatoFlow"
        else:
            base = Path.home() / ".config" / "GatoFlow"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            base = APP_DIR
        p = base / "config.json"
        try:
            legacy = APP_DIR / "config.json"
            if not p.exists() and legacy.exists() and legacy.resolve() != p.resolve():
                shutil.move(str(legacy), str(p))
        except Exception as e:
            print(f"[GatoFlow] no pude migrar config: {e}")
        return p
    return BASE_DIR / "config.json"


CONFIG_PATH = _config_path()

NATIVE_W, NATIVE_H = 278, 498
ASPECT = NATIVE_H / NATIVE_W
MIN_W, MAX_W = 100, 480
MAX_VIDEO_H = 316
EDGE = 10

DEFAULT_CONFIG = {
    "focus_min": 25,
    "short_break_min": 5,
    "long_break_min": 15,
    "sessions_per_cycle": 4,
    "total_pomodoros": 8,
    "auto_start_next": False,
    "sound": True,
    "speed_auto": False,
    "manual_pct": 100,
    "automax_pct": 200,
    "video_w": 260,
    "pos_x": None,
    "pos_y": None,
}

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[GatoFlow] config corrupta, uso defaults: {e}")
    for k in ("focus_min", "short_break_min", "long_break_min",
              "sessions_per_cycle", "total_pomodoros", "video_w",
              "manual_pct", "automax_pct"):
        try:
            cfg[k] = int(cfg.get(k, DEFAULT_CONFIG[k]))
        except Exception:
            cfg[k] = DEFAULT_CONFIG[k]
    cfg["focus_min"] = max(1, min(180, cfg["focus_min"]))
    cfg["short_break_min"] = max(0, min(60, cfg["short_break_min"]))
    cfg["long_break_min"] = max(0, min(90, cfg["long_break_min"]))
    cfg["sessions_per_cycle"] = max(1, min(12, cfg["sessions_per_cycle"]))
    cfg["total_pomodoros"] = max(1, min(32, cfg["total_pomodoros"]))
    cfg["video_w"] = max(MIN_W, min(MAX_W, cfg["video_w"]))
    cfg["speed_auto"] = bool(cfg.get("speed_auto", False))
    cfg["manual_pct"] = max(50, min(200, cfg["manual_pct"]))
    cfg["automax_pct"] = max(100, min(250, cfg["automax_pct"]))
    return cfg

def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[GatoFlow] no pude guardar config en {CONFIG_PATH}: {e}")

_afplay_proc = None
_mci_open = False


def _fallback_beep():


    try:
        import winsound
        winsound.Beep(880, 250)
        winsound.Beep(1174, 350)
        return
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            import subprocess
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            pass
    print("\a", end="", flush=True)


def beep(ok=True):


    global _afplay_proc, _mci_open
    if not MP3_PATH.exists():
        threading.Thread(target=_fallback_beep, daemon=True).start()
        return
    try:
        if sys.platform == "win32":
            import ctypes
            mci = ctypes.windll.winmm.mciSendStringW
            if not _mci_open:
                if mci(f'open "{MP3_PATH}" alias gatonotify', None, 0, None) != 0:
                    return
                mci("setaudio gatonotify volume to 400", None, 0, None)
                _mci_open = True
            mci("stop gatonotify", None, 0, None)
            mci("seek gatonotify to start", None, 0, None)
            mci("play gatonotify", None, 0, None)
            return
        if sys.platform == "darwin":
            import subprocess
            try:
                if _afplay_proc is not None and _afplay_proc.poll() is None:
                    _afplay_proc.terminate()
            except Exception:
                pass
            _afplay_proc = subprocess.Popen(
                ["afplay", "-v", "0.4", str(MP3_PATH)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    except Exception as e:
        print(f"[GatoFlow] sonido ({e})")


import cv2
from PySide6.QtCore import Qt, QTimer, QEvent, QLockFile, QDir
from PySide6.QtGui import QImage, QPixmap, QIcon, QRegion
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QDialog, QFormLayout, QSpinBox, QCheckBox,
    QDialogButtonBox, QMessageBox, QGraphicsDropShadowEffect, QLayout,
    QSpacerItem, QSizePolicy,
)

_HAS_BGR888 = hasattr(QImage, "Format_BGR888")

OVERLAY_BTN = """
QPushButton {
    background: rgba(15, 15, 22, 170);
    color: #ffffff;
    border: 1px solid rgba(255,255,255,45);
    border-radius: 15px;
    font-size: 14px;
}
QPushButton:hover { background: rgba(50, 50, 65, 200); }
"""


def shadow(obj, blur=10):
    eff = QGraphicsDropShadowEffect(obj)
    eff.setBlurRadius(blur)
    eff.setColor(Qt.black)
    eff.setOffset(0, 0)
    obj.setGraphicsEffect(eff)
    return eff


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"GatoFlow — configurar (v{APP_VERSION})")
        self.setModal(True)
        layout = QFormLayout(self)

        self.spin_focus = QSpinBox(); self.spin_focus.setRange(1, 180); self.spin_focus.setValue(cfg["focus_min"]); self.spin_focus.setSuffix(" min")
        self.spin_short = QSpinBox(); self.spin_short.setRange(0, 60); self.spin_short.setValue(cfg["short_break_min"]); self.spin_short.setSuffix(" min")
        self.spin_long = QSpinBox(); self.spin_long.setRange(0, 90); self.spin_long.setValue(cfg["long_break_min"]); self.spin_long.setSuffix(" min")
        self.spin_cycle = QSpinBox(); self.spin_cycle.setRange(1, 12); self.spin_cycle.setValue(cfg["sessions_per_cycle"])
        self.spin_total = QSpinBox(); self.spin_total.setRange(1, 32); self.spin_total.setValue(cfg["total_pomodoros"])
        self.chk_auto = QCheckBox("Auto-iniciar siguiente fase"); self.chk_auto.setChecked(cfg.get("auto_start_next", False))
        self.chk_sound = QCheckBox("Sonido al cambiar de fase"); self.chk_sound.setChecked(cfg.get("sound", True))
        self.chk_speed = QCheckBox("Velocidad AUTO (beta)")
        self.chk_speed.setChecked(cfg.get("speed_auto", False))
        speed_desc = QLabel("Escucha el audio y aumenta o decrementa la velocidad del gato con el sonido.")
        speed_desc.setWordWrap(True)
        speed_desc.setStyleSheet("color:#71717a; font-size:11px;")
        self.spin_manual = QSpinBox(); self.spin_manual.setRange(50, 200); self.spin_manual.setValue(cfg.get("manual_pct", 100)); self.spin_manual.setSuffix(" %")
        self.spin_max = QSpinBox(); self.spin_max.setRange(100, 250); self.spin_max.setValue(cfg.get("automax_pct", 200)); self.spin_max.setSuffix(" % tope")
        self.spin_manual.setEnabled(not self.chk_speed.isChecked())
        self.spin_max.setEnabled(self.chk_speed.isChecked())
        self.chk_speed.toggled.connect(lambda on: (self.spin_manual.setEnabled(not on), self.spin_max.setEnabled(on)))

        layout.addRow("Trabajo:", self.spin_focus)
        layout.addRow("Descanso corto:", self.spin_short)
        layout.addRow("Descanso largo:", self.spin_long)
        layout.addRow("Descanso largo cada N sesiones:", self.spin_cycle)
        layout.addRow("Total pomodoros:", self.spin_total)
        layout.addRow(self.chk_auto)
        layout.addRow(self.chk_sound)
        layout.addRow(self.chk_speed)
        layout.addRow(speed_desc)
        layout.addRow("Velocidad manual:", self.spin_manual)
        layout.addRow("Tope en AUTO:", self.spin_max)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def values(self):
        return {
            "focus_min": self.spin_focus.value(),
            "short_break_min": self.spin_short.value(),
            "long_break_min": self.spin_long.value(),
            "sessions_per_cycle": self.spin_cycle.value(),
            "total_pomodoros": self.spin_total.value(),
            "auto_start_next": self.chk_auto.isChecked(),
            "sound": self.chk_sound.isChecked(),
            "speed_auto": self.chk_speed.isChecked(),
            "manual_pct": self.spin_manual.value(),
            "automax_pct": self.spin_max.value(),
        }


class GatoWidget(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.phase = "IDLE"
        self.current_pomodoro = 1
        self.remaining = cfg["focus_min"] * 60
        self.running = False
        self.video_w = cfg.get("video_w", 260)

        self._drag_pos = None
        self._resize_mode = None
        self._resize_start = None
        self._resize_start_global = None
        self._press_btn = None
        self._press_global = None
        self._hovered = False
        self._qimg = None
        self._cap = None
        self.base_interval = 40
        self.speed = 1.0
        self.tracker = None
        self._speed_warned = False
        self._last_speed_tick = time.monotonic()
        self._retry_at = 0.0
        self._tracker_since = 0.0
        self._log_n = 0
        self._sfont = 11
        self._mx = 10
        self._btn = 30
        self._status_color = "#7CFFB2"
        self._timer_color = "white"

        self._build_ui()
        self._apply_video_size(self.video_w)
        self._open_mp4()
        self.tick = QTimer(self)
        self.tick.setInterval(1000)
        self.tick.timeout.connect(self._on_tick)
        self._refresh_ui()
        self._place_window()


    def _build_ui(self):
        self.setWindowTitle("GatoFlow")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setMinimumWidth(MIN_W)


        self.setStyleSheet("GatoWidget { background-color: rgba(0, 0, 0, 2); }")
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.setSizeConstraint(QLayout.SetNoConstraint)

        self.cat = QLabel(alignment=Qt.AlignCenter)
        self.cat.setStyleSheet("background: transparent;")
        self.cat.setMouseTracking(True)
        root.addWidget(self.cat)


        self.scrim = QLabel("", self)
        self.scrim.setStyleSheet("background-color: rgba(0, 0, 0, 8);")
        self.scrim.setMouseTracking(True)
        self.scrim.hide()
        self.status = QLabel("", self)
        self.status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.status.setStyleSheet("background: transparent; color: #7CFFB2; font-size: 11px; font-weight: 700;")
        shadow(self.status, 8)
        self.status.hide()

        self.timer_label = QLabel("25:00", self)
        self.timer_label.setAlignment(Qt.AlignCenter)
        self.timer_label.setStyleSheet("background: transparent; color: white; font-size: 22px; font-weight: 800;")
        shadow(self.timer_label, 10)


        self.timer_label.setFixedHeight(30)
        root.addSpacerItem(QSpacerItem(0, 4, QSizePolicy.Fixed, QSizePolicy.Fixed))
        root.addWidget(self.timer_label)

        self.topbar = QWidget(self)
        self.topbar.setStyleSheet("background: transparent;")
        tb = QHBoxLayout(self.topbar)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(6)
        tb.addStretch(1)
        self.btn_cfg = QPushButton("⚙")
        self.btn_close = QPushButton("✕")
        for b in (self.btn_cfg, self.btn_close):
            b.setFixedSize(30, 30)
            b.setStyleSheet(OVERLAY_BTN)
            b.setCursor(Qt.PointingHandCursor)
        self.btn_cfg.setToolTip("Configurar")
        self.btn_close.setToolTip("Cerrar")
        self.btn_cfg.clicked.connect(self.open_settings)
        self.btn_close.clicked.connect(self.close_app)
        tb.addWidget(self.btn_cfg)
        tb.addWidget(self.btn_close)

        self.controls = QWidget(self)
        self.controls.setStyleSheet("background: transparent;")
        cl = QHBoxLayout(self.controls)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        cl.addStretch(1)
        self.btn_start = QPushButton("▶")
        self.btn_skip = QPushButton("⏭")
        self.btn_reset = QPushButton("↺")
        for b in (self.btn_start, self.btn_skip, self.btn_reset):
            b.setFixedSize(30, 30)
            b.setStyleSheet(OVERLAY_BTN)
            b.setCursor(Qt.PointingHandCursor)
        self.btn_start.setToolTip("Iniciar / pausar")
        self.btn_skip.setToolTip("Saltar fase")
        self.btn_reset.setToolTip("Reiniciar")
        self.btn_start.clicked.connect(self.toggle)
        self.btn_skip.clicked.connect(self.skip)
        self.btn_reset.clicked.connect(self.reset_all)
        cl.addWidget(self.btn_start)
        cl.addWidget(self.btn_skip)
        cl.addWidget(self.btn_reset)
        cl.addStretch(1)

        self.topbar.hide()
        self.controls.hide()

        for w in (self.cat, self.scrim, self.status,
                  self.timer_label, self.topbar, self.controls):
            w.installEventFilter(self)

    def _layout_overlays(self):


        w = self.width()
        vh = self.cat.height()
        self.scrim.setGeometry(0, 0, w, vh)
        b = getattr(self, "_btn", 30)
        gap = getattr(self, "_gap", 6)
        tbw = 2 * b + gap + 4
        self.topbar.setGeometry(max(0, w - tbw - 8), 8, tbw, b)
        cw, ch = 3 * b + 2 * gap + 8, b + 6

        cx, cy = max(0, (w - cw) // 2), max(0, (vh - ch) // 2)
        self.controls.setGeometry(cx, cy, cw, ch)

        self.status.setGeometry(0, min(vh - 18, cy + ch + 8), w, 18)


    def _update_mask(self):


        try:
            self.setMask(QRegion(self.rect()))
        except Exception:
            pass

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._layout_overlays()
        self._update_mask()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._layout_overlays()
        self._update_mask()

    def _sync_status_vis(self):
        self.status.setAlignment(Qt.AlignCenter)
        if not self.status.text():
            self.status.hide()
        elif self.phase in ("SHORT_BREAK", "LONG_BREAK", "DONE"):
            self.status.show()
        else:
            self.status.setVisible(self._hovered)

    def enterEvent(self, ev):
        self._show_overlays()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hovered = False
        self.topbar.hide()
        self.controls.hide()
        self._sync_status_vis()
        super().leaveEvent(ev)


    def _video_h(self, w):
        return round(w * NATIVE_H / NATIVE_W)

    def _clamp_w(self, w):


        try:
            w = min(w, MAX_VIDEO_H * NATIVE_W / NATIVE_H)
            scr = QApplication.primaryScreen().availableGeometry()
            cap_h = scr.height() - 80
            if self.isVisible():
                cap_h = min(cap_h, scr.bottom() - self.y() - 60)
            w = min(w, max(cap_h - 34, MIN_W) * NATIVE_W / NATIVE_H)
        except Exception:
            pass
        return max(MIN_W, min(MAX_W, int(round(w))))

    def _apply_video_size(self, w):
        self.video_w = self._clamp_w(w)
        self.cat.setFixedSize(self.video_w, self._video_h(self.video_w))
        self.setFixedWidth(self.video_w)
        self.adjustSize()
        try:
            scr = QApplication.primaryScreen().availableGeometry()
            if self.isVisible() and self.y() + self.height() > scr.bottom() - 20:
                self.move(self.x(), max(scr.top(), scr.bottom() - 20 - self.height()))
        except Exception:
            pass
        self._apply_ui_scale()
        self._paint_frame()
        self._layout_overlays()

    def _ui_metrics(self):
        s = self.video_w / 260.0
        btn = max(20, min(30, round(30 * s)))
        gap = max(3, min(6, round(6 * s)))
        tfont = max(14, min(22, round(22 * s)))
        sfont = max(9, min(11, round(11 * s)))
        mx = max(6, round(10 * s))
        return btn, gap, tfont, sfont, mx

    def _apply_ui_scale(self):
        btn, gap, tfont, sfont, mx = self._ui_metrics()
        for b in (self.btn_start, self.btn_skip, self.btn_reset,
                  self.btn_cfg, self.btn_close):
            b.setFixedSize(btn, btn)
        try:
            self.controls.layout().setSpacing(gap)
            self.topbar.layout().setSpacing(gap)
        except Exception:
            pass
        self._sfont = sfont
        self._mx = mx
        self._btn = btn
        self._gap = gap
        self._paint_timer()
        self._paint_status()

    def _paint_timer(self):
        try:
            tfont = max(14, min(22, round(22 * self.video_w / 260.0)))
            self.timer_label.setStyleSheet(
                f"background: transparent; color: {self._timer_color}; "
                f"font-size: {tfont}px; font-weight: 800;")
        except Exception:
            pass

    def _paint_status(self):
        try:
            self.status.setStyleSheet(
                f"background: transparent; color: {self._status_color}; "
                f"font-size: {self._sfont}px; font-weight: 700;")
        except Exception:
            pass

    def _focus_text(self):
        total = self.cfg["total_pomodoros"]
        cur = self.current_pomodoro
        pct = f" · {round(self.speed * 100)}%" if self.cfg.get("speed_auto") else ""
        if self.video_w < 160:
            return f"●{cur}/{total}{pct}"
        return f"● ENFOQUE {cur}/{total}{pct}"

    def _hit_zone(self, p):
        w, h = self.width(), self.height()
        l = p.x() <= EDGE
        r = p.x() >= w - EDGE
        t = p.y() <= EDGE
        b = p.y() >= h - EDGE
        if t and l:
            return "topleft"
        if t and r:
            return "topright"
        if b and l:
            return "bottomleft"
        if b and r:
            return "bottomright"
        if l:
            return "left"
        if r:
            return "right"
        if t:
            return "top"
        if b:
            return "bottom"
        return None

    def _update_cursor(self, zone):
        cursors = {"left": Qt.SizeHorCursor, "right": Qt.SizeHorCursor,
                   "top": Qt.SizeVerCursor, "bottom": Qt.SizeVerCursor,
                   "topleft": Qt.SizeFDiagCursor, "bottomright": Qt.SizeFDiagCursor,
                   "topright": Qt.SizeBDiagCursor, "bottomleft": Qt.SizeBDiagCursor}
        c = cursors.get(zone, Qt.ArrowCursor)
        for obj in (self, self.cat, self.scrim,
                    self.status, self.timer_label, self.topbar, self.controls):
            try:
                obj.setCursor(c)
            except Exception:
                pass

    def _show_overlays(self):
        self._hovered = True
        self.topbar.show()
        self.controls.show()
        self._sync_status_vis()

    def _press_at(self, local, glob, button):
        if button != Qt.LeftButton:
            return
        if not self.controls.isVisible():


            self._show_overlays()
            hit = self.childAt(local)
            if isinstance(hit, QPushButton):
                self._press_btn = hit
                self._press_global = glob
                return
        zone = self._hit_zone(local)
        if zone:
            self._resize_mode = zone
            g = self.geometry()
            self._resize_start = (g.x(), g.y(), self.video_w)
            self._resize_start_global = glob
        else:
            self._drag_pos = glob - self.frameGeometry().topLeft()

    def _move_at(self, local, glob, buttons):
        if self._press_btn is not None and (buttons & Qt.LeftButton):
            if (glob - self._press_global).manhattanLength() > 8:

                self._press_btn = None
                self._press_global = None
                self._drag_pos = glob - self.frameGeometry().topLeft()
                return
        if self._resize_mode and (buttons & Qt.LeftButton):
            sx, sy, sw = self._resize_start
            dx = glob.x() - self._resize_start_global.x()
            dy = glob.y() - self._resize_start_global.y()
            mode = self._resize_mode
            if mode == "right":
                cand = sw + dx
            elif mode == "left":
                cand = sw - dx
            elif mode == "bottom":
                cand = sw + dy / ASPECT
            elif mode == "top":
                cand = sw - dy / ASPECT
            elif mode == "bottomright":
                cand = max(sw + dx, sw + dy / ASPECT)
            elif mode == "bottomleft":
                cand = max(sw - dx, sw + dy / ASPECT)
            elif mode == "topright":
                cand = max(sw + dx, sw - dy / ASPECT)
            else:
                cand = max(sw - dx, sw - dy / ASPECT)
            new_w = self._clamp_w(cand)
            nx, ny = sx, sy
            if mode in ("left", "bottomleft", "topleft"):
                nx = sx + (sw - new_w)
            if mode in ("top", "topright", "topleft"):
                ny = sy + round((sw - new_w) * ASPECT)
            self._apply_video_size(new_w)
            self.move(int(nx), int(ny))
        elif self._drag_pos is not None and (buttons & Qt.LeftButton):
            self.move(glob - self._drag_pos)
        else:
            self._update_cursor(self._hit_zone(local))

    def _release(self, local=None):
        if self._press_btn is not None:
            btn, self._press_btn = self._press_btn, None
            self._press_global = None
            if local is not None:
                try:
                    if self.childAt(local) is btn:
                        btn.click()
                except Exception:
                    pass
        was_resizing = self._resize_mode is not None
        self._resize_mode = None
        self._drag_pos = None
        self._resize_start_global = None
        if was_resizing:
            self.cfg["video_w"] = self.video_w
            save_config(self.cfg)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press_at(ev.position().toPoint(), ev.globalPosition().toPoint(), ev.button())
            ev.accept()

    def mouseMoveEvent(self, ev):
        self._move_at(ev.position().toPoint(), ev.globalPosition().toPoint(), ev.buttons())
        ev.accept()

    def mouseReleaseEvent(self, ev):
        self._release(ev.position().toPoint())

    def eventFilter(self, obj, ev):
        if isinstance(obj, QPushButton):
            return False
        t = ev.type()
        try:
            if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
                self._press_at(obj.mapTo(self, ev.position().toPoint()),
                               ev.globalPosition().toPoint(), ev.button())
            elif t == QEvent.MouseMove:
                btns = ev.buttons()
                if btns & Qt.LeftButton:
                    self._move_at(obj.mapTo(self, ev.position().toPoint()),
                                  ev.globalPosition().toPoint(), btns)
                else:
                    self._update_cursor(self._hit_zone(obj.mapTo(self, ev.position().toPoint())))
            elif t == QEvent.MouseButtonRelease:
                self._release(obj.mapTo(self, ev.position().toPoint()))
        except Exception:
            pass
        return False


    def _open_mp4(self):
        if not MP4_PATH.exists():
            self.cat.setText("🐈\nNo encuentro:\n" + str(MP4_PATH))
            return
        self._cap = cv2.VideoCapture(str(MP4_PATH))
        if not self._cap.isOpened():
            self.cat.setText("🐈\nNo pude abrir el mp4")
            self._cap = None
            return
        fps = self._cap.get(cv2.CAP_PROP_FPS) or 25.0
        interval = max(1, int(round(1000.0 / fps)))
        self.base_interval = interval
        self._vid_fps = float(fps)
        self.frame_tick = QTimer(self)
        self.frame_tick.setTimerType(Qt.PreciseTimer)
        self.frame_tick.setInterval(max(1, int(round(interval / self.speed))))
        self.frame_tick.timeout.connect(self._next_mp4_frame)
        self.frame_tick.start()
        self._resync_vclock()

    def _resync_vclock(self):

        self._vtime = 0.0
        self._vt_wall = time.monotonic()
        self._vshown = 0

    def _show_one_frame(self):
        ok, frame = self._cap.read()
        if not ok or frame is None:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
            if not ok or frame is None:
                return False
        h, w = frame.shape[:2]
        try:
            if _HAS_BGR888:
                self._qimg = QImage(frame.data, w, h, w * 3, QImage.Format_BGR888).copy()
            else:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                self._qimg = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888).copy()
        except Exception:
            return False
        self._paint_frame()
        self._vshown += 1
        return True

    def _next_mp4_frame(self):
        if self._cap is None or self.phase not in ("IDLE", "FOCUS"):
            return
        now = time.monotonic()
        gap = now - self._vt_wall
        if gap > 0.5:
            self._resync_vclock()
            return
        self._vtime += gap * self.speed
        self._vt_wall = now
        due = int(self._vtime * self._vid_fps)


        for _ in range(min(max(0, due - self._vshown), 4)):
            if not self._show_one_frame():
                break

    def _paint_frame(self):
        if self._qimg is None or self._qimg.isNull():
            return
        try:
            pix = QPixmap.fromImage(self._qimg).scaled(
                self.cat.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.cat.setPixmap(pix)
        except Exception:
            pass

    def _place_window(self):
        x, y = self.cfg.get("pos_x"), self.cfg.get("pos_y")
        if isinstance(x, int) and isinstance(y, int):
            self.move(x, y)
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - self.width() - 30, screen.bottom() - self.height() - 60)
        self.speed_tick = QTimer(self)
        self.speed_tick.setInterval(250)
        self.speed_tick.timeout.connect(self._speed_tick)
        self.speed_tick.start()
        self._apply_manual_speed()
        if self.cfg.get("speed_auto"):
            self._ensure_tracker(True)


    def _set_frame_interval(self):
        ft = getattr(self, "frame_tick", None)
        if ft is not None:
            try:
                ft.setInterval(max(1, int(round(self.base_interval / max(0.25, self.speed)))))
            except Exception:
                pass

    def _apply_manual_speed(self):
        self.speed = self.cfg.get("manual_pct", 100) / 100.0
        self._set_frame_interval()
        self._resync_vclock()

    def _stop_tracker_sync(self, timeout=0.5):
        tr, self.tracker = self.tracker, None
        if tr is not None:
            try:
                tr.stop()
            except Exception:
                pass
            try:
                tr.join(timeout=timeout)
            except Exception:
                pass

    def _ensure_tracker(self, on):
        if on:
            alive = self.tracker is not None and self.tracker.is_alive()
            if not alive:
                self._stop_tracker_sync(timeout=0.2)
                self.tracker = bpm.EnergyTracker()
                self.tracker.start()
                self._tracker_since = time.monotonic()
        else:
            self._stop_tracker_sync(timeout=0.5)

    def _append_log_file(self, text):
        try:
            p = Path(CONFIG_PATH).parent / "audio.log"
            try:
                lines = p.read_text(encoding="utf-8").splitlines()[-399:]
            except Exception:
                lines = []
            lines.append(text)
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _geo_check(self):


        try:
            W, H = self.width(), self.height()
            bad = []
            for name in ("status", "timer_label", "controls", "topbar", "scrim"):
                g = getattr(self, name).geometry()
                if g.x() < 0 or g.y() < 0 or g.x() + g.width() > W + 1 or g.y() + g.height() > H + 1:
                    bad.append(f"{name}=({g.x()},{g.y()},{g.width()}x{g.height()})")
            tc = self.timer_label.geometry()
            cc = self.controls.geometry()
            if abs((tc.x() + tc.width() // 2) - W // 2) > 2:
                bad.append(f"timer descentrado (cx={tc.x() + tc.width() // 2} vs {W // 2})")
            if abs((cc.x() + cc.width() // 2) - W // 2) > 2:
                bad.append(f"controles descentrados (cx={cc.x() + cc.width() // 2} vs {W // 2})")
            if bad:
                self._append_log_file(f"GEO fase={self.phase} win={W}x{H} " + " ".join(bad))
        except Exception:
            pass

    def _log_audio(self):
        if self.tracker is None:
            return
        try:
            d = self.tracker.get_diag()
            e, lvl, ok, _ = self.tracker.get_state()
            self._append_log_file(
                f"{datetime.now():%H:%M:%S} ok={int(ok)} "
                f"lvl={lvl:.4f} rate={d.get('rate', 0)} bpm={d.get('bpm', 0)} "
                f"conf={d.get('conf', 0)} e={e:.3f} spd={round(self.speed * 100)}% "
                f"dev={str(d.get('device', ''))[:48]}")
        except Exception:
            pass

    def _speed_tick(self):
        now = time.monotonic()
        dt = now - self._last_speed_tick
        self._last_speed_tick = now
        auto = self.cfg.get("speed_auto", False)
        if auto and (self.tracker is None or not self.tracker.is_alive()):
            if now - self._retry_at > 15.0:
                self._retry_at = now
                self._ensure_tracker(True)
        if auto and self.tracker is not None:
            e, _, ok, err = self.tracker.get_state()
            if not ok:


                if not self._speed_warned and now - self._tracker_since > 5.0:
                    self._speed_warned = True
                    QMessageBox.warning(self, "GatoFlow",
                        f"Velocidad AUTO sin audio del sistema:\n{err}\n\nEl video sigue a 1.0x.")
                tgt = 1.0
            else:
                self._speed_warned = False
                tgt = bpm.energy_to_speed(e, self.cfg.get("automax_pct", 200) / 100.0)
            self._log_n += 1
            if self._log_n % 20 == 0:
                self._log_audio()
        elif auto:
            tgt = 1.0
        else:
            tgt = self.cfg.get("manual_pct", 100) / 100.0
        new = bpm.smooth_speed(self.speed, tgt, dt)
        if abs(new - self.speed) > 0.005:
            self.speed = new
            self._set_frame_interval()
            if self.phase == "FOCUS":
                self.status.setText(self._focus_text())
                self._sync_status_vis()


    @staticmethod
    def fmt(secs):
        m, s = divmod(max(0, int(secs)), 60)
        return f"{m:02d}:{s:02d}"

    def phase_minutes(self):
        if self.phase == "FOCUS":
            return self.cfg["focus_min"]
        if self.phase == "SHORT_BREAK":
            return self.cfg["short_break_min"]
        if self.phase == "LONG_BREAK":
            return self.cfg["long_break_min"]
        return self.cfg["focus_min"]

    def _is_zero_break(self, phase):
        if phase == "SHORT_BREAK":
            return self.cfg["short_break_min"] <= 0
        if phase == "LONG_BREAK":
            return self.cfg["long_break_min"] <= 0
        return False

    def start_phase(self, phase):
        while self._is_zero_break(phase):
            self.current_pomodoro += 1
            if self.current_pomodoro > self.cfg["total_pomodoros"]:
                return self._finish_day()
            phase = "FOCUS"
        self.phase = phase
        self.remaining = self.phase_minutes() * 60
        self.running = True
        self.tick.start()
        self._resync_vclock()
        self._refresh_ui()

    def _finish_day(self):
        self.phase = "DONE"
        self.running = False
        self.tick.stop()
        self.btn_start.setText("↺")
        self._refresh_ui()
        QMessageBox.information(self, "GatoFlow",
            f"Meta cumplida: {self.cfg['total_pomodoros']} pomodoros.")

    def toggle(self):
        if self.phase == "DONE":
            self.reset_all()
            return
        if self.phase == "IDLE":
            self.current_pomodoro = 1
            self.start_phase("FOCUS")
            return
        self.running = not self.running
        if self.running:
            self.tick.start()
            self._resync_vclock()
        else:
            self.tick.stop()
        self._refresh_ui()

    def skip(self):
        self._advance(auto=True)

    def reset_all(self):
        self.tick.stop()
        self.phase = "IDLE"
        self.current_pomodoro = 1
        self.remaining = self.cfg["focus_min"] * 60
        self.running = False
        self._refresh_ui()

    def _on_tick(self):
        if not self.running:
            return
        self.remaining -= 1
        if self.remaining <= 0:
            self._advance()
        else:
            self.timer_label.setText(self.fmt(self.remaining))

    def _advance(self, auto=False):
        finished = self.phase
        if self.cfg.get("sound", True):
            beep(ok=(finished == "FOCUS"))

        if finished == "FOCUS":
            done = self.current_pomodoro
            total = self.cfg["total_pomodoros"]
            per_cycle = max(1, self.cfg["sessions_per_cycle"])
            if done >= total:
                return self._finish_day()
            nxt = "LONG_BREAK" if (done % per_cycle == 0) else "SHORT_BREAK"
            if self._is_zero_break(nxt):
                self.current_pomodoro += 1
                if self.current_pomodoro > total:
                    return self._finish_day()
                nxt = "FOCUS"
        elif finished in ("SHORT_BREAK", "LONG_BREAK"):
            self.current_pomodoro += 1
            nxt = "FOCUS"
        else:
            nxt = "FOCUS"

        if self.cfg.get("auto_start_next", False) or auto:
            self.start_phase(nxt)
        else:
            self.phase = nxt
            self.remaining = self.phase_minutes() * 60
            self.running = False
            self.tick.stop()
            self._refresh_ui()


    def _refresh_ui(self):
        total = self.cfg["total_pomodoros"]
        self.timer_label.setText(self.fmt(self.remaining))

        show_cat = self.phase in ("IDLE", "FOCUS")


        self.scrim.setVisible(not show_cat)
        self.setFixedWidth(self.video_w)
        if show_cat:
            self._paint_frame()
        else:
            try:
                self.cat.clear()
            except Exception:
                pass

        if self.phase == "FOCUS":
            self.status.setText(self._focus_text())
            self._status_color = "#7CFFB2"
            self._timer_color = "#4ADE80"
            self.btn_start.setText("⏸" if self.running else "▶")
        elif self.phase == "SHORT_BREAK":
            self.status.setText(f"☕ DESCANSO · {self.current_pomodoro}/{total}")
            self._status_color = "#FDA4AF"
            self._timer_color = "#F87171"
            self.btn_start.setText("⏸" if self.running else "▶")
        elif self.phase == "LONG_BREAK":
            self.status.setText(f"🌙 DESCANSO LARGO · {self.current_pomodoro}/{total}")
            self._status_color = "#FDA4AF"
            self._timer_color = "#F87171"
            self.btn_start.setText("⏸" if self.running else "▶")
        elif self.phase == "DONE":
            self.status.setText("🎉 COMPLETADO")
            self._status_color = "#7CFFB2"
            self._timer_color = "#4ADE80"
            self.btn_start.setText("↺")
        else:
            self.status.setText("")
            self._timer_color = "white"
            self.btn_start.setText("▶")

        self._paint_timer()
        self._paint_status()
        self._sync_status_vis()
        self.adjustSize()
        self._layout_overlays()
        self._geo_check()


    def open_settings(self):
        was_running = self.running
        self.tick.stop()
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            keep = {"pos_x": self.x(), "pos_y": self.y(), "video_w": self.video_w}
            self.cfg.update(dlg.values())
            self.cfg.update(keep)
            save_config(self.cfg)
            self._ensure_tracker(self.cfg.get("speed_auto", False))
            self._speed_warned = False
            if not self.cfg.get("speed_auto"):
                self._apply_manual_speed()
            if self.phase == "IDLE":
                self.remaining = self.cfg["focus_min"] * 60
            if self._is_zero_break(self.phase) and not self.running:
                self._advance(auto=True)
            else:
                self._refresh_ui()
        if was_running and self.phase != "DONE":
            self.tick.start()

    def _save_geom(self):
        self.cfg["pos_x"] = self.x()
        self.cfg["pos_y"] = self.y()
        self.cfg["video_w"] = self.video_w
        save_config(self.cfg)

    def close_app(self):
        self._stop_tracker_sync(timeout=2.0)
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        self._save_geom()
        QApplication.quit()

    def closeEvent(self, ev):
        self._stop_tracker_sync(timeout=2.0)
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        self._save_geom()
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    lock = QLockFile(QDir.tempPath() + "/GatoFlow.lock")
    if not lock.tryLock(100):
        QMessageBox.warning(None, "GatoFlow", "GatoFlow ya está abierto.")
        return
    cfg = load_config()
    w = GatoWidget(cfg)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
