"""Окно «Настройки и статистика» (CustomTkinter). Живёт в своём потоке внутри программы:
создаётся при первом открытии из трея, при закрытии просто прячется."""
import ctypes
import logging
import os
import queue
import sys
import threading
import time
from datetime import date, timedelta

import winapi

log = logging.getLogger("dictation")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(APP_DIR, "icon.ico")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# --- цвета: (светлая, тёмная)
BG = ("#f4f4f2", "#121211")
CARD = ("#fcfcfb", "#1a1a19")
CARD_2 = ("#efefec", "#242422")
BORDER = ("#e2e1dc", "#2e2e2b")
TEXT = ("#0b0b0b", "#ffffff")
TEXT_2 = ("#52514e", "#c3c2b7")
TEXT_3 = ("#85847e", "#8d8c84")
GRID = ("#e7e6e1", "#2a2a28")
ACCENT = ("#2a78d6", "#3987e5")
ACCENT_HOVER = ("#2167bd", "#2f76cf")
SERIES_TIME = ("#2a78d6", "#3987e5")
SERIES_COST = ("#eb6834", "#d95926")
WARN_BG = ("#fff4dc", "#3a2d10")
WARN_INK = ("#7a4b00", "#f2c26b")
ERR_INK = ("#a1201f", "#f19a9a")
GOOD = ("#1a7f37", "#4ac26b")

FONT = "Segoe UI"

HOTKEY_HINT = ("Нажмите на поле, затем клавишу, которой хотите диктовать, и «Сохранить». Подойдут Copilot, "
               "правый Ctrl или Alt, Caps Lock, F1–F12. Пока программа запущена, эта клавиша "
               "работает только для диктовки.")

PERIODS = {"Сегодня": "today", "7 дней": "7", "30 дней": "30", "Всё время": "all"}
KINDS = (("transcribe", "Распознавание"), ("cleanup", "Живая правка"), ("translate", "Перевод"))

# поле настроек -> подписи вариантов (значение, подпись)
CHOICES = {
    "local_model": [("gigaam-v3-e2e-rnnt", "GigaAM v3 RNNT — лучший русский"),
                    ("gigaam-v3-e2e-ctc", "GigaAM v3 CTC — чуть быстрее"),
                    ("nemo-parakeet-tdt-0.6b-v3", "Parakeet v3 — русский + английский")],
    "local_quantization": [("int8", "int8 — быстрее, меньше памяти"), ("", "Полная точность")],
    "transcribe_model": [("gpt-live-transcribe", "gpt-live-transcribe — текст по ходу речи, ~$1/час"),
                         ("gpt-4o-transcribe", "gpt-4o-transcribe — по фразам, ~$0,36/час"),
                         ("gpt-4o-mini-transcribe", "gpt-4o-mini-transcribe — вдвое дешевле")],
    "live_delay": [("minimal", "Минимальная — быстрее всего, больше ошибок"),
                   ("low", "Низкая — оптимально"), ("medium", "Средняя"),
                   ("high", "Высокая — точнее, текст отстаёт сильнее")],
    "language": [("", "Определять автоматически"), ("ru", "Русский"), ("en", "Английский")],
    "noise_reduction": [("near_field", "Ноутбук или гарнитура"), ("far_field", "Микрофон далеко"),
                        ("", "Выключено")],
    "local_cleanup_model": [("qwen3.5-2b", "Qwen3.5 2B — быстрая, ~1,3 ГБ памяти"),
                            ("qwen3.5-4b", "Qwen3.5 4B — точнее, вдвое медленнее, ~2,7 ГБ")],
    "cleanup_model": [("gpt-4.1-mini", "gpt-4.1-mini — оптимально"),
                      ("gpt-4.1-nano", "gpt-4.1-nano — дешевле и быстрее"),
                      ("gpt-4.1", "gpt-4.1 — умнее, в 5 раз дороже"), ("gpt-4o-mini", "gpt-4o-mini")],
    "translate_model": [("gpt-4.1-mini", "gpt-4.1-mini"), ("gpt-4.1-nano", "gpt-4.1-nano"),
                        ("gpt-4.1", "gpt-4.1"), ("gpt-4o-mini", "gpt-4o-mini")],
}


# ---------------------------------------------------------------- автозапуск

def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "Dictation")
        return True
    except OSError:
        return False


def set_autostart(enable):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            winreg.SetValueEx(k, "Dictation", 0, winreg.REG_SZ,
                              f'"{pythonw}" "{os.path.join(APP_DIR, "dictation.py")}"')
        else:
            try:
                winreg.DeleteValue(k, "Dictation")
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- форматирование

def fmt_duration(sec):
    sec = max(0, round(sec))
    if sec < 60:
        return f"{sec} с"
    m = round(sec / 60)
    if m < 60:
        return f"{m} мин"
    h, mm = divmod(m, 60)
    return f"{h} ч {mm} мин" if mm else f"{h} ч"


def fmt_usd(v):
    if v == 0:
        return "$0"
    if v < 0.01:
        return "$" + f"{v:.4f}".rstrip("0")
    if v < 10:
        return f"${v:.2f}"
    return f"${round(v):,}".replace(",", " ")


def fmt_int(n):
    return f"{int(n):,}".replace(",", " ")


def trim_num(v):
    return f"{v:.3g}".replace(".", ",")


MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def short_day(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def long_day(d):
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def nice_scale(v):
    """Верх шкалы и число делений, чтобы подписи были круглыми."""
    if v <= 0:
        return 1, 4
    import math
    p = 10 ** math.floor(math.log10(v))
    for m, div in ((1, 5), (2, 4), (2.5, 5), (5, 5), (10, 5)):
        if m * p >= v * (1 - 1e-9):
            return m * p, div
    return 10 * p, 5


def cost_of(d):
    return sum((d.get("cost") or {}).values())


EMPTY_DAY = {"sessions": 0, "seconds": 0.0, "chars": 0, "words": 0, "cost": {}}


# ---------------------------------------------------------------- график

class BarChart:
    """Столбики по дням на обычном Canvas: подписи осей, сетка, подсказка при наведении."""

    def __init__(self, parent, ctk, color, fmt_axis, fmt_tip):
        self.ctk = ctk
        self.color = color
        self.fmt_axis = fmt_axis
        self.fmt_tip = fmt_tip
        self.points = []
        self.canvas = ctk.CTkCanvas(parent, height=10, highlightthickness=0, bd=0)
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda e: self.draw())
        self.geom = None

    def c(self, pair):
        return pair[1] if self.ctk.get_appearance_mode() == "Dark" else pair[0]

    def scale(self):
        try:
            return self.ctk.ScalingTracker.get_widget_scaling(self.canvas)
        except Exception:
            return 1.0

    def set(self, points):
        self.points = points
        s = self.scale()
        self.canvas.configure(height=int(170 * s))
        self.draw()

    def draw(self, active=None):
        cv = self.canvas
        cv.delete("all")
        cv.configure(bg=self.c(CARD))
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 50 or not self.points:
            return
        s = self.scale()
        L, R, T, B = 44 * s, 4 * s, 10 * s, 24 * s
        font = (FONT, -int(11 * s))
        top_v, div = nice_scale(max(p["v"] for p in self.points))
        n = len(self.points)
        slot = (W - L - R) / n
        bw = max(2 * s, min(28 * s, slot - 3 * s))
        y = lambda v: T + (H - T - B) * (1 - v / top_v)
        self.geom = (L, slot, y)

        for i in range(div + 1):
            v = top_v * i / div
            yy = round(y(v))
            cv.create_line(L, yy, W - R, yy, fill=self.c(GRID))
            cv.create_text(L - 6 * s, yy, text=self.fmt_axis(v), anchor="e", fill=self.c(TEXT_3), font=font)

        label_every = max(1, -(-n // max(1, int((W - L) / (58 * s)))))
        color = self.c(self.color)
        dim = _mix(color, self.c(CARD), 0.55)
        for i, p in enumerate(self.points):
            cx = L + slot * i + slot / 2
            if p["v"] > 0:
                fill = color if active is None or active == i else dim
                _round_top_bar(cv, cx - bw / 2, y(p["v"]), cx + bw / 2, H - B, min(4 * s, bw / 2), fill)
            if (n - 1 - i) % label_every == 0:
                cv.create_text(cx, H - B + 13 * s, text=short_day(p["day"]), fill=self.c(TEXT_3), font=font)

        if active is not None:
            self._tooltip(active)

    def _hover(self, e):
        if not self.geom or not self.points:
            return
        L, slot, _ = self.geom
        i = int((e.x - L) // slot)
        if 0 <= i < len(self.points):
            self.draw(active=i)
        else:
            self.draw()

    def _tooltip(self, i):
        cv, s = self.canvas, self.scale()
        L, slot, y = self.geom
        p = self.points[i]
        lines = [long_day(p["day"])] + self.fmt_tip(p)
        font_b = (FONT, -int(12 * s), "bold")
        font = (FONT, -int(12 * s))
        cx = L + slot * i + slot / 2
        pad = 8 * s
        ids = []
        yy = 0
        for k, line in enumerate(lines):
            ids.append(cv.create_text(0, yy, text=line, anchor="nw",
                                      font=font_b if k == 1 else font,
                                      fill=self.c(TEXT if k == 1 else TEXT_2)))
            yy += 17 * s
        x0, y0, x1, y1 = cv.bbox(*ids)
        w, h = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
        tx = min(max(cx - w / 2, 0), cv.winfo_width() - w)
        ty = max(0, y(p["v"]) - h - 8 * s)
        bg = cv.create_rectangle(tx, ty, tx + w, ty + h, fill=self.c(CARD_2), outline=self.c(BORDER))
        for t in ids:
            cv.move(t, tx + pad, ty + pad)
            cv.tag_raise(t, bg)


def _mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def _round_top_bar(cv, x0, top, x1, base, r, fill):
    r = max(0, min(r, (base - top)))
    kw = {"fill": fill, "outline": ""}
    if r < 1:
        cv.create_rectangle(x0, top, x1, base, **kw)
        return
    cv.create_rectangle(x0, top + r, x1, base, **kw)
    cv.create_rectangle(x0 + r, top, x1 - r, top + r + 1, **kw)
    cv.create_oval(x0, top, x0 + 2 * r, top + 2 * r, **kw)
    cv.create_oval(x1 - 2 * r, top, x1, top + 2 * r, **kw)


# ---------------------------------------------------------------- окно

class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.cmds = queue.Queue()
        self.thread = None
        self.root = None

    def open(self):
        """Можно вызывать из любого потока."""
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True, name="settings-ui")
            self.thread.start()
        else:
            self.cmds.put(("show",))

    # --- поток окна

    def _run(self):
        try:
            import customtkinter as ctk
        except ImportError:
            self.app.notify("Для окна настроек нужен пакет customtkinter: pip install customtkinter")
            return
        self.ctk = ctk
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Dictation.App")
        except Exception:
            pass
        ctk.set_appearance_mode("system")
        root = self.root = ctk.CTk(fg_color=BG)
        root.title("Диктовка")
        root.geometry("1040x780")
        root.minsize(720, 520)
        self._set_icon(root)
        root.protocol("WM_DELETE_WINDOW", self._hide)
        root.bind_all("<Control-KeyPress>", self._ctrl_keys, add="+")
        root.bind_all("<Button-3>", self._context_menu, add="+")

        self.period = "7"
        self.dirty = False
        self.need_restart = False
        self.vars = {}
        self.capturing = None
        self._build()
        self._load_settings()
        self._refresh()
        self._show()
        root.after(100, self._poll)
        root.after(5000, self._tick)
        root.mainloop()

    # В русской раскладке Tk не узнаёт Ctrl+V/C/X/A (keysym кириллический) — ловим по коду клавиши
    CTRL_EVENTS = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}

    def _ctrl_keys(self, e):
        if e.keysym.lower() in ("v", "c", "x", "a"):
            return None   # латинская раскладка — Tk справится сам
        ev = self.CTRL_EVENTS.get(e.keycode)
        if ev and e.widget.winfo_class() in ("Entry", "Text"):
            e.widget.event_generate(ev)
            return "break"
        return None

    def _context_menu(self, e):
        w = e.widget
        if w.winfo_class() not in ("Entry", "Text"):
            return
        import tkinter as tk
        menu = tk.Menu(w, tearoff=0)
        for label, ev in (("Вырезать", "<<Cut>>"), ("Копировать", "<<Copy>>"), ("Вставить", "<<Paste>>")):
            def run(ev=ev):
                w.focus_set()
                w.event_generate(ev)
                if ev != "<<Copy>>":
                    self._mark_dirty()
            menu.add_command(label=label, command=run)
        menu.tk_popup(e.x_root, e.y_root)

    def _set_icon(self, root):
        try:
            if not os.path.exists(ICON_PATH):
                self.app.make_image().save(ICON_PATH, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
            root.iconbitmap(ICON_PATH)
        except Exception:
            log.exception("иконка окна")

    def _hide(self):
        if self.capturing:
            self.app.cancel_capture()
        self.root.withdraw()

    def _show(self):
        r = self.root
        r.deiconify()
        r.lift()
        r.attributes("-topmost", True)
        r.after(200, lambda: r.attributes("-topmost", False))
        r.focus_force()

    def _poll(self):
        try:
            while True:
                cmd = self.cmds.get_nowait()
                if cmd[0] == "show":
                    self._refresh()
                    self._show()
                elif cmd[0] == "saved":
                    self._on_saved(cmd[1])
                elif cmd[0] == "hotkey":
                    self._on_hotkey(cmd[1], cmd[2])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _tick(self):
        if self.root.state() != "withdrawn":
            self._refresh()
            if not self.dirty and not self.capturing:
                self._load_settings()
        self.root.after(5000, self._tick)

    # --- общие виджеты

    def font(self, size=13, weight="normal"):
        return self.ctk.CTkFont(family=FONT, size=size, weight=weight)

    def card(self, parent, **kw):
        return self.ctk.CTkFrame(parent, fg_color=CARD, border_color=BORDER, border_width=1,
                                 corner_radius=12, **kw)

    def seg_style(self):
        return {"selected_color": ACCENT, "selected_hover_color": ACCENT_HOVER,
                "unselected_color": CARD_2, "unselected_hover_color": BORDER, "fg_color": CARD_2,
                "text_color": TEXT}

    def label(self, parent, text="", size=13, weight="normal", color=TEXT, **kw):
        kw.setdefault("anchor", "w")
        return self.ctk.CTkLabel(parent, text=text, font=self.font(size, weight), text_color=color,
                                 justify="left", **kw)

    # --- разметка

    def _build(self):
        ctk, root = self.ctk, self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(root, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 8))
        head.grid_columnconfigure(1, weight=1)
        title = ctk.CTkFrame(head, fg_color="transparent")
        title.grid(row=0, column=0, sticky="w")
        self.label(title, "Диктовка", 20, "bold").pack(anchor="w")
        self.status = self.label(title, "", 12, color=TEXT_3)
        self.status.pack(anchor="w")
        self.tabs = ctk.CTkSegmentedButton(head, values=["Статистика", "Настройки"],
                                           command=self._switch_tab, font=self.font(13),
                                           **self.seg_style())
        self.tabs.grid(row=0, column=2, sticky="e")
        self.tabs.set("Статистика")

        self.stats_page = ctk.CTkScrollableFrame(root, fg_color="transparent")
        self.settings_page = ctk.CTkFrame(root, fg_color="transparent")
        self._build_stats(self.stats_page)
        self._build_settings(self.settings_page)
        self.stats_page.grid(row=1, column=0, sticky="nsew", padx=(14, 6), pady=(0, 12))

    def _switch_tab(self, name):
        if name == "Статистика":
            self.settings_page.grid_forget()
            self.stats_page.grid(row=1, column=0, sticky="nsew", padx=(14, 6), pady=(0, 12))
            self._refresh()
        else:
            self.stats_page.grid_forget()
            self.settings_page.grid(row=1, column=0, sticky="nsew", padx=(14, 6), pady=(0, 12))

    # ---------------- статистика

    def _build_stats(self, page):
        ctk = self.ctk
        page.grid_columnconfigure(0, weight=1)
        P = dict(padx=10)

        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(4, 10), **P)
        bar.grid_columnconfigure(1, weight=1)
        self.period_btn = ctk.CTkSegmentedButton(bar, values=list(PERIODS), command=self._set_period,
                                                 font=self.font(12), **self.seg_style())
        self.period_btn.set("7 дней")
        self.period_btn.grid(row=0, column=0, sticky="w")
        self.calc_note = self.label(bar, "", 12, color=TEXT_3)
        self.calc_note.grid(row=0, column=2, sticky="e")

        self.empty = self.card(page)
        self.label(self.empty, "Пока нет данных", 15, "bold").pack(pady=(36, 2))
        self.label(self.empty, "Зажмите клавишу диктовки и продиктуйте что-нибудь — статистика появится здесь.",
                   13, color=TEXT_2).pack(pady=(0, 36))

        body = self.body = ctk.CTkFrame(page, fg_color="transparent")
        body.grid_columnconfigure(0, weight=1)

        kpis = ctk.CTkFrame(body, fg_color="transparent")
        kpis.grid(row=0, column=0, sticky="ew")
        for i, w in enumerate((14, 10, 10, 10)):
            kpis.grid_columnconfigure(i, weight=w, uniform="kpi")
        self.kpi = {}
        for i, (key, name) in enumerate((("saved", "Сэкономлено времени"), ("cost", "Потрачено на OpenAI"),
                                         ("words", "Надиктовано слов"), ("sessions", "Диктовок"))):
            c = self.card(kpis)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 3 else 6))
            self.label(c, name, 12, color=TEXT_2).pack(anchor="w", padx=18, pady=(16, 0))
            v = self.label(c, "—", 34 if key == "saved" else 24, "bold")
            v.pack(anchor="w", padx=18)
            sub = self.label(c, "", 12, color=TEXT_3, wraplength=400 if key == "saved" else 220)
            sub.pack(anchor="w", padx=18, pady=(0, 16))
            self.kpi[key] = (v, sub)

        self.charts = {}
        for row, (key, title, color) in enumerate((("time", "Сэкономлено по дням, минут", SERIES_TIME),
                                                   ("cost", "Расходы по дням, $", SERIES_COST)), start=1):
            c = self.card(body)
            c.grid(row=row, column=0, sticky="ew", pady=(12, 0))
            self.label(c, title, 14, "bold").pack(anchor="w", padx=18, pady=(14, 4))
            if key == "time":
                chart = BarChart(c, ctk, color, trim_num,
                                 lambda p: [f"{fmt_duration(p['v'] * 60)} сэкономлено",
                                            f"{fmt_int(p['d']['words'])} слов, {p['d']['sessions']} диктовок"])
            else:
                chart = BarChart(c, ctk, color, lambda v: "$" + trim_num(v).replace(",", "."),
                                 lambda p: [f"{fmt_usd(p['v'])} потрачено"])
            chart.canvas.pack(fill="x", padx=14, pady=(0, 12))
            self.charts[key] = chart

        split = ctk.CTkFrame(body, fg_color="transparent")
        split.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        split.grid_columnconfigure((0, 1), weight=1, uniform="split")

        bd = self.card(split)
        bd.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.label(bd, "На что ушли деньги", 14, "bold").pack(anchor="w", padx=18, pady=(14, 4))
        self.breakdown = {}
        for key, name in KINDS:
            row = ctk.CTkFrame(bd, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=(6, 0))
            row.grid_columnconfigure(0, weight=1)
            self.label(row, name).grid(row=0, column=0, sticky="w")
            val = self.label(row, "", weight="bold")
            val.grid(row=0, column=1, sticky="e")
            pb = ctk.CTkProgressBar(row, height=8, corner_radius=4, fg_color=CARD_2, progress_color=SERIES_COST)
            pb.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
            self.breakdown[key] = (val, pb)
        self.label(bd, "По ценам OpenAI на модели. Сверить можно на platform.openai.com/usage.",
                   12, color=TEXT_3, wraplength=380).pack(anchor="w", padx=18, pady=(12, 16))

        fc = self.card(split)
        fc.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.label(fc, "Подробнее", 14, "bold").pack(anchor="w", padx=18, pady=(14, 4))
        self.facts = []
        for i in range(5):
            row = ctk.CTkFrame(fc, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=3)
            row.grid_columnconfigure(0, weight=1)
            a = self.label(row, "", color=TEXT_2)
            a.grid(row=0, column=0, sticky="w")
            b = self.label(row, "", weight="bold")
            b.grid(row=0, column=1, sticky="e")
            self.facts.append((a, b))

        tc = self.card(body)
        tc.grid(row=4, column=0, sticky="ew", pady=(12, 8))
        self.label(tc, "По дням", 14, "bold").pack(anchor="w", padx=18, pady=(14, 4))
        self.table = ctk.CTkFrame(tc, fg_color="transparent")
        self.table.pack(fill="x", padx=18, pady=(0, 14))

    def _set_period(self, name):
        self.period = PERIODS[name]
        self._render_stats()

    def _days(self):
        all_days = self.app.stats.snapshot().get("days", {})
        today = date.today()
        if self.period == "today":
            n = 1
        elif self.period == "all":
            n = (today - date.fromisoformat(min(all_days))).days + 1 if all_days else 1
        else:
            n = int(self.period)
        out = []
        for i in range(n - 1, -1, -1):
            d = today - timedelta(days=i)
            out.append((d, {**EMPTY_DAY, **all_days.get(d.isoformat(), {})}))
        return out, all_days

    def _saved(self, d):
        cpm = self.app.cfg.get("typing_cpm") or 200
        return max(0.0, d["chars"] / cpm * 60 - d["seconds"])

    def _refresh(self):
        cfg, app = self.app.cfg, self.app
        if app.session is not None:
            self.status.configure(text="● Идёт диктовка", text_color=ERR_INK)
        else:
            self.status.configure(text="● Работает · " + ("локальная модель" if cfg["backend"] == "local"
                                                          else "OpenAI"), text_color=GOOD)
        if self.tabs.get() == "Статистика":
            self._render_stats()

    def _render_stats(self):
        days, all_days = self._days()
        cpm = self.app.cfg.get("typing_cpm") or 200
        self.calc_note.configure(text=f"при скорости печати {cpm} знаков/мин")
        has_any = any(d.get("sessions") or cost_of(d) for d in all_days.values())
        if not has_any:
            self.body.grid_forget()
            self.empty.grid(row=1, column=0, sticky="ew", padx=10)
            return
        self.empty.grid_forget()
        self.body.grid(row=1, column=0, sticky="ew", padx=10)

        tot = {"sessions": 0, "seconds": 0.0, "chars": 0, "words": 0, "saved": 0.0, "cost": 0.0}
        by_kind = {}
        for _, d in days:
            for k in ("sessions", "seconds", "chars", "words"):
                tot[k] += d[k]
            tot["saved"] += self._saved(d)
            tot["cost"] += cost_of(d)
            for k, v in (d.get("cost") or {}).items():
                by_kind[k] = by_kind.get(k, 0) + v
        typing = tot["chars"] / cpm * 60

        v, sub = self.kpi["saved"]
        v.configure(text=fmt_duration(tot["saved"]))
        if tot["chars"]:
            text = f"печатать — {fmt_duration(typing)}, диктовать — {fmt_duration(tot['seconds'])}"
            if tot["seconds"] > 0:
                text += f"\nв {trim_num(round(typing / tot['seconds'], 1))} раза быстрее клавиатуры"
            sub.configure(text=text)
        else:
            sub.configure(text="нет диктовок за период")
        v, sub = self.kpi["cost"]
        v.configure(text=fmt_usd(tot["cost"]))
        if tot["saved"] > 60 and tot["cost"] > 0:
            sub.configure(text=f"{fmt_usd(tot['cost'] / (tot['saved'] / 3600))} за час сэкономленного")
        else:
            sub.configure(text="локальная модель бесплатна" if tot["cost"] == 0 else "")
        v, sub = self.kpi["words"]
        v.configure(text=fmt_int(tot["words"]))
        sub.configure(text=f"{fmt_int(tot['chars'])} знаков")
        v, sub = self.kpi["sessions"]
        v.configure(text=fmt_int(tot["sessions"]))
        sub.configure(text=f"в среднем {round(tot['words'] / tot['sessions'])} слов" if tot["sessions"] else "")

        chart_days = days[-60:]
        if len(chart_days) < 7:
            today = date.today()
            chart_days = [(today - timedelta(days=i),
                           {**EMPTY_DAY, **all_days.get((today - timedelta(days=i)).isoformat(), {})})
                          for i in range(6, -1, -1)]
        self.charts["time"].set([{"day": dd, "v": self._saved(d) / 60, "d": d} for dd, d in chart_days])
        self.charts["cost"].set([{"day": dd, "v": cost_of(d), "d": d} for dd, d in chart_days])

        mx = max([by_kind.get(k, 0) for k, _ in KINDS] + [1e-12])
        for k, _ in KINDS:
            val, pb = self.breakdown[k]
            x = by_kind.get(k, 0)
            val.configure(text=fmt_usd(x))
            pb.set(x / mx if x else 0)

        active = sum(1 for _, d in days if d["sessions"])
        facts = [
            ("Время диктовки", fmt_duration(tot["seconds"])),
            ("Ушло бы на печать", fmt_duration(typing)),
            ("Скорость диктовки", f"{fmt_int(tot['chars'] / tot['seconds'] * 60)} знаков/мин"
             if tot["seconds"] > 0 else "—"),
            ("Дней с диктовкой", f"{active} из {len(days)}"),
            ("Цена 1000 слов", fmt_usd(tot["cost"] / tot["words"] * 1000) if tot["words"] else "—"),
        ]
        for (a, b), (x, y) in zip(self.facts, facts):
            a.configure(text=x)
            b.configure(text=y)

        self._render_table([(dd, d) for dd, d in days if d["sessions"] or cost_of(d)][::-1][:31])

    def _render_table(self, rows):
        t = self.table
        for w in t.winfo_children():
            w.destroy()
        heads = ("День", "Диктовок", "Слов", "Сэкономлено", "Потрачено")
        for c in range(5):
            t.grid_columnconfigure(c, weight=2 if c == 0 else 1)
        for c, h in enumerate(heads):
            self.label(t, h, 12, "bold", TEXT_2, anchor="w" if c == 0 else "e").grid(
                row=0, column=c, sticky="ew", pady=(0, 2))
        for r, (dd, d) in enumerate(rows, start=1):
            vals = (long_day(dd), str(d["sessions"]), fmt_int(d["words"]), fmt_duration(self._saved(d)),
                    fmt_usd(cost_of(d)))
            for c, v in enumerate(vals):
                self.label(t, v, 12, anchor="w" if c == 0 else "e").grid(row=r, column=c, sticky="ew")

    # ---------------- настройки

    def _build_settings(self, page):
        ctk = self.ctk
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)

        self.restart_bar = ctk.CTkFrame(page, fg_color=WARN_BG, corner_radius=10)
        self.label(self.restart_bar, "Чтобы новая локальная модель загрузилась, программу нужно перезапустить.",
                   color=WARN_INK).pack(side="left", padx=14, pady=10)
        self.restart_btn = ctk.CTkButton(self.restart_bar, text="Перезапустить", width=130,
                                         command=self._restart, fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.restart_btn.pack(side="right", padx=10, pady=8)

        scroll = self.settings_scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
        scroll.grid(row=2, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        self.rows = {}

        sec = self._section(scroll, 0, "Клавиша диктовки",
                            "Держите клавишу — идёт диктовка. Коротко нажмите и сразу зажмите — "
                            "диктовка с переводом на английский.")
        self._field(sec, "hotkey_vk", "Клавиша", "hotkey",
                    hint=HOTKEY_HINT)

        sec = self._section(scroll, 1, "Распознавание речи",
                            "Локальная модель работает без интернета и бесплатно. "
                            "OpenAI быстрее и лучше понимает смешанную речь, но стоит денег.")
        self._field(sec, "backend", "Где распознавать", "segment",
                    choices=[("local", "Локально"), ("openai", "OpenAI")])
        self._field(sec, "local_model", "Локальная модель", "choice", group="local")
        self._field(sec, "local_quantization", "Точность модели", "choice", group="local")
        self._field(sec, "local_streaming", "Текст по ходу речи", "switch", group="local",
                    hint="Слова печатаются через ~1 с после того, как сказаны, не дожидаясь паузы. "
                         "Нагружает процессор сильнее.")
        self._field(sec, "transcribe_model", "Модель OpenAI", "choice", group="openai")
        self._field(sec, "live_delay", "Задержка потокового текста", "choice", group="openai",
                    hint="Только для gpt-live-transcribe.")
        self._field(sec, "language", "Язык", "choice", group="openai")
        self._field(sec, "transcribe_prompt", "Подсказка для распознавания", "text", group="openai",
                    hint="Термины, имена, названия, которые модель должна писать правильно.")
        self._field(sec, "noise_reduction", "Шумоподавление", "choice", group="openai")
        self._field(sec, "vad_silence_ms", "Пауза — конец фразы", "slider",
                    hint="Для локальной модели и gpt-4o-transcribe. Меньше — текст появляется быстрее, "
                         "но фразы чаще рвутся посередине.")

        sec = self._section(scroll, 2, "Перевод на английский",
                            "Коротко нажать клавишу диктовки и сразу зажать — диктовка с переводом на английский.")
        self._field(sec, "translate_engine", "Где переводить", "segment",
                    choices=[("local", "Локально"), ("openai", "OpenAI")],
                    hint="Локально — без интернета и бесплатно, перевод печатается по ходу речи. "
                         "OpenAI переводит литературнее, но нужны интернет и ключ.")
        self._field(sec, "translate_model", "Модель OpenAI", "choice", group="translate_openai")

        sec = self._section(scroll, 3, "ИИ-обработка", None)
        self._field(sec, "cleanup", "Живая правка", "switch",
                    hint="Убирает «э», «ну», оговорки и правит пунктуацию прямо в набранном тексте.")
        self._field(sec, "cleanup_engine", "Где править", "segment", group="cleanup",
                    choices=[("auto", "Авто"), ("local", "Локально"), ("openai", "OpenAI")],
                    hint="Авто — через OpenAI, пока есть интернет (ноутбук не греется), а без интернета — "
                         "своей моделью. Локально — всегда своей моделью: бесплатно, но ~1,5 ГБ памяти "
                         "и нагрузка на процессор.")
        self._field(sec, "local_cleanup_model", "Локальная модель", "choice", group="cleanup_local")
        self._field(sec, "cleanup_model", "Модель OpenAI", "choice", group="cleanup_openai")
        self._field(sec, "openai_api_key", "API-ключ OpenAI", "secret")

        sec = self._section(scroll, 4, "Поведение", None)
        self._field(sec, "mic_device", "Микрофон", "choice",
                    hint="Подключили новый микрофон — перезапустите программу, чтобы он появился в списке.")
        self._field(sec, "keep_mic_open", "Держать микрофон открытым", "switch",
                    hint="Запись стартует мгновенно, но Windows всё время показывает значок микрофона.")
        self._field(sec, "add_trailing_space", "Пробел после текста", "switch")
        self._field(sec, "overlay_position", "Индикатор записи", "segment",
                    choices=[("top", "Сверху, под камерой"), ("bottom", "Снизу, на панели задач")])
        self._field(sec, "autostart", "Запускать вместе с Windows", "switch", hint="Применяется сразу.")

        sec = self._section(scroll, 5, "Подсчёт сэкономленного времени",
                            "Сколько ушло бы на то, чтобы напечатать тот же текст, "
                            "минус время, пока вы держали клавишу.")
        self._field(sec, "typing_cpm", "Скорость печати, знаков/мин", "number",
                    hint="Обычно 150–250. Проверить можно на любом тренажёре слепой печати.")

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=10, pady=(10, 0))
        self.save_btn = ctk.CTkButton(actions, text="Сохранить", width=130, command=self._save,
                                      fg_color=ACCENT, hover_color=ACCENT_HOVER, state="disabled",
                                      font=self.font(13, "bold"))
        self.save_btn.pack(side="left")
        ctk.CTkButton(actions, text="Отменить изменения", width=150, command=self._load_settings,
                      fg_color="transparent", border_width=1, border_color=BORDER, text_color=TEXT,
                      hover_color=CARD_2).pack(side="left", padx=8)
        self.save_msg = self.label(actions, "", 12)
        self.save_msg.pack(side="left", padx=6)
        for text, name in (("Открыть лог", "dictation.log"), ("Открыть config.json", "config.json")):
            ctk.CTkButton(actions, text=text, width=10, fg_color="transparent", text_color=TEXT_2,
                          hover_color=CARD_2,
                          command=lambda n=name: os.startfile(os.path.join(APP_DIR, n))).pack(side="right")

    def _section(self, parent, row, title, note):
        c = self.card(parent)
        c.grid(row=row, column=0, sticky="ew", padx=10, pady=(0, 12))
        c.grid_columnconfigure(1, weight=1)
        self.label(c, title, 15, "bold").grid(row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(14, 0))
        if note:
            self.label(c, note, 12, color=TEXT_3, wraplength=700).grid(
                row=1, column=0, columnspan=2, sticky="w", padx=18)
        c._next_row = 2
        return c

    def _field(self, sec, key, name, kind, group=None, hint=None, choices=None):
        ctk = self.ctk
        r = sec._next_row
        sec._next_row += 2 if hint else 1
        lab = self.label(sec, name, 13, width=240)
        lab.grid(row=r, column=0, sticky="nw", padx=(18, 12), pady=(12, 0 if hint else 12))
        mark = self._mark_dirty
        if kind == "segment":
            labels = dict(choices)
            w = ctk.CTkSegmentedButton(sec, values=list(labels.values()), font=self.font(13),
                                       **self.seg_style(),
                                       command=lambda _: mark())
            get = lambda: {v: k for k, v in labels.items()}.get(w.get())
            put = lambda v: w.set(labels.get(v, ""))
        elif kind == "choice":
            opts = self.app.mic_choices() if key == "mic_device" else list(CHOICES[key])
            w = ctk.CTkOptionMenu(sec, values=[l for _, l in opts], width=340, font=self.font(13),
                                  dropdown_font=self.font(13), fg_color=CARD_2, button_color=CARD_2,
                                  button_hover_color=BORDER, text_color=TEXT, dropdown_fg_color=CARD,
                                  dropdown_text_color=TEXT, dropdown_hover_color=CARD_2,
                                  command=lambda _: mark())

            def get(w=w, opts=opts):
                return next((v for v, l in opts if l == w.get()), w.get())

            def put(v, w=w, opts=opts):
                if not any(o == v for o, _ in opts):
                    opts.append((v, v))
                    w.configure(values=[l for _, l in opts])
                w.set(next(l for o, l in opts if o == v))
        elif kind == "switch":
            w = ctk.CTkSwitch(sec, text="", command=(self._toggle_autostart if key == "autostart" else mark),
                              progress_color=ACCENT)
            get = lambda: bool(w.get())
            put = lambda v: w.select() if v else w.deselect()
        elif kind == "slider":
            box = ctk.CTkFrame(sec, fg_color="transparent")
            val = self.label(box, "", color=TEXT_2, width=60)

            def on_slide(v):
                val.configure(text=f"{v / 1000:.2f} с".replace(".", ","))
                mark()
            slider = ctk.CTkSlider(box, from_=200, to=1500, number_of_steps=26, width=280,
                                   button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                                   progress_color=ACCENT, command=on_slide)
            slider.pack(side="left")
            val.pack(side="left", padx=10)
            get = lambda: int(round(slider.get()))
            put = lambda v: (slider.set(v), val.configure(text=f"{v / 1000:.2f} с".replace(".", ",")))
            w = box
        elif kind == "text":
            w = ctk.CTkTextbox(sec, height=64, width=420, fg_color=CARD_2, text_color=TEXT,
                               border_width=0, font=self.font(13))
            w.bind("<KeyRelease>", lambda e: mark())
            get = lambda: w.get("1.0", "end").strip()
            put = lambda v: (w.delete("1.0", "end"), w.insert("1.0", v or ""))
        elif kind == "hotkey":
            state = {"vk": None}
            w = ctk.CTkButton(sec, text="", width=240, fg_color=CARD_2, hover_color=BORDER, text_color=TEXT,
                              border_width=1, border_color=BORDER, font=self.font(13),
                              command=lambda: self._capture_hotkey(key))

            def get(state=state):
                return state["vk"]

            def put(v, w=w, state=state):
                state["vk"] = v
                w.configure(text=winapi.key_name(v), border_color=BORDER)
        elif kind == "number":
            w = ctk.CTkEntry(sec, width=120, fg_color=CARD_2, text_color=TEXT,
                             border_color=BORDER, font=self.font(13))
            w.bind("<KeyRelease>", lambda e: mark())
            get = lambda: w.get().strip()
            put = lambda v: (w.delete(0, "end"), w.insert(0, str(v)))
        elif kind == "secret":
            w = ctk.CTkFrame(sec, fg_color="transparent")
            entry = ctk.CTkEntry(w, width=420, fg_color=CARD_2, text_color=TEXT, border_color=BORDER,
                                 font=self.font(13), show="•")
            entry.bind("<KeyRelease>", lambda e: mark())
            entry.pack(side="left")

            def paste(entry=entry):
                try:
                    text = self.root.clipboard_get().strip()
                except Exception:
                    return
                entry.delete(0, "end")
                entry.insert(0, text)
                mark()
            ctk.CTkButton(w, text="Вставить", width=90, fg_color=CARD_2, hover_color=BORDER, text_color=TEXT,
                          border_width=1, border_color=BORDER, font=self.font(13),
                          command=paste).pack(side="left", padx=(8, 0))
            get = lambda: entry.get().strip()
            put = lambda v: (entry.delete(0, "end"), entry.insert(0, str(v)))
        w.grid(row=r, column=1, sticky="w", padx=(0, 18), pady=(12, 0 if hint else 12))
        hint_lab = None
        if hint or kind == "secret":
            hint_lab = self.label(sec, hint or "", 12, color=TEXT_3, wraplength=560)
            hint_lab.grid(row=r + 1, column=1, sticky="w", padx=(0, 18), pady=(2, 12))
            if not hint:
                sec._next_row += 1
        self.rows[key] = {"get": get, "put": put, "group": group, "label": lab, "widget": w,
                          "hint": hint_lab}

    def _capture_hotkey(self, key):
        row = self.rows[key]
        if self.capturing:
            self.app.cancel_capture()
            return
        token = self.capturing = object()
        row["widget"].configure(text="Нажмите клавишу…  (Esc — отмена)", border_color=ACCENT)
        row["hint"].configure(text=HOTKEY_HINT, text_color=TEXT_3)
        self.app.start_capture(lambda vk: self.cmds.put(("hotkey", key, vk)))

        def timeout():
            if self.capturing is token:
                self.app.cancel_capture()
        self.root.after(10_000, timeout)

    def _on_hotkey(self, key, vk):
        self.capturing = None
        row = self.rows[key]
        row["put"](row["get"]())   # вернуть подпись текущей клавиши
        if vk is None or vk == 0x1B:
            return
        if vk in winapi.TYPING_KEYS:
            row["hint"].configure(text=f"«{winapi.key_name(vk)}» нужна для набора текста — выберите другую "
                                       "клавишу.", text_color=ERR_INK)
            return
        if vk != row["get"]():
            row["put"](vk)
            self._mark_dirty()

    def _set_group_state(self):
        backend = self.rows["backend"]["get"]()
        cleanup = self.rows["cleanup"]["get"]()
        cleanup_engine = self.rows["cleanup_engine"]["get"]()
        translate = self.rows["translate_engine"]["get"]()
        for key, row in self.rows.items():
            g = row["group"]
            on = g is None or (g == "local" and backend == "local") or \
                (g == "openai" and backend == "openai") or (g == "cleanup" and cleanup) or \
                (cleanup and g in ("cleanup_local", "cleanup_openai")
                 and (cleanup_engine == "auto" or g == "cleanup_" + cleanup_engine)) or \
                (g == "translate_openai" and translate == "openai")
            row["label"].configure(text_color=TEXT if on else TEXT_3)

    def _mark_dirty(self):
        if self._loading:
            return
        self.dirty = True
        self.save_btn.configure(state="normal")
        self.save_msg.configure(text="")
        self._set_group_state()

    _loading = False

    def _load_settings(self):
        self._loading = True
        try:
            cfg = self.app.cfg
            for key, row in self.rows.items():
                if key == "openai_api_key":
                    row["put"]("")
                elif key == "autostart":
                    row["put"](autostart_enabled())
                else:
                    row["put"](cfg.get(key, ""))
            key = cfg.get("openai_api_key") or ""
            env = os.environ.get("OPENAI_API_KEY")
            hint = ("Сейчас берётся из переменной окружения OPENAI_API_KEY." if not key and env else
                    f"Задан ({'…' + key[-4:] if len(key) > 8 else 'скрыт'}). Введите новый, чтобы заменить."
                    if key else "Нужен только для распознавания, перевода и правки через OpenAI.")
            self.rows["openai_api_key"]["hint"].configure(text=hint)
            self.dirty = False
            self.save_btn.configure(state="disabled")
            self._set_group_state()
        finally:
            self._loading = False

    def _toggle_autostart(self):
        if self._loading:
            return
        try:
            set_autostart(self.rows["autostart"]["get"]())
        except Exception as e:
            log.exception("автозапуск")
            self.save_msg.configure(text=f"Автозапуск: {e}", text_color=ERR_INK)

    def _save(self):
        changes = {k: r["get"]() for k, r in self.rows.items() if k != "autostart"}
        if not changes["openai_api_key"]:
            del changes["openai_api_key"]
        try:
            changes["typing_cpm"] = int(changes["typing_cpm"])
        except ValueError:
            self.save_msg.configure(text="Скорость печати — целое число", text_color=ERR_INK)
            return
        self.save_btn.configure(state="disabled")
        self.save_msg.configure(text="Сохраняю…", text_color=TEXT_3)

        def worker():
            reply = queue.Queue()
            self.app.events.put(("settings", changes, reply))
            try:
                res = reply.get(timeout=15)
            except queue.Empty:
                res = {"error": "Программа не ответила — попробуйте ещё раз"}
            self.cmds.put(("saved", res))
        threading.Thread(target=worker, daemon=True).start()

    def _on_saved(self, res):
        if res.get("error"):
            self.save_msg.configure(text=res["error"], text_color=ERR_INK)
            self.save_btn.configure(state="normal")
            return
        if res.get("restart"):
            self.need_restart = True
        if self.need_restart:
            self.restart_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(0, 10))
        self._load_settings()
        self.save_msg.configure(text="✓ Сохранено", text_color=GOOD)
        self.root.after(2500, lambda: self.save_msg.configure(text=""))
        self._refresh()

    def _restart(self):
        self.restart_btn.configure(state="disabled", text="Перезапускаю…")
        self.root.withdraw()
        threading.Thread(target=self.app.restart, daemon=True).start()
