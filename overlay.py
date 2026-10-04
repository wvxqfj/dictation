"""Всплывающий индикатор диктовки в стиле Dynamic Island: чёрная капсула у верхнего
(или нижнего) края экрана раскрывается из точки на пружинах, внутри — микрофон и волна голоса
(layered-окно с попиксельной прозрачностью)."""
import ctypes
import ctypes.wintypes as wt
import math
import os
import queue
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from winapi import (BITMAPINFO, BLENDFUNCTION, WNDCLASSEXW, WNDPROC, foreground_monitor,
                    gdi32, kernel32, user32)

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x80000, 0x20, 0x8
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x8000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
WM_APP = 0x8000
ULW_ALPHA = 2

# логические размеры (умножаются на DPI-масштаб)
PILL_W, PILL_H = 200, 44
DOT = 16              # капсула рождается из точки такого размера и в неё же схлопывается
TOP_GAP = 8           # сверху: капсула парит под краем экрана, как остров под камерой
BOTTOM_GAP = 2        # снизу: от нижнего края экрана (поверх панели задач)
PAD = 22              # поле под тень и «перелёт» пружины
SS = 4                # суперсэмплинг для сглаживания

MIC_R = 14            # круг микрофона концентричен левому скруглению капсулы
BARS = 22
BAR_W, BAR_GAP = 3, 3
WAVE_H = 28
WAVE_RPAD = 20        # от правого края капсулы до волны

SHADOW_A, SHADOW_BLUR, SHADOW_DY = 0.5, 7, 3
RIM_A = 0.09          # еле заметный светлый кант, чтобы чёрная капсула не терялась на тёмном фоне

REC_RED = (255, 69, 58)
EN_BLUE = (10, 132, 255)

HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
user32.SetWindowPos.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]


class Spring:
    """Затухающая пружина в терминах SwiftUI: response — период (с), damping — доля затухания
    (1 — без перелёта, меньше — с отскоком). Перенацеливается на лету без рывков."""

    def __init__(self, x, response=0.5, damping=1.0):
        self.x, self.v, self.target = float(x), 0.0, float(x)
        self.response, self.damping = response, damping

    def to(self, target, response=None, damping=None):
        self.target = float(target)
        if response:
            self.response = response
        if damping:
            self.damping = damping

    def snap(self, x):
        self.x, self.v, self.target = float(x), 0.0, float(x)

    def step(self, dt):
        w = 2 * math.pi / self.response
        k, c = w * w, 2 * self.damping * w
        n = max(1, math.ceil(dt * 480))
        h = dt / n
        for _ in range(n):
            self.v += (-k * (self.x - self.target) - c * self.v) * h
            self.x += self.v * h


