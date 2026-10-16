"""Меню переписывания выделенного текста: тёмная плашка рядом с выделением, все варианты видны
сразу и срабатывают одним щелчком. Окно не забирает фокус, поэтому выделение в поле остаётся."""
import logging
import math
import threading
import time

import popup
import rewrite
from popup import ACCENT, BTN, INK, LABEL

log = logging.getLogger("dictation")

# логические размеры (умножаются на DPI-масштаб)
PAD = 8               # от края плашки до кнопок
BTN_H, BTN_PAD = 30, 12
GAP, ROW_GAP = 6, 6
LABEL_GAP = 10


class RewriteMenu(popup.Popup):
    """on_pick(token, kind) вызывается из потока меню сразу после щелчка по варианту."""
    CLASS = "DictationRewriteMenu"
    IDLE_HIDE = 30

    def __init__(self, on_pick):
        self.on_pick = on_pick
        self.busy = None
        super().__init__()

    def _reset(self):
        self.busy = None

    def _animating(self):
        return self.busy is not None   # выбранный вариант пульсирует, пока текст переписывается

    def _clicked(self, i):
        if self.busy is not None:
            return
        self.busy = i
        self._paint()
        try:
            self.on_pick(self.token, self.layout["buttons"][i][0])
        except Exception:
            log.exception("меню переписывания")

    def _button_style(self, i):
        if self.busy is None:
            return super()._button_style(i)
        if i == self.busy:
            pulse = 0.5 + 0.5 * math.sin(time.time() * 6)
            return ACCENT + (int(170 + 85 * pulse),), INK
        return BTN[:3] + (130,), INK[:3] + (90,)

    def _build(self, s):
        """Раскладка кнопок и неподвижная часть картинки (тень, плашка, подписи) для масштаба s."""
        if self.layout and self.layout["scale"] == s:
            return
        SS = popup.SS
        f_btn = popup.bold_font(int(13 * s * SS))
        f_lab = popup.regular_font(int(12 * s * SS))
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

        def labels(d, k, o):
            for name, ry in rows:
                d.text((o + PAD * k, o + (ry + BTN_H / 2) * k), name, font=f_lab, fill=LABEL, anchor="lm")
        size = self._make_base(s, width, height, labels)
        self.layout = {"scale": s, "size": size, "buttons": buttons, "font": f_btn}


if __name__ == "__main__":
    # посмотреть меню без программы: python rewrite_menu.py
    from winapi import set_dpi_aware, user32
    logging.basicConfig(level=logging.INFO)
    set_dpi_aware()
    menu = RewriteMenu(lambda token, kind: (print("выбрано:", kind), threading.Timer(1.5, menu.hide).start()))
    menu.ready.wait()
    menu.show(None, user32.GetForegroundWindow())
    time.sleep(20)
