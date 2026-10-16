"""Локальное распознавание: Silero VAD режет речь на фразы, GigaAM / Parakeet их распознаёт."""
import collections
import logging
import queue
import threading
import time

import numpy as np

from live_edit import LiveText
from realtime import CLAUSE_END, EN_MAX_WORDS, EN_MIN_WORDS, SENTENCE_END, _translate_piece_impl

log = logging.getLogger("dictation")

SAMPLE_RATE = 16000
VAD_HOP = 512              # 32 мс — размер кадра Silero на 16 кГц
VAD_CONTEXT = 64
SPEECH_ON, SPEECH_OFF = 0.5, 0.35
PREFIX_SEC = 0.3           # звук перед началом речи, чтобы не съесть первый слог
MAX_PHRASE_SEC = 20        # без пауз дольше — режем принудительно (модели рассчитаны на <25 с)
STREAM_MAX_PHRASE_SEC = 12 # в потоковом режиме фраза распознаётся заново много раз — держим её короче
INTERIM_SEC = 0.3          # как часто перераспознавать недоговорённую фразу


def _sess_options(threads=0):
    """Без spin-wait: по умолчанию потоки onnxruntime после каждого прогона ещё крутятся
    вхолостую в ожидании работы — это в 2–3 раза больше процессора (и батареи) при той же скорости.
    Silero VAD крошечный и зовётся каждые 32 мс — ему хватает одного потока."""
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")
    so.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return so


