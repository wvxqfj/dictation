"""Что вокруг курсора в чужом окне: текст перед курсором и выделенный текст.

Основной способ — UI Automation (работает в Блокноте, Word, браузерах и приложениях на Chromium/Electron):
читаем без буфера обмена и ничего не нажимаем. Если поле UI Automation не поддерживает, выделение
берём копированием (Ctrl+C), а прежнее содержимое буфера обмена сразу возвращаем на место."""
import ctypes
import ctypes.wintypes as wt
import logging
import queue
import sys
import threading
import time

import winapi as w

log = logging.getLogger("dictation")

BEFORE_CHARS = 300        # сколько текста перед курсором читать
MAX_SELECTION = 20000
UIA_TIMEOUT_MS = 1500     # дольше окно не ответило — считаем, что UI Automation в нём нет

# окна терминалов: Ctrl+C там прерывает программу, а набранный текст не заменяет выделение
CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty", "VirtualConsoleClass",
                   "PuTTY", "KiTTY"}

user32 = w.user32
kernel32 = w.kernel32
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.OpenClipboard.argtypes = [wt.HWND]
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.EnumClipboardFormats.argtypes = [wt.UINT]
user32.EnumClipboardFormats.restype = wt.UINT
user32.GetClipboardSequenceNumber.restype = wt.DWORD
user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wt.UINT
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalSize.argtypes = [wt.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.restype = wt.HGLOBAL

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x2
# данные этих форматов — не блок памяти (картинки GDI, метафайлы и т.п.): их не сохраняем,
# картинка всё равно вернётся через CF_DIB
_NOT_MEMORY = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
VK_C, VK_V = 0x43, 0x56


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_console(hwnd):
    return window_class(hwnd) in CONSOLE_CLASSES


# ---------------------------------------------------------------- UI Automation

class Uia:
    """Все вызовы UI Automation — в одном своём потоке (COM инициализируется в нём один раз).
    Если comtypes нет или UI Automation не запустилась, просто возвращаем None."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.api = None
        self.U = None
        self.error = None
        threading.Thread(target=self._run, daemon=True, name="uia").start()

    def _run(self):
        try:
            sys.coinit_flags = 0   # COINIT_MULTITHREADED: так советуют для клиентов UI Automation
            import comtypes
            import comtypes.client
            comtypes.CoInitializeEx(0)
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as U
            api = comtypes.client.CreateObject(U.CUIAutomation8, interface=U.IUIAutomation2)
            api.ConnectionTimeout = UIA_TIMEOUT_MS
            api.TransactionTimeout = UIA_TIMEOUT_MS
            self.U, self.api = U, api
        except Exception as e:
            log.warning("UI Automation недоступна: %s", e)
            self.error = e
        while True:
            fn, res = self.jobs.get()
            if self.api is None:
                res.put(None)
                continue
            try:
                res.put(fn())
            except Exception as e:
                log.debug("uia: %s", e)
                res.put(None)

    def submit(self, fn):
        """Запустить fn в потоке UI Automation; вернуть функцию get(timeout) -> результат или None."""
        res = queue.Queue()
        self.jobs.put((fn, res))
        box = []

        def get(timeout):
            if not box:
                try:
                    box.append(res.get(timeout=timeout))
                except queue.Empty:
                    return None
            return box[0]
        return get

    def caret(self, before_chars=BEFORE_CHARS):
        """get(timeout) -> словарь о поле с фокусом или None (UI Automation в нём нет):
        before — текст перед курсором (или перед выделением), selection — выделенный текст,
        editable — False, если поле точно только для чтения, rect — где на экране выделение/курсор."""
        return self.submit(lambda: self._caret(before_chars))

    def _caret(self, before_chars):
        U = self.U
        el = self.api.GetFocusedElement()
        if not el:
            return None
        editable = None
        vp = el.GetCurrentPattern(U.UIA_ValuePatternId)
        if vp:
            editable = not vp.QueryInterface(U.IUIAutomationValuePattern).CurrentIsReadOnly
        tp = el.GetCurrentPattern(U.UIA_TextPatternId)
        if not tp:
            return None
        tp = tp.QueryInterface(U.IUIAutomationTextPattern)
        sel = tp.GetSelection()
        if not sel or not sel.Length:
            return None   # каретки в поле нет — не знаем, куда пойдёт текст
        r = sel.GetElement(0)
        info = {"editable": editable, "selection": r.GetText(MAX_SELECTION) or "", "rect": None}
        b = r.Clone()
        b.MoveEndpointByRange(U.TextPatternRangeEndpoint_End, r, U.TextPatternRangeEndpoint_Start)
        b.MoveEndpointByUnit(U.TextPatternRangeEndpoint_Start, U.TextUnit_Character, -before_chars)
        info["before"] = (b.GetText(-1) or "").replace("\r\n", "\n").replace("\r", "\n")
        rects = list(r.GetBoundingRectangles() or ())
        if len(rects) >= 4:
            xs = [(rects[i], rects[i + 1], rects[i] + rects[i + 2], rects[i + 1] + rects[i + 3])
                  for i in range(0, len(rects) - 3, 4)]
            info["rect"] = (min(x[0] for x in xs), min(x[1] for x in xs),
                            max(x[2] for x in xs), max(x[3] for x in xs))
        return info


# ---------------------------------------------------------------- буфер обмена

def _open_clipboard(owner):
    for _ in range(50):
        if user32.OpenClipboard(owner):
            return True
        time.sleep(0.01)
    return False


def _save_clipboard(owner):
    """[(формат, байты)] всего, что сейчас в буфере обмена (кроме картинок GDI — см. _NOT_MEMORY)."""
    saved = []
    if not _open_clipboard(owner):
        return None
    try:
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in _NOT_MEMORY or 0x200 <= fmt <= 0x3FF:
                continue
            h = user32.GetClipboardData(fmt)
            size = kernel32.GlobalSize(h) if h else 0
            if not size:
                continue
            p = kernel32.GlobalLock(h)
            if p:
                saved.append((fmt, ctypes.string_at(p, size)))
                kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()
    return saved


def _set_clipboard(owner, items):
    """Положить в буфер обмена [(формат, байты)]. Пометка Exclude… — чтобы наши временные записи
    не копились в журнале буфера обмена (Win+V)."""
    if not _open_clipboard(owner):
        return False
    try:
        user32.EmptyClipboard()
        for fmt, data in list(items) + [(user32.RegisterClipboardFormatW(
                "ExcludeClipboardContentFromMonitorProcessing"), b"\0")]:
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(fmt, h):
                kernel32.GlobalFree(h)
    finally:
        user32.CloseClipboard()
    return True


def _read_text(owner):
    if not _open_clipboard(owner):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        p = kernel32.GlobalLock(h) if h else None
        if not p:
            return None
        try:
            text = ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
        # VS Code и JetBrains без выделения копируют всю строку — это не выделение
        vsc = user32.GetClipboardData(user32.RegisterClipboardFormatW("vscode-editor-data"))
        if vsc:
            q = kernel32.GlobalLock(vsc)
            if q:
                meta = ctypes.string_at(q, kernel32.GlobalSize(vsc))
                kernel32.GlobalUnlock(vsc)
                if b'"isFromEmptySelection":true' in meta.replace(b" ", b""):
                    return None
        return text
    finally:
        user32.CloseClipboard()


def _combo(vk):
    w.send_keys((w.VK_CONTROL, False), (vk, False), (vk, True), (w.VK_CONTROL, True))


def copy_selection(owner, timeout=0.6):
    """Выделенный текст активного окна через Ctrl+C или None, если ничего не скопировалось.
    Буфер обмена потом возвращается к прежнему содержимому."""
    saved = _save_clipboard(owner)
    if saved is None:
        return None
    seq = user32.GetClipboardSequenceNumber()
    _combo(VK_C)
    end = time.time() + timeout
    while user32.GetClipboardSequenceNumber() == seq and time.time() < end:
        time.sleep(0.01)
    if user32.GetClipboardSequenceNumber() == seq:
        return None
    time.sleep(0.03)
    try:
        return _read_text(owner)
    finally:
        _set_clipboard(owner, saved)


def paste_text(owner, text, settle=0.6):
    """Вставить текст в активное окно через буфер обмена (Ctrl+V) и вернуть прежний буфер.
    Так переносы строк сохраняются, а замена — один шаг отмены (Ctrl+Z)."""
    saved = _save_clipboard(owner)
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le") + b"\0\0"
    if not _set_clipboard(owner, [(CF_UNICODETEXT, data)]):
        raise RuntimeError("буфер обмена занят")
    _combo(VK_V)
    time.sleep(settle)   # приложение читает буфер не сразу — возвращаем прежний, когда оно уже вставило
    if saved is not None:
        _set_clipboard(owner, saved)
