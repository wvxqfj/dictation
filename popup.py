"""Основа всплывающих плашек у выделенного текста (меню переписывания, перевод): тёмная плашка
с тенью и кнопками, layered-окно с попиксельной прозрачностью. Окно не забирает фокус, поэтому
выделение в поле остаётся. Всё окно живёт в своём потоке; API можно звать из любого."""
import ctypes
import ctypes.wintypes as wt
import logging
import math
import os
import queue
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from winapi import BITMAPINFO, BLENDFUNCTION, MONITORINFO, WNDCLASSEXW, WNDPROC, gdi32, kernel32, shcore, user32

log = logging.getLogger("dictation")

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x8, 0x80, 0x8000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
WM_APP, WM_TIMER = 0x8000, 0x0113
WM_MOUSEACTIVATE, WM_SETCURSOR, WM_MOUSEMOVE, WM_MOUSELEAVE = 0x0021, 0x0020, 0x0200, 0x02A3
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
MA_NOACTIVATE = 3
TME_LEAVE = 2
IDC_ARROW, IDC_HAND = 32512, 32649
ULW_ALPHA = 2
HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

# логические размеры (умножаются на DPI-масштаб)
SHADOW = 14           # поле под тень
RADIUS = 14
BTN_R = 9
GAP_TO_TEXT = 6       # от выделения до плашки
SS = 3                # суперсэмплинг для сглаживания

PANEL = (30, 30, 33, 248)
RIM = (255, 255, 255, 40)
LABEL = (152, 152, 160, 255)
BTN = (48, 48, 52, 255)
BTN_HOVER = (64, 64, 70, 255)
BTN_DOWN = (80, 80, 88, 255)
ACCENT = (10, 132, 255)
INK = (240, 240, 245, 255)


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD), ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]


user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wt.HANDLE
user32.SetCursor.argtypes = [wt.HANDLE]
user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
user32.SetWindowPos.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetForegroundWindow.restype = wt.HWND


def font(names, size):
    for name in names:
        try:
            return ImageFont.truetype(os.path.join(os.environ["WINDIR"], "Fonts", name), size)
        except OSError:
            pass
    return ImageFont.load_default()


def bold_font(size):
    return font(("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"), size)


def regular_font(size):
    return font(("segoeui.ttf", "arial.ttf"), size)


def cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