class LocalEngine:
    """Модели грузятся один раз при старте приложения и живут, пока оно запущено."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.ready = threading.Event()
        self.error = None
        self.asr = None
        self.vad = None
        self.lock = threading.Lock()   # onnxruntime-сессии распознавания — по одной фразе за раз
        threading.Thread(target=self._load, daemon=True, name="asr-load").start()

    def _load(self):
        try:
            import onnx_asr
            t0 = time.time()
            quant = self.cfg.get("local_quantization") or None
            self.asr = onnx_asr.load_model(self.cfg["local_model"], quantization=quant,
                                           sess_options=_sess_options())
            self.vad = onnx_asr.load_vad("silero", sess_options=_sess_options(threads=1))._model
            self.transcribe(np.zeros(SAMPLE_RATE, np.float32))   # прогрев
            log.info("локальная модель %s загружена за %.1fs", self.cfg["local_model"], time.time() - t0)
        except Exception as e:
            log.exception("локальная модель не загрузилась")
            self.error = e
        self.ready.set()

    def transcribe(self, audio):
        with self.lock:
            return (self.asr.recognize(audio, sample_rate=SAMPLE_RATE) or "").strip()

    def vad_state(self):
        return np.zeros((2, 1, 128), np.float32)

    def speech_prob(self, frame, state):
        out, state = self.vad.run(["output", "stateN"], {
            "input": frame[None, :], "state": state, "sr": np.array(SAMPLE_RATE, np.int64)})
        return float(out[0, 0]), state


class LocalSession:
    """Тот же интерфейс, что у RealtimeSession: одна сессия = одно удержание клавиши.

    Фраза кончается паузой vad_silence_ms. Пока она не кончилась (и включён local_streaming),
    её звук каждые INTERIM_SEC распознаётся заново, и печатаются слова, совпавшие в двух
    распознаваниях подряд (LocalAgreement, как в whisper_streaming). В конце фразы — финальное
    распознавание; если оно разошлось с напечатанным, хвост фразы исправляется.

    Режим перевода с локальным переводчиком (translate_engine = local): он быстрый, поэтому
    устоявшиеся слова фразы при каждом обновлении переводятся заново целиком, на экран идут слова,
    совпавшие в двух переводах подряд, а перевод финального текста исправляет хвост (см. _retranslator).

    С переводом через OpenAI устоявшиеся слова отдаются переводчику кусками (до запятой/точки или
    по EN_MAX_WORDS слов), как синхронному переводчику. Перевод в обоих случаях идёт в отдельном
    потоке и печатается по мере готовности, не задерживая распознавание."""

    def __init__(self, cfg, engine):
        self.cfg = cfg
        self.engine = engine
        self.dead = False
        self.started = False
        self.finishing = False
        self.audio = []            # откат на REST OpenAI не нужен — оставляем пустым
        self.audio_q = queue.Queue()
        self.cleanup = None                # задаёт App: функция правки текста или None
        self.can_edit = lambda: True
        self.prior = None                  # задаёт App: () -> текст перед курсором

    def usable(self, max_age):
        return not self.started

    def close(self):
        self.dead = True

    def begin(self, get_mode, emit, translate, on_done):
        self.started = True
        self.get_mode, self.emit, self.translate, self.on_done = get_mode, emit, translate, on_done
        threading.Thread(target=self._run, daemon=True, name="local-asr").start()

    def push(self, pcm):
        if not self.finishing:
            self.audio_q.put(pcm)

    def finish(self):
        self.finishing = True
        self.audio_q.put(None)

    # --- обработка

    def _run(self):
        self.streaming = bool(self.cfg.get("local_streaming", True))
        self.en = self.get_mode() == "en"
        self.retranslate = self.en and self.cfg.get("translate_engine") == "local"
        # живая правка — для русского текста, английский она не трогает
        self.live = LiveText(None if self.en else self.cleanup, self.can_edit, self.prior)
        tr = None
        if self.retranslate or (self.streaming and self.en):
            self.tr_q = queue.Queue()
            tr = threading.Thread(target=self._retranslator if self.retranslate else self._translator,
                                  daemon=True, name="local-translate")
            tr.start()
        failed = False
        try:
            if not self.engine.ready.wait(60) or self.engine.error:
                raise RuntimeError(f"локальная модель недоступна: {self.engine.error}")
            self._process()
        except Exception:
            log.exception("локальное распознавание")
            failed = True
        if tr:
            self.tr_q.put(None)
            tr.join(30)
        self.live.finish()
        self.typed_any = self.live.typed_any
        if self.typed_any and self.cfg.get("add_trailing_space", True):
            self.emit(" ")
        self.on_done(self, self.typed_any, failed)

    def _process(self):
        eng = self.engine
        silence_frames = max(1, int(self.cfg["vad_silence_ms"]) * SAMPLE_RATE // 1000 // VAD_HOP)
        prefix = int(PREFIX_SEC * SAMPLE_RATE)
        buf = np.zeros(0, np.float32)    # звук текущей (ещё не распознанной) фразы
        pos = 0                          # сколько звука из buf уже прошло через VAD
        state = eng.vad_state()
        context = np.zeros(VAD_CONTEXT, np.float32)
        speech_start = None              # начало речи в buf
        quiet = 0                        # кадров тишины подряд после речи
        probs = []                       # вероятности речи по кадрам текущей фразы
        max_phrase = (STREAM_MAX_PHRASE_SEC if self.streaming else MAX_PHRASE_SEC) * SAMPLE_RATE
        self._reset_interim()
        next_interim = 0                 # позиция в buf, после которой распознаём фразу заново

        while True:
            pcm = self.audio_q.get()
            if pcm is None:
                break
            buf = np.concatenate([buf, np.frombuffer(pcm, np.int16).astype(np.float32) / 32768])
            while pos + VAD_HOP <= len(buf):
                frame = buf[pos:pos + VAD_HOP]
                p, state = eng.speech_prob(np.concatenate([context, frame]), state)
                context = frame[-VAD_CONTEXT:]
                pos += VAD_HOP
                probs.append(p)
                if speech_start is None:
                    if p >= SPEECH_ON:
                        speech_start = max(0, pos - VAD_HOP - prefix)
                        quiet = 0
                    elif pos > prefix + VAD_HOP:
                        # до начала речи храним только хвост для prefix
                        cut = pos - prefix
                        buf, pos, probs = buf[cut:], pos - cut, []
                    continue
                quiet = quiet + 1 if p < SPEECH_OFF else 0
                if quiet >= silence_frames:
                    self._phrase(buf[speech_start:pos])
                    buf, pos, probs, speech_start = buf[pos:], 0, [], None
                    next_interim = 0
                elif pos - speech_start > max_phrase:
                    # режем по самому тихому кадру за последние 3 с
                    tail = probs[-int(3 * SAMPLE_RATE / VAD_HOP):]
                    cut = pos - (len(tail) - int(np.argmin(tail))) * VAD_HOP + VAD_HOP
                    self._phrase(buf[speech_start:cut])
                    buf, pos, probs, speech_start = buf[cut:], pos - cut, [], 0
                    next_interim = 0
            if (self.streaming and speech_start is not None and pos >= next_interim
                    and self.audio_q.empty() and pos - speech_start >= SAMPLE_RATE // 2):
                dt = self._interim(buf[speech_start:pos])
                # не перераспознаём чаще, чем успеваем: длинная фраза — реже
                next_interim = pos + int(max(INTERIM_SEC, 1.5 * dt) * SAMPLE_RATE)

        if speech_start is not None:
            self._phrase(buf[speech_start:])

    def _reset_interim(self):
        self.prev_words = []     # слова прошлого промежуточного распознавания
        self.shown_words = []    # слова фразы, уже напечатанные (в режиме перевода — устоявшиеся)
        self.sent = 0            # режим перевода: сколько слов фразы уже отдано переводчику

    def _interim(self, audio):
        t0 = time.time()
        words = self.engine.transcribe(audio).split()
        dt = time.time() - t0
        log.debug("interim %.1fs за %.2fs: %s", len(audio) / SAMPLE_RATE, dt, " ".join(words))
        # знаки препинания и регистр модель на обрывке фразы ставит наугад — сравниваем без них
        n = 0
        while (n < min(len(words), len(self.prev_words))
               and _norm(words[n]) == _norm(self.prev_words[n])):
            n += 1
        self.prev_words = words
        if self.en and not self.retranslate:
            if n > len(self.shown_words):
                self.shown_words = words[:n]
            self._send_stable(final_word=n == len(words))
            return dt
        if n > len(self.shown_words):
            self.shown_words = words[:n]
            text = " ".join(self.shown_words)
            if n == len(words):
                text = text.rstrip(".!?…")   # точка в конце обрывка — не настоящая, её поставит финал
            if self.retranslate:
                self.tr_q.put(("interim", text))
            else:
                self.live.stream_phrase(text, False)
        return dt

    def _send_stable(self, final_word):
        """Отдать переводчику готовый кусок устоявшихся слов: до последнего знака препинания
        (не короче EN_MIN_WORDS слов) или, если его нет, когда набралось EN_MAX_WORDS слов."""
        new = self.shown_words[self.sent:]
        # знак после последнего слова обрывка модель ставит наугад — на него не опираемся
        limit = len(new) - 1 if final_word else len(new)
        cut = 0
        for i in range(EN_MIN_WORDS - 1, limit):
            if new[i][-1] in CLAUSE_END:
                cut = i + 1
        if not cut and len(new) >= EN_MAX_WORDS:
            cut = len(new)
        if cut:
            self.tr_q.put((" ".join(new[:cut]), False))
            self.sent += cut

    def _translator(self):
        """Переводит куски по порядку с учётом уже переведённого и сразу печатает."""
        src_done = out_done = ""
        while True:
            item = self.tr_q.get()
            if item is None:
                return
            src, last = item
            src = (" " if src_done else "") + src
            t0 = time.time()
            out = _translate_piece_impl(self.translate, src, src_done, out_done, last)
            log.info("кусок переведён за %.2fs", time.time() - t0)
            src_done += src
            out_done += (" " if out_done and out and out[0] not in CLAUSE_END else "") + out
            if out:
                if out[0] not in CLAUSE_END:
                    out = self.live.lead() + out
                self.live.append(out, last, editable=False)

    def _retranslator(self):
        """Перевод локальной моделью. Каждый раз переводится вся текущая фраза; незаконченное
        предложение из прошлых фраз модель получает как уже готовое начало и продолжает его."""
        ctx = ("", "")          # незаконченное предложение прошлых фраз: исходник и перевод
        prev, shown = None, 0   # прошлый перевод текущей фразы (слова) и сколько его слов на экране
        pending = collections.deque()
        while True:
            if not pending:
                pending.append(self.tr_q.get())
            while True:
                try:
                    pending.append(self.tr_q.get_nowait())
                except queue.Empty:
                    break
            job = pending.popleft()
            if job is None:
                return
            kind, src = job
            if kind == "interim" and pending:
                continue        # перевод уже устарел: есть текст новее
            t0 = time.time()
            try:
                out = self.translate(src, ctx) if src else ""
            except Exception as e:
                if kind == "interim":
                    continue
                log.warning("перевод не удался: %s", e)
                out = src
            # фраза печатается через пробел, а запятую в начале модель ставит редко и не обязательно
            out = out.lstrip(CLAUSE_END + " ")
            if kind == "interim":
                words = out.split()
                n = 0
                while (prev is not None and n < min(len(words), len(prev))
                       and _norm(words[n]) == _norm(prev[n])):
                    n += 1
                prev = words
                if n > shown:
                    shown = n
                    text = " ".join(words[:n])
                    self.live.stream_phrase(text.rstrip(".!?…") if n == len(words) else text, False)
                continue
            log.info("фраза переведена за %.2fs: %s -> %s", time.time() - t0, src, out)
            self.live.stream_phrase(out, True)
            prev, shown = None, 0
            if src:
                ctx = (("", "") if src[-1] in SENTENCE_END
                       else (f"{ctx[0]} {src}".strip(), f"{ctx[1]} {out}".strip()))

    def _phrase(self, audio):
        shown = bool(self.shown_words)
        sent = self.sent
        self._reset_interim()
        if len(audio) < SAMPLE_RATE // 4 and not shown:
            return
        t0 = time.time()
        text = self.engine.transcribe(audio)
        log.info("фраза %.1fs распознана за %.2fs", len(audio) / SAMPLE_RATE, time.time() - t0)
        if self.retranslate:
            if text or shown:
                self.tr_q.put(("final", text))
            return
        if self.streaming and self.en:
            # уже отданные слова не трогаем — переводим только остаток фразы
            rest = text.split()[sent:]
            if rest:
                self.tr_q.put((" ".join(rest), True))
            return
        if self.streaming:
            if text or shown:
                self.live.stream_phrase(text, True)
            return
        if not text:
            return
        en = self.get_mode() == "en"
        if en:
            try:
                text = self.translate(text)
            except Exception as e:
                log.warning("перевод не удался: %s", e)
        self.live.add(text, editable=not en)


def _norm(word):
    return word.strip(".,!?…:;—-«»\"'()").lower().replace("ё", "е")
