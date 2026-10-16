"""Пузырь с переводом выделенного текста: появляется рядом с выделением, пока идёт перевод —
«Перевожу…», потом сам перевод и кнопка «Копировать». Фокус не забирает, текст в окне не меняет."""
import logging
import math
import threading
import time

import popup
from popup import ACCENT, INK, LABEL

log = logging.getLogger("dictation")

# логические размеры (умножаются на DPI-масштаб)
PAD = 14
MIN_W, MAX_W = 240, 460
HEAD_H = 26           # строка заголовка с кнопкой «Копировать»
HEAD_GAP = 6
LINE_H = 21
BTN_PAD = 10
MAX_SCREEN_SHARE = 0.6   # выше этой доли экрана пузырь не растёт — длинный перевод обрезается «…»
COPIED_FOR = 1.5
TEXT = (236, 236, 241, 255)
ERROR = (255, 140, 130, 255)


def wrap(text, font, maxw):
    """Строки не шире maxw (px) по словам; слово длиннее строки режется по буквам."""
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            t = f"{cur} {word}" if cur else word
            if font.getlength(t) <= maxw:
                cur = t
                continue
            if cur:
                lines.append(cur)
            cur = word
            while font.getlength(cur) > maxw and len(cur) > 1:
                n = len(cur) - 1
                while n > 1 and font.getlength(cur[:n]) > maxw:
                    n -= 1
                lines.append(cur[:n])
                cur = cur[n:]
        lines.append(cur)
    return lines


class TranslateBubble(popup.Popup):
    """on_copy(text) — щелчок по «Копировать»."""
    CLASS = "DictationTranslate"

    def __init__(self, on_copy):
        self.on_copy = on_copy
        self.text = self.error = None
        self.title = "Перевожу…"
        self.copied_at = 0.0
        super().__init__()

    def set_result(self, token, text=None, title="", error=None):
        self._send("result", (token, text, title, error))

    def _reset(self):
        self.text = self.error = None
        self.title = "Перевожу…"
        self.copied_at = 0.0

    def _command(self, cmd, arg):
        if cmd == "result":
            token, text, title, error = arg
            if token == self.token and self.visible:
                self.text, self.title, self.error = text, title, error
                self._relayout()

    def _loading(self):
        return self.text is None and self.error is None

    def _animating(self):
        return self._loading() or (self.copied_at and time.time() - self.copied_at < COPIED_FOR + 0.1)

    def _clicked(self, i):
        if self.text:
            try:
                self.on_copy(self.text)
                self.copied_at = time.time()
            except Exception:
                log.exception("копирование перевода")

    def _button_style(self, i):
        fill, ink = super()._button_style(i)
        if self.copied_at and time.time() - self.copied_at < COPIED_FOR:
            fill = ACCENT + (255,)
        return fill, ink

    def _build(self, s):
        SS = popup.SS
        k = s * SS
        f_head = popup.regular_font(int(12 * s * SS))
        f_btn = popup.bold_font(int(12 * s * SS))
        f_text = popup.regular_font(int(14 * s * SS))
        self.f_text = f_text
        wl, wt_, wr, wb = self.monitor[0]
        body = self.error or self.text or ""
        buttons = []
        btn_w = (max(f_btn.getlength("Копировать"), f_btn.getlength("Скопировано")) / k + 2 * BTN_PAD
                 if self.text else 0)
        head_w = f_head.getlength(self.title) / k + (btn_w + 16 if btn_w else 0)
        if body:
            natural = max(f_text.getlength(p) for p in body.split("\n")) / k
            width = min(MAX_W, max(MIN_W, head_w + 2 * PAD, natural + 2 * PAD + 1))
            lines = wrap(body, f_text, (width - 2 * PAD) * k)
            max_lines = max(3, int(((wb - wt_) / s * MAX_SCREEN_SHARE - HEAD_H - HEAD_GAP - 2 * PAD) / LINE_H))
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                last = lines[-1]
                while last and f_text.getlength(last + "…") > (width - 2 * PAD) * k:
                    last = last[:-1]
                lines[-1] = last.rstrip() + "…"
            height = PAD + HEAD_H + HEAD_GAP + len(lines) * LINE_H + PAD - 4
        else:
            width, lines = max(MIN_W, head_w + 2 * PAD), []
            height = PAD + HEAD_H + PAD - 4
        if btn_w:
            buttons.append(("copy", "Копировать", (width - PAD - btn_w + 4, PAD - 4, width - PAD + 4, PAD - 4 + HEAD_H)))
        ink = ERROR if self.error else TEXT

        def static(d, k_, o):
            if self.text or self.error:
                d.text((o + PAD * k_, o + (PAD - 4 + HEAD_H / 2) * k_), self.title, font=f_head, fill=LABEL,
                       anchor="lm")
            for i, line in enumerate(lines):
                y = PAD + HEAD_H + HEAD_GAP + i * LINE_H + LINE_H / 2
                d.text((o + PAD * k_, o + y * k_), line, font=f_text, fill=ink, anchor="lm")
        size = self._make_base(s, width, height, static)
        self.layout = {"scale": s, "size": size, "buttons": buttons, "font": f_btn}

    def _draw(self, d, k, o):
        copied = self.copied_at and time.time() - self.copied_at < COPIED_FOR
        if self.layout["buttons"]:
            key, _, box = self.layout["buttons"][0]
            self.layout["buttons"][0] = (key, "Скопировано" if copied else "Копировать", box)
        if self._loading():
            # «Перевожу…» мерцает, пока ждём ответ
            a = int(150 + 105 * (0.5 + 0.5 * math.sin(time.time() * 6)))
            d.text((o + PAD * k, o + (PAD - 4 + HEAD_H / 2) * k), self.title, font=self.f_text,
                   fill=INK[:3] + (a,), anchor="lm")


if __name__ == "__main__":
    # посмотреть пузырь без программы: python translate_popup.py
    from winapi import set_dpi_aware, user32
    logging.basicConfig(level=logging.INFO)
    set_dpi_aware()
    b = TranslateBubble(lambda text: print("скопировано:", text[:40]))
    b.ready.wait()
    tok = b.show(None, user32.GetForegroundWindow())
    time.sleep(1.5)
    b.set_result(tok, "Привет! Я посмотрел твой отчёт — в целом всё отлично, но в третьем разделе цифры "
                      "за март не сходятся с таблицей. Сможешь перепроверить до пятницы?\n\nСпасибо!",
                 "Перевод на русский")
    time.sleep(20)