class Popup:
    """Наследник задаёт _build(s) — раскладку для масштаба s: self.layout = {"scale", "size" (окно, px),
    "panel" (ширина, высота плашки, лог. px), "buttons": [(ключ, подпись, (x0, y0, x1, y1))], "font"} и
    вызывает self._make_base(...); по желанию — _draw(d, k, o) (своё содержимое поверх плашки),
    _button_style(i) и _clicked(i)."""
    CLASS = "DictationPopup"
    IDLE_HIDE = None      # без дела столько секунд — прячемся (None — не прячемся)

    def __init__(self):
        self.cmds = queue.Queue()
        self.hwnd = None
        self.visible = False
        self.token = 0
        self.rect = (0, 0, 0, 0)       # плашка на экране — для хука мыши (щелчок мимо её закрывает)
        self.target = None             # окно, в котором выделен текст
        self.anchor = None
        self.hover = self.pressed = None
        self.shown_at = self.painted = 0.0
        self.alpha = 0.0
        self.tracking = False
        self.layout = None
        self.base = None
        self.monitor = None
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name=self.CLASS).start()

    # --- API (из любого потока)

    def show(self, anchor, target):
        """anchor — (left, top, right, bottom) выделения на экране или None (тогда у курсора мыши)."""
        self.token += 1
        self.visible = True    # сразу: хук мыши и клавиатуры уже может закрыть плашку
        self._send("show", (self.token, anchor, target))
        return self.token

    def hide(self, token=None):
        if token is None or token == self.token:
            self.visible = False   # сразу: хук и App не должны считать плашку открытой
            self._send("hide", None)

    def active(self, token):
        return self.visible and self.token == token

    def contains(self, x, y):
        l, t, r, b = self.rect
        return l <= x < r and t <= y < b

    def _send(self, cmd, arg):
        self.cmds.put((cmd, arg))
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP, 0, 0)

    # --- для наследников

    def _reset(self):
        """Состояние наследника перед новым показом."""

    def _command(self, cmd, arg):
        """Свои команды наследника (из _send)."""

    def _animating(self):
        """True — перерисовывать ~20 раз в секунду (что-то пульсирует)."""
        return False

    def _button_style(self, i):
        if i == self.pressed:
            return BTN_DOWN, INK
        if i == self.hover:
            return BTN_HOVER, INK
        return BTN, INK

    def _clicked(self, i):
        pass

    def _draw(self, d, k, o):
        pass

    def _make_base(self, s, width, height, draw=None):
        """Тень и плашка width×height (лог. px) с еле заметным светлым кантом; draw(d, k, o) рисует
        неподвижные подписи. Возвращает размер окна в px."""
        W = int(math.ceil((width + 2 * SHADOW) * s))
        H = int(math.ceil((height + 2 * SHADOW) * s))
        k = s * SS
        o = SHADOW * k
        mask = Image.new("L", (W * SS, H * SS), 0)
        ImageDraw.Draw(mask).rounded_rectangle((o, o + 3 * k, o + width * k, o + height * k + 3 * k),
                                               RADIUS * k, fill=110)
        mask = mask.resize((W, H), Image.BOX).filter(ImageFilter.GaussianBlur(7 * s))
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        base.putalpha(mask)
        panel = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(panel)
        d.rounded_rectangle((o, o, o + width * k, o + height * k), RADIUS * k, fill=RIM)
        d.rounded_rectangle((o + k, o + k, o + width * k - k, o + height * k - k), RADIUS * k - k, fill=PANEL)
        if draw:
            draw(d, k, o)
        base.alpha_composite(panel.resize((W, H), Image.LANCZOS))
        self.base = base
        return W, H

    def _relayout(self):
        """Содержимое поменяло размер — перестроить, поставить на место и перерисовать."""
        if not self.visible or not self.monitor:
            return
        self._build(self.monitor[1])
        self._place()
        self._paint()

    # --- окно

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS
        user32.RegisterClassExW(ctypes.byref(wc))
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, self.CLASS, "Диктовка", WS_POPUP,
                                           0, 0, 10, 10, None, None, hinst, None)
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        self.dib = self.dib_size = None
        self.cur_arrow = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        self.cur_hand = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _proc(self, hwnd, m, wp, lp):
        try:
            if m == WM_APP:
                self._drain()
                return 0
            if m == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE   # щелчок не забирает фокус у поля с выделением
            if m == WM_SETCURSOR:
                user32.SetCursor(self.cur_hand if self.hover is not None else self.cur_arrow)
                return 1
            if m == WM_MOUSEMOVE:
                if not self.tracking:
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                    user32.TrackMouseEvent(ctypes.byref(tme))
                    self.tracking = True
                self._set_hover(self._hit(lp))
                return 0
            if m == WM_MOUSELEAVE:
                self.tracking = False
                self.pressed = None
                self._set_hover(None)
                return 0
            if m == WM_LBUTTONDOWN:
                self.pressed = self._hit(lp)
                self._paint()
                return 0
            if m == WM_LBUTTONUP:
                down, self.pressed = self.pressed, None
                hit = self._hit(lp)
                if hit is not None and hit == down:
                    self._clicked(hit)
                self._paint()
                return 0
            if m == WM_TIMER:
                self._tick()
                return 0
        except Exception:
            log.exception("плашка %s", self.CLASS)
        return user32.DefWindowProcW(hwnd, m, wp, lp)

    def _drain(self):
        while True:
            try:
                cmd, arg = self.cmds.get_nowait()
            except queue.Empty:
                return
            if cmd == "show":
                self._show(*arg)
            elif cmd == "hide":
                if not self.visible:
                    user32.KillTimer(self.hwnd, 1)
                    user32.ShowWindow(self.hwnd, SW_HIDE)
                    self.rect = (0, 0, 0, 0)
            else:
                self._command(cmd, arg)

    def _show(self, token, anchor, target):
        if token != self.token or not self.visible:
            return   # плашку успели закрыть, пока команда шла до окна
        self.target, self.anchor = target, anchor
        self.hover = self.pressed = None
        self._reset()
        ax, ay = (anchor[0], anchor[3]) if anchor else cursor_pos()
        hmon = user32.MonitorFromPoint(wt.POINT(int(ax), int(ay)), 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        dx, dy = wt.UINT(96), wt.UINT(96)
        try:
            shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
        except Exception:
            pass
        wa = mi.rcWork
        self.monitor = ((wa.left, wa.top, wa.right, wa.bottom), dx.value / 96.0)
        self.cursor = cursor_pos()
        self._build(self.monitor[1])
        self._place()
        self.alpha = 0.0
        self.shown_at = time.time()
        self._paint()
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        user32.SetTimer(self.hwnd, 1, 16, None)

    def _place(self):
        """Под выделением, а если не влезает — над ним; выделение больше экрана — у курсора мыши."""
        (wl, wt_, wr, wb), s = self.monitor
        W, H = self.layout["size"]
        sh, gap = int(SHADOW * s), int(GAP_TO_TEXT * s)
        anchor = self.anchor
        if anchor:
            l, t, r, b = (int(v) for v in anchor)
            x = l - sh
            y = b + gap - sh
            if y + H - sh > wb:
                y = t - gap - H + sh
            if y + sh < wt_ or y + H - sh > wb:
                anchor = None
        if not anchor:
            cx, cy = self.cursor
            x, y = cx - sh, cy + int(18 * s) - sh
            if y + H - sh > wb:
                y = cy - int(8 * s) - H + sh
        x = max(wl - sh, min(x, wr - W + sh))
        y = max(wt_ - sh, min(y, wb - H + sh))
        self.pos = (x, y)
        self.rect = (x + sh, y + sh, x + W - sh, y + H - sh)

    def _tick(self):
        if not self.visible:
            return
        if user32.GetForegroundWindow() != self.target or (
                self.IDLE_HIDE and not self._animating() and time.time() - self.shown_at > self.IDLE_HIDE):
            self.visible = False
            self._send("hide", None)
            return
        if self.alpha < 1.0:
            self.alpha = min(1.0, self.alpha + 0.016 / 0.09)   # проявляется за ~0,1 с
            self._paint(render=False)
        if self._animating() and time.time() - self.painted > 0.05:
            self._paint()

    # --- отрисовка

    def _hit(self, lp):
        if not self.layout:
            return None
        x = ctypes.c_short(lp & 0xFFFF).value
        y = ctypes.c_short((lp >> 16) & 0xFFFF).value
        s = self.layout["scale"]
        lx, ly = x / s - SHADOW, y / s - SHADOW
        for i, (_, _, (x0, y0, x1, y1)) in enumerate(self.layout["buttons"]):
            if x0 <= lx < x1 and y0 <= ly < y1:
                return i
        return None

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self._paint()

    def _frame(self):
        L = self.layout
        s, (W, H) = L["scale"], L["size"]
        k = s * SS
        o = SHADOW * k
        layer = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        self._draw(d, k, o)
        for i, (_, label, (x0, y0, x1, y1)) in enumerate(L["buttons"]):
            fill, ink = self._button_style(i)
            box = (o + x0 * k, o + y0 * k, o + x1 * k, o + y1 * k)
            d.rounded_rectangle(box, BTN_R * k, fill=fill)
            d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label, font=L["font"], fill=ink, anchor="mm")
        img = self.base.copy()
        img.alpha_composite(layer.resize((W, H), Image.LANCZOS))
        rgba = np.asarray(img.convert("RGBa"))
        return np.ascontiguousarray(rgba[..., [2, 1, 0, 3]])

    def _paint(self, render=True):
        if not self.visible or not self.layout:
            return
        if render or self.dib is None:
            self._render()
        w_, h = self.dib_size
        blend = BLENDFUNCTION(0, 0, max(0, min(255, int(self.alpha * 255))), 1)
        user32.UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(wt.POINT(*self.pos)),
                                   ctypes.byref(wt.SIZE(w_, h)), self.mem_dc, ctypes.byref(wt.POINT(0, 0)),
                                   0, ctypes.byref(blend), ULW_ALPHA)

    def _render(self):
        self.painted = time.time()
        bgra = self._frame()
        h, w_ = bgra.shape[:2]
        if self.dib_size != (w_, h):
            if self.dib:
                gdi32.DeleteObject(self.dib)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(bmi.bmiHeader)
            bmi.bmiHeader.biWidth = w_
            bmi.bmiHeader.biHeight = -h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            self.bits = ctypes.c_void_p()
            self.dib = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
            gdi32.SelectObject(self.mem_dc, self.dib)
            self.dib_size = (w_, h)
        ctypes.memmove(self.bits, bgra.ctypes.data, bgra.nbytes)