class Overlay:
    def __init__(self, position=lambda: "top"):
        self.cmds = queue.Queue()
        self.position = position  # () -> "top" | "bottom", читается при каждом показе
        self.pos = "top"
        self.level = 0.0
        self.mode = "native"
        self.phase = "hidden"     # listening | finishing | hidden
        self.target = 0.0         # 1 — показан, 0 — убирается
        self.scale = None
        self.cache = {}
        self.shape_key = self.shape = None
        self.history = [0.0] * BARS
        self.heights = [0.0] * BARS
        self.last_push = 0.0
        self.peak = 0.0
        self.glow = 0.0
        self.alpha = 0.0          # общая прозрачность окна
        self.w = Spring(DOT)      # размеры капсулы
        self.h = Spring(DOT)
        self.content = Spring(0)  # 0..1: проявление микрофона и волны
        self.badge = Spring(0)    # 0..1: значок EN
        self.accent = np.array(REC_RED, np.float32)
        self.content_at = 0.0     # когда начать проявлять содержимое
        self.hwnd = None
        threading.Thread(target=self._run, daemon=True, name="overlay").start()

    # --- API (из любого потока)

    def show(self, mode):
        self._send("show", mode)

    def set_mode(self, mode):
        self._send("mode", mode)

    def finishing(self):
        self._send("finishing", None)

    def hide(self):
        self._send("hide", None)

    def _send(self, cmd, arg):
        self.cmds.put((cmd, arg))
        # будим поток окна, если он ждёт в GetMessage
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP, 0, 0)

    def set_level(self, v):
        self.level = v

    # --- окно

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "DictationOverlay"
        user32.RegisterClassExW(ctypes.byref(wc))
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, "DictationOverlay", "Dictation", WS_POPUP,
                                           0, 0, 10, 10, None, None, hinst, None)
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        self.dib = None
        self.dib_size = None

    def _ensure_dib(self, w, h):
        if self.dib_size == (w, h):
            return
        if self.dib:
            gdi32.DeleteObject(self.dib)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(bmi.bmiHeader)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        self.bits = ctypes.c_void_p()
        self.dib = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0,
                                          ctypes.byref(self.bits), None, 0)
        gdi32.SelectObject(self.mem_dc, self.dib)
        self.dib_size = (w, h)

    def _blit(self, bgra, x, y, alpha):
        """bgra — premultiplied uint8 (h, w, 4)."""
        h, w = bgra.shape[:2]
        self._ensure_dib(w, h)
        bgra = np.ascontiguousarray(bgra)
        ctypes.memmove(self.bits, bgra.ctypes.data, bgra.nbytes)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, int(alpha * 255))), 1)
        user32.UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(wt.POINT(x, y)),
                                   ctypes.byref(wt.SIZE(w, h)), self.mem_dc,
                                   ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)

    # --- отрисовка

    def _font(self, size):
        for name in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"):
            try:
                return ImageFont.truetype(os.path.join(os.environ["WINDIR"], "Fonts", name), size)
            except OSError:
                pass
        return ImageFont.load_default()

    def _canvas(self):
        """Размер холста (px) и координаты центров пикселей по осям."""
        key = ("canvas", self.scale)
        if key not in self.cache:
            s = self.scale
            W = int(math.ceil((PILL_W + 2 * PAD) * s))
            H = int(math.ceil((PILL_H + PAD + max(TOP_GAP, BOTTOM_GAP) + 6) * s))
            self.cache[key] = (W, H, np.arange(W, dtype=np.float32) + 0.5,
                               np.arange(H, dtype=np.float32) + 0.5)
        return self.cache[key]

    def _mic_icon(self):
        """Красный круг с белым микрофоном (premultiplied); центр круга — в центре картинки."""
        key = ("mic", self.scale)
        if key not in self.cache:
            s = self.scale * SS
            n = int(math.ceil((MIC_R + 1) * 2 * self.scale)) * SS
            img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            c = n / 2
            d.ellipse((c - MIC_R * s, c - MIC_R * s, c + MIC_R * s, c + MIC_R * s), fill=REC_RED)
            w = 3.2 * s
            d.rounded_rectangle((c - w, c - 9 * s, c + w, c + 2.5 * s), w, fill="white")
            lw = max(1, int(1.7 * s))
            d.arc((c - 6.5 * s, c - 5.5 * s, c + 6.5 * s, c + 6.5 * s), 0, 180, fill="white", width=lw)
            d.line((c, c + 6.5 * s, c, c + 9.5 * s), fill="white", width=lw)
            self.cache[key] = img.resize((n // SS, n // SS), Image.LANCZOS).convert("RGBa")
        return self.cache[key]

    def _en_badge(self):
        key = ("en", self.scale)
        if key not in self.cache:
            s = self.scale * SS
            bw, bh = 24, 15
            img = Image.new("RGBA", (int(bw * s), int(bh * s)), (0, 0, 0, 0))
            ImageDraw.Draw(img).rounded_rectangle((0, 0, bw * s - 1, bh * s - 1), bh * s / 2,
                                                  fill=(0, 0, 0, 255))
            b = 2 * s  # чёрная окантовка отделяет значок от круга микрофона
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((b, b, bw * s - 1 - b, bh * s - 1 - b), (bh * s - 2 * b) / 2,
                                fill=EN_BLUE)
            d.text((bw * s / 2, bh * s / 2 + 0.3 * s), "EN", font=self._font(int(8.5 * s)),
                   fill="white", anchor="mm")
            img = img.resize((int(bw * self.scale), int(bh * self.scale)), Image.LANCZOS)
            self.cache[key] = img.convert("RGBa")
        return self.cache[key]

    @staticmethod
    def _sdf(xs, ys, cx, cy, w, h):
        """Расстояние до капсулы w×h с центром (cx, cy); xs, ys — координаты по осям."""
        r = min(w, h) / 2
        qx = np.abs(xs - cx) - (w / 2 - r)
        qy = np.abs(ys - cy) - (h / 2 - r)
        d = np.hypot(np.maximum(qx, 0)[None, :], np.maximum(qy, 0)[:, None])
        d += np.minimum(np.maximum(qx[None, :], qy[:, None]), 0)
        return d - r

    def _shape(self, w, h, cy):
        """Тень, капсула и кант: покрытие капсулы, premultiplied яркость и альфа, готовый кадр.
        Пока пружины стоят, берётся из кэша."""
        key = (self.scale, round(w, 2), round(h, 2), round(cy, 2))
        if self.shape_key != key:
            s = self.scale
            W, H, xs, ys = self._canvas()
            d = self._sdf(xs, ys, W / 2, cy, w, h)
            body = np.clip(0.5 - d, 0, 1)
            ds = self._sdf(xs, ys, W / 2, cy + SHADOW_DY * s, w, h)
            # размытая тень: логистическая аппроксимация гауссова размытия по расстоянию
            shadow = SHADOW_A / (1 + np.exp(np.clip(ds * (1.7 / (SHADOW_BLUR * s)), -30, 30)))
            rim = (body - np.clip(0.5 - (d + max(1.0, s)), 0, 1)) * RIM_A
            a = rim + (body + shadow * (1 - body)) * (1 - rim)
            frame = np.empty((H, W, 4), np.uint8)
            frame[..., :3] = (rim * 255 + 0.5)[..., None]  # белый кант поверх чёрного
            frame[..., 3] = a * 255 + 0.5
            self.shape = (body, rim, a, frame)
            self.shape_key = key
        return self.shape

    def _bars(self, t):
        """Высота (лог. px) и непрозрачность каждого столбика волны."""
        i = np.arange(BARS, dtype=np.float32)
        if self.phase == "finishing":
            # фазу приводим заранее: во float32 большое t теряет точность
            h = BAR_W + (0.5 + 0.5 * np.sin((t * 7) % (2 * math.pi) - i * 0.45)) * 7
            a = 150 / 255
        else:
            h = BAR_W + np.array(self.heights, np.float32) * (WAVE_H - BAR_W)
            a = 245 / 255
        # самые старые столбики слева тают
        return h.astype(np.float32), (a * np.minimum(1.0, (i + 1) / 6)).astype(np.float32)

    @staticmethod
    def _place(img, size, cx, cy, scale):
        """img (premultiplied) центром в (cx, cy) на прозрачном поле size, с масштабом
        и субпиксельной точностью -> float32 (h, w, 4)."""
        iw, ih = img.size
        inv = 1 / scale
        # обратное преобразование: точка поля -> точка картинки
        data = (inv, 0, iw / 2 - cx * inv, 0, inv, ih / 2 - cy * inv)
        moved = img.transform(size, Image.AFFINE, data, resample=Image.BILINEAR)
        return np.asarray(moved, dtype=np.float32) / 255

    def _render(self, t):
        """Кадр: premultiplied BGRA uint8."""
        s = self.scale
        W, H, xs, ys = self._canvas()
        w = max(self.w.x, 1.0) * s
        h = max(self.h.x, 1.0) * s
        cy = TOP_GAP * s + h / 2 if self.pos == "top" else H - BOTTOM_GAP * s - h / 2
        body, rim, alpha, frame = self._shape(w, h, cy)
        k = min(max(self.content.x, 0.0), 1.0)
        if k <= 0.01:
            return frame

        # содержимое рисуется только в прямоугольнике капсулы
        x0, x1 = max(0, int(W / 2 - w / 2)), min(W, math.ceil(W / 2 + w / 2))
        y0, y1 = max(0, int(cy - h / 2)), min(H, math.ceil(cy + h / 2))
        xs, ys = xs[x0:x1], ys[y0:y1]
        body, rim, alpha = body[y0:y1, x0:x1], rim[y0:y1, x0:x1], alpha[y0:y1, x0:x1]
        zoom = 0.6 + 0.4 * k
        mic_x = W / 2 - w / 2 + h / 2  # концентрично левому скруглению
        accent = self.accent / 255

        # волна прижата к правому краю капсулы; считается только в своей полосе
        bw, pitch = BAR_W * s * zoom, (BAR_W + BAR_GAP) * s * zoom
        left = W / 2 + w / 2 - WAVE_RPAD * s - (BARS * pitch - BAR_GAP * s * zoom)
        ca = np.zeros((y1 - y0, x1 - x0), np.float32)
        wx = max(0, int(left) - 1 - x0)
        wy0 = max(0, int(cy - WAVE_H * s * zoom / 2) - 1 - y0)
        wy1 = max(wy0, min(y1, math.ceil(cy + WAVE_H * s * zoom / 2) + 1) - y0)
        heights, opacity = self._bars(t)
        bx = xs[wx:]
        idx = np.clip(np.rint((bx - left - bw / 2) / pitch), 0, BARS - 1).astype(np.intp)
        half = heights[idx] * np.float32(s * zoom / 2) - np.float32(bw / 2)  # прямой участок
        dx = np.abs(bx - (np.float32(left + bw / 2) + idx.astype(np.float32) * np.float32(pitch)))
        dy = np.maximum(np.abs(ys[wy0:wy1] - np.float32(cy))[:, None] - half[None, :], 0)
        ca[wy0:wy1, wx:] = (np.clip(np.float32(0.5 + bw / 2) - np.hypot(dx[None, :], dy), 0, 1)
                            * opacity[idx][None, :])
        crgb = ca[..., None] * accent

        icon = self._place(self._mic_icon(), (x1 - x0, y1 - y0), mic_x - x0, cy - y0, zoom)
        if self.badge.x > 0.01:
            badge = self._place(self._en_badge(), (x1 - x0, y1 - y0), mic_x - x0 + 12 * s * zoom,
                                cy - y0 + 10 * s * zoom, zoom * self.badge.x)
            icon = badge + icon * (1 - badge[..., 3:])
        crgb = icon[..., :3] + crgb * (1 - icon[..., 3:])
        ca = icon[..., 3] + ca * (1 - icon[..., 3])

        if k < 0.97:
            # проявление из размытия; размываем premultiplied, поэтому каналы независимы
            px = Image.fromarray((np.dstack([crgb, ca]) * 255 + 0.5).astype(np.uint8))
            px = np.asarray(px.filter(ImageFilter.GaussianBlur((1 - k) * 5 * s)), np.float32) / 255
            crgb, ca = px[..., :3], px[..., 3]
        m = body * k  # содержимое обрезается по форме капсулы
        crgb, ca = crgb * m[..., None], ca * m

        rgb, a = rim, alpha  # под содержимым: белый кант на чёрном (яркость одна на все каналы)
        if self.glow > 0.01:
            # свечение вокруг микрофона дышит в такт голосу
            gd = np.hypot((xs - np.float32(mic_x))[None, :], (ys - np.float32(cy))[:, None])
            ga = np.clip(np.float32((MIC_R + 1 + 4 * self.glow) * s * zoom + 0.5) - gd, 0, 1)
            ga *= body * np.float32(0.35 * self.glow * k)
            rgb = accent * ga[..., None] + (rim * (1 - ga))[..., None]
            a = ga + a * (1 - ga)
        else:
            rgb = rgb[..., None]
        rgb = crgb + rgb * (1 - ca[..., None])
        a = ca + a * (1 - ca)

        frame = frame.copy()
        roi = frame[y0:y1, x0:x1]
        roi[..., 0] = rgb[..., 2] * 255 + 0.5
        roi[..., 1] = rgb[..., 1] * 255 + 0.5
        roi[..., 2] = rgb[..., 0] * 255 + 0.5
        roi[..., 3] = a * 255 + 0.5
        return frame

    def _update_wave(self, now, dt):
        # огибающая: быстро растёт, медленно спадает
        lv = self.level if self.phase == "listening" else 0.0
        self.peak = lv if lv > self.peak else self.peak * math.exp(-dt / 0.12)
        if now - self.last_push > 0.045:
            self.last_push = now
            self.history = self.history[1:] + [self.peak]
        k = 1 - math.exp(-dt / 0.05)
        for i in range(BARS):
            self.heights[i] += (self.history[i] - self.heights[i]) * k
        self.glow += (self.peak - self.glow) * (1 - math.exp(-dt / 0.08))

    def _step(self, now, dt):
        """Физика: пружины формы, проявление содержимого, общая прозрачность."""
        if self.target and now >= self.content_at:
            self.content.to(1)
        if self.target:
            self.alpha = min(1.0, self.alpha + dt / 0.08)
        elif self.content.x < 0.3:
            # прозрачность следует за размером: капсула тает, сжимаясь в точку
            self.alpha = min(self.alpha, max(0.0, (self.w.x - DOT) / (PILL_H * 1.5 - DOT)))
        for sp in (self.w, self.h, self.content, self.badge):
            sp.step(dt)
        tgt = np.array(EN_BLUE if self.mode == "en" else REC_RED, np.float32)
        self.accent += (tgt - self.accent) * (1 - math.exp(-dt / 0.1))
        self._update_wave(now, dt)

    # --- цикл

    def _expand(self, now):
        # ширина мягче и с отскоком, высота жёстче — капсула «перетекает», как остров у iPhone
        self.w.to(PILL_W, 0.55, 0.68)
        self.h.to(PILL_H, 0.42, 0.75)
        self.content.to(self.content.target, 0.38, 0.9)
        # пока форма маленькая, содержимое ждёт, чтобы проявиться уже в раскрытой капсуле
        self.content_at = now + (0.12 if self.w.x < PILL_W * 0.6 else 0.0)

    def _collapse(self):
        self.content.to(0, 0.16, 1.0)
        # цель меньше точки: размер проходит DOT за конечное время, и капсула исчезает без «хвоста»
        self.w.to(DOT / 2, 0.36, 0.95)
        self.h.to(DOT / 2, 0.32, 0.95)

    def _handle(self, cmd, arg):
        now = time.perf_counter()
        if cmd == "show":
            self.mode = arg
            self.phase = "listening"
            self.target = 1.0
            self.history = [0.0] * BARS
            self.heights = [0.0] * BARS
            if self.alpha <= 0.0:
                self.rc, self.scale = foreground_monitor()
                self.pos = "bottom" if self.position() == "bottom" else "top"
                for sp in (self.w, self.h):
                    sp.snap(DOT)
                self.content.snap(0)
                self.accent = np.array(EN_BLUE if arg == "en" else REC_RED, np.float32)
                self.badge.snap(1 if arg == "en" else 0)
                user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.badge.to(1 if arg == "en" else 0, 0.4, 0.55)
            self._expand(now)
            # другие «поверх всех» окна (панель задач, плееры) могли оказаться выше — поднимаемся
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        elif cmd == "mode":
            if arg != self.mode:
                self.mode = arg
                self.badge.to(1 if arg == "en" else 0, 0.4, 0.55)
                self.w.v += 90  # лёгкий «толчок» капсулы при смене режима
        elif cmd == "finishing":
            if self.phase == "listening":
                self.phase = "finishing"
        elif cmd == "hide":
            self.target = 0.0
            self._collapse()

    def _warmup(self):
        self.rc, self.scale = foreground_monitor()
        self.pos = "bottom" if self.position() == "bottom" else "top"
        self.w.snap(PILL_W)
        self.h.snap(PILL_H)
        self.content.snap(0.5)
        self.badge.snap(1)
        self._render(0.0)
        self.w.snap(DOT)
        self.h.snap(DOT)
        self.content.snap(0)
        self.badge.snap(0)

    def _run(self):
        self._create_window()
        try:
            self._warmup()
        except Exception:
            pass
        msg = wt.MSG()
        last = time.perf_counter()
        while True:
            if self.target == 0.0 and self.alpha <= 0.0:
                if self.phase != "hidden":
                    self.phase = "hidden"
                    user32.ShowWindow(self.hwnd, SW_HIDE)
                # ждём команду, продолжая разбирать сообщения окна: если просто висеть на очереди,
                # Windows на первой же широковещательной рассылке (питание, дисплей, настройки)
                # сочтёт процесс зависшим и закроет его
                try:
                    cmd, arg = self.cmds.get_nowait()
                except queue.Empty:
                    if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                        user32.TranslateMessage(ctypes.byref(msg))
                        user32.DispatchMessageW(ctypes.byref(msg))
                    continue
                self._handle(cmd, arg)
                last = time.perf_counter()
            while True:
                try:
                    cmd, arg = self.cmds.get_nowait()
                except queue.Empty:
                    break
                self._handle(cmd, arg)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            now = time.perf_counter()
            dt = min(now - last, 0.1)
            last = now
            self._step(now, dt)

            try:
                frame = self._render(now)
                H, W = frame.shape[:2]
                rc = self.rc
                x = (rc.left + rc.right) // 2 - W // 2
                # окно не вылезает за край монитора (там может быть другой монитор)
                y = rc.top if self.pos == "top" else rc.bottom - H
                self._blit(frame, x, y, self.alpha)
            except Exception:
                import logging
                logging.getLogger("dictation").exception("overlay")
            time.sleep(max(0.0, 1 / 60 - (time.perf_counter() - now)))


if __name__ == "__main__":
    # демонстрация без диктовки: python overlay.py
    from winapi import set_dpi_aware
    set_dpi_aware()
    ov = Overlay()
    for mode in ("native", "en"):
        time.sleep(0.6)
        ov.show(mode)
        t0 = time.time()
        while time.time() - t0 < 3.5:
            t = time.time() - t0
            ov.set_level(max(0.0, 0.55 + 0.4 * math.sin(t * 9) * math.sin(t * 2.3)))
            time.sleep(0.02)
        ov.set_level(0.0)
        ov.finishing()
        time.sleep(1.2)
        ov.hide()
        time.sleep(1.0)
