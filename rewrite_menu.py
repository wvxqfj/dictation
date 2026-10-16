"""Меню переписывания выделенного текста: тёмная плашка рядом с выделением, все варианты видны
сразу и срабатывают одним щелчком. Окно не забирает фокус, поэтому выделение в поле остаётся."""
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

import rewrite
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
PAD = 8               # от края плашки до кнопок
RADIUS = 14
BTN_H, BTN_PAD, BTN_R = 30, 12, 9
GAP, ROW_GAP = 6, 6
LABEL_GAP = 10
GAP_TO_TEXT = 6       # от выделения до плашки
SS = 3                # суперсэмплинг для сглаживания
IDLE_HIDE = 30        # без дела столько секунд — прячемся

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


def _font(names, size):
    for name in names:
        try:
            return ImageFont.truetype(os.path.join(os.environ["WINDIR"], "Fonts", name), size)
        except OSError:
            pass
    return ImageFont.load_default()


def cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


class RewriteMenu:
    """on_pick(token, kind) вызывается из потока меню сразу после щелчка по варианту."""

    def __init__(self, on_pick):
        self.on_pick = on_pick
        self.cmds = queue.Queue()
        self.hwnd = None
        self.visible = False
        self.token = 0
        self.rect = (0, 0, 0, 0)       # окно на экране — для хука мыши (щелчок мимо меню его закрывает)
        self.target = None             # окно, в котором выделен текст
        self.hover = self.pressed = self.busy = None
        self.shown_at = self.painted = 0.0
        self.alpha = 0.0
        self.tracking = False
        self.layout = None
        self.base = None
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="rewrite-menu").start()

    # --- API (из любого потока)

    def show(self, anchor, target):
        """anchor — (left, top, right, bottom) выделения на экране или None (тогда у курсора мыши)."""
        self.token += 1
        self.visible = True    # сразу: хук мыши и клавиатуры уже может закрыть меню
        self._send("show", (self.token, anchor, target))
        return self.token

    def hide(self, token=None):
        if token is None or token == self.token:
            self.visible = False   # сразу: хук и App не должны считать меню открытым
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

    # --- окно

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "DictationRewriteMenu"
        user32.RegisterClassExW(ctypes.byref(wc))
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, "DictationRewriteMenu", "Переписать", WS_POPUP,
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
                user32.SetCursor(self.cur_hand if self.hover is not None and self.busy is None
                                 else self.cur_arrow)
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
                if hit is not None and hit == down and self.busy is None:
                    self.busy = hit
                    self._paint()
                    try:
                        self.on_pick(self.token, self.layout["buttons"][hit][0])
                    except Exception:
                        log.exception("меню переписывания")
                else:
                    self._paint()
                return 0
            if m == WM_TIMER:
                self._tick()
                return 0
        except Exception:
            log.exception("меню переписывания")
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

    def _show(self, token, anchor, target):
        if token != self.token or not self.visible:
            return   # меню успели закрыть, пока команда шла до окна
        self.target = target
        self.hover = self.pressed = self.busy = None
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
        s = dx.value / 96.0
        if not self.layout or self.layout["scale"] != s:
            self._build(s)
        W, H = self.layout["size"]
        sh, gap = int(SHADOW * s), int(GAP_TO_TEXT * s)
        wa = mi.rcWork
        if anchor:
            l, t, r, b = (int(v) for v in anchor)
            x = l - sh
            y = b + gap - sh                      # под выделением
            if y + H - sh > wa.bottom:
                y = t - gap - H + sh              # не влезает — над ним
            if y - sh < wa.top or y + H > wa.bottom + sh:
                anchor = None                     # выделение больше экрана — показываем у курсора
        if not anchor:
            cx, cy = cursor_pos()
            x, y = cx - sh, cy + int(18 * s) - sh
            if y + H - sh > wa.bottom:
                y = cy - int(8 * s) - H + sh
        x = max(wa.left - sh, min(x, wa.right - W + sh))
        y = max(wa.top - sh, min(y, wa.bottom - H + sh))
        self.pos = (x, y)
        self.rect = (x + sh, y + sh, x + W - sh, y + H - sh)
        self.alpha = 0.0
        self.shown_at = time.time()
        self._paint()
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        user32.SetTimer(self.hwnd, 1, 16, None)

    def _tick(self):
        if not self.visible:
            return
        fg = user32.GetForegroundWindow()
        if fg != self.target or (self.busy is None and time.time() - self.shown_at > IDLE_HIDE):
            self.visible = False
            self._send("hide", None)
            return
        if self.alpha < 1.0:
            self.alpha = min(1.0, self.alpha + 0.016 / 0.09)   # проявляется за ~0,1 с
            self._paint(render=False)
        if self.busy is not None and time.time() - self.painted > 0.05:
            self._paint()   # выбранный вариант пульсирует, пока текст переписывается

    # --- отрисовка

    def _build(self, s):
        """Раскладка кнопок и неподвижная часть картинки (тень, плашка, подписи) для масштаба s."""
        f_btn = _font(("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"), int(13 * s * SS))
        f_lab = _font(("segoeui.ttf", "arial.ttf"), int(12 * s * SS))
        labw = max(f_lab.getlength(name) for name, _ in rewrite.GROUPS) / SS / s
        rows, buttons = [], []
        y = PAD
        width = 0
        for name, items in rewrite.GROUPS:
            x = PAD + labw + LABEL_GAP
            for key, label, _ in items:
                bw = f_btn.getlength(label) / SS / s + 2 * BTN_PAD
                buttons.append((key, label, (x, y, x + bw, y + BTN_H)))
                x += bw + GAP
            rows.append((name, y))
            width = max(width, x - GAP + PAD)
            y += BTN_H + ROW_GAP
        height = y - ROW_GAP + PAD
        W = int(math.ceil((width + 2 * SHADOW) * s))
        H = int(math.ceil((height + 2 * SHADOW) * s))
        k = s * SS
        o = SHADOW * k
        # тень
        mask = Image.new("L", (W * SS, H * SS), 0)
        ImageDraw.Draw(mask).rounded_rectangle((o, o + 3 * k, o + width * k, o + height * k + 3 * k),
                                               RADIUS * k, fill=110)
        mask = mask.resize((W, H), Image.BOX).filter(ImageFilter.GaussianBlur(7 * s))
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        base.putalpha(mask)
        # плашка с еле заметным светлым кантом и подписи строк
        panel = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(panel)
        d.rounded_rectangle((o, o, o + width * k, o + height * k), RADIUS * k, fill=RIM)
        d.rounded_rectangle((o + k, o + k, o + width * k - k, o + height * k - k), RADIUS * k - k, fill=PANEL)
        for name, ry in rows:
            d.text((o + PAD * k, o + (ry + BTN_H / 2) * k), name, font=f_lab, fill=LABEL, anchor="lm")
        base.alpha_composite(panel.resize((W, H), Image.LANCZOS))
        self.layout = {"scale": s, "size": (W, H), "buttons": buttons, "font": f_btn}
        self.base = base

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
        pulse = 0.5 + 0.5 * math.sin(time.time() * 6)
        for i, (_, label, (x0, y0, x1, y1)) in enumerate(L["buttons"]):
            ink = INK
            if self.busy is not None:
                if i == self.busy:
                    fill = ACCENT + (int(170 + 85 * pulse),)
                else:
                    fill, ink = BTN[:3] + (130,), INK[:3] + (90,)
            elif i == self.pressed:
                fill = BTN_DOWN
            elif i == self.hover:
                fill = BTN_HOVER
            else:
                fill = BTN
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


if __name__ == "__main__":
    # посмотреть меню без программы: python rewrite_menu.py
    from winapi import set_dpi_aware
    logging.basicConfig(level=logging.INFO)
    set_dpi_aware()
    menu = RewriteMenu(lambda token, kind: (print("выбрано:", kind), threading.Timer(1.5, menu.hide).start()))
    menu.ready.wait()
    menu.show(None, user32.GetForegroundWindow())
    time.sleep(20)
