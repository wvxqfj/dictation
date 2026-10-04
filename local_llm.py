"""Локальная живая правка: небольшая языковая модель через llama.cpp (llama-server), без интернета.
Сервер запускается один раз в фоне, слушает только 127.0.0.1 и закрывается вместе с приложением."""
import ctypes
import ctypes.wintypes as wt
import io
import json
import logging
import os
import platform
import socket
import subprocess
import threading
import time
import urllib.request
import zipfile

log = logging.getLogger("dictation")
_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # к 127.0.0.1 — мимо системного прокси

# Сборка llama.cpp зафиксирована: эту версию проверяли. Под ARM (Snapdragon) берём родную ARM64-сборку —
# она в разы быстрее x64 под эмуляцией.
LLAMA_BUILD = "b11377"
LLAMA_URL = "https://github.com/ggml-org/llama.cpp/releases/download/{b}/llama-{b}-bin-win-cpu-{arch}.zip"
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Dictation")

# ключ -> (репозиторий HF, ревизия, файл). Ревизии зафиксированы: эти сборки проверяли.
MODELS = {
    "qwen3.5-2b": ("unsloth/Qwen3.5-2B-GGUF", "f6d5376be1edb4d416d56da11e5397a961aca8ae", "Qwen3.5-2B-Q4_0.gguf"),
    "qwen3.5-4b": ("unsloth/Qwen3.5-4B-GGUF", "e87f176479d0855a907a41277aca2f8ee7a09523", "Qwen3.5-4B-Q4_0.gguf"),
}
DEFAULT_MODEL = "qwen3.5-2b"

# половина ядер: вторая половина остаётся распознаванию, которое идёт одновременно
THREADS = max(2, min(8, (os.cpu_count() or 4) // 2))
CTX = 3072
# Память: веса через mmap (страницы файла ОС может выгрузить без подкачки), без кэша промптов в RAM
# и с небольшим батчем — у Qwen огромный словарь, и буфер логитов на батч 512 весит сотни мегабайт.
MEMORY = ["--cache-ram", "0", "-b", "256", "-ub", "256"]
# Правка почти целиком переписывает входной текст, поэтому угадываем продолжение по n-граммам
# из запроса (prompt lookup): генерация втрое быстрее, а результат тот же, что без угадывания.
SPEC = ["--spec-type", "ngram-simple", "--spec-ngram-simple-size-n", "2", "--spec-ngram-simple-size-m", "24"]


def _arch():
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def _llama_dir():
    d = os.path.join(CACHE_DIR, f"llama-{LLAMA_BUILD}-{_arch()}")
    exe = os.path.join(d, "llama-server.exe")
    if os.path.exists(exe):
        return exe
    url = LLAMA_URL.format(b=LLAMA_BUILD, arch=_arch())
    log.info("скачиваю llama.cpp %s (%s)", LLAMA_BUILD, _arch())
    data = urllib.request.urlopen(url, timeout=60).read()
    tmp = d + ".tmp"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(tmp)
    os.replace(tmp, d)
    return exe


def _model_path(key):
    from huggingface_hub import hf_hub_download
    repo, rev, fname = MODELS[key]
    try:
        return hf_hub_download(repo, fname, revision=rev, local_files_only=True)
    except Exception:
        log.info("скачиваю модель правки %s", fname)
        return hf_hub_download(repo, fname, revision=rev)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD),
                ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kill_with_us(proc):
    """Сервер умирает вместе с приложением, даже если оно упало: job object с KILL_ON_JOB_CLOSE."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wt.HANDLE
    k.OpenProcess.restype = wt.HANDLE
    job = k.CreateJobObjectW(None, None)
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k.SetInformationJobObject(wt.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    h = k.OpenProcess(0x0101, False, proc.pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    k.AssignProcessToJobObject(wt.HANDLE(job), wt.HANDLE(h))
    k.CloseHandle(wt.HANDLE(h))
    return job   # держим хэндл открытым, пока живёт процесс Python


class LocalLLM:
    """Модель грузится один раз в фоне и живёт, пока запущено приложение."""

    def __init__(self, model_key, warmup=None):
        self.key = model_key if model_key in MODELS else DEFAULT_MODEL
        self.warmup = warmup            # запрос для прогрева: общее начало промпта попадает в кэш
        self.ready = threading.Event()
        self.error = None
        self.noted = False              # пользователю уже сказали, что модель грузится / не загрузилась
        self.last_used = time.time()
        self.proc = None
        self._job = None
        threading.Thread(target=self._load, daemon=True, name="llm-load").start()

    def _load(self):
        try:
            t0 = time.time()
            exe, model = _llama_dir(), _model_path(self.key)
            self.port = _free_port()
            self.proc = subprocess.Popen(
                [exe, "-m", model, "--host", "127.0.0.1", "--port", str(self.port), "-t", str(THREADS),
                 "-c", str(CTX), "-np", "1", "-fa", "on", "--no-webui", "--log-disable", *MEMORY, *SPEC],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self._job = _kill_with_us(self.proc)
            while True:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"llama-server завершился с кодом {self.proc.returncode}")
                try:
                    _local.open(f"http://127.0.0.1:{self.port}/health", timeout=1)
                    break
                except Exception:
                    if time.time() - t0 > 120:
                        raise RuntimeError("llama-server не запустился за 2 минуты")
                    time.sleep(0.2)
            self.ready.set()
            if self.warmup:
                try:
                    self.chat(self.warmup, max_tokens=8)
                except Exception as e:
                    log.warning("прогрев модели правки не удался: %s", e)
            log.info("локальная модель правки %s загружена за %.1fs", self.key, time.time() - t0)
        except Exception as e:
            log.exception("локальная модель правки не загрузилась")
            self.error = e
            self.ready.set()

    def chat(self, messages, max_tokens=400, wait=120):
        if not self.ready.wait(wait):
            raise RuntimeError("локальная модель правки ещё загружается")
        if self.error:
            raise RuntimeError(f"локальная модель правки недоступна: {self.error}")
        self.last_used = time.time()
        body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens, "cache_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                     json.dumps(body).encode(), {"Content-Type": "application/json"})
        with _local.open(req, timeout=30) as r:
            return json.load(r)["choices"][0]["message"]["content"] or ""

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(3)   # память освобождается до того, как стартует другая модель
            except subprocess.TimeoutExpired:
                pass


class Online:
    """Есть ли связь с OpenAI. Пока всё работает — ничего не проверяем; после сетевой ошибки
    считаем, что интернета нет, и раз в RECHECK секунд пробуем открыть TCP-соединение."""
    HOST, RECHECK = ("api.openai.com", 443), 20

    def __init__(self):
        self.ok = True
        self.checked = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            if self.ok or time.time() - self.checked < self.RECHECK:
                return self.ok
            self.checked = time.time()
        try:
            socket.create_connection(self.HOST, timeout=1.5).close()
            ok = True
        except OSError:
            ok = False
        with self.lock:
            if ok and not self.ok:
                log.info("интернет снова есть — правка через OpenAI")
            self.ok = ok
        return ok

    def lost(self, err):
        with self.lock:
            if self.ok:
                log.warning("нет связи с OpenAI (%s) — правка локально", err)
            self.ok, self.checked = False, time.time()
