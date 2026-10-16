"""Потоковое распознавание через OpenAI Realtime API (режим transcription)."""
import base64
import json
import logging
import queue
import threading
import time

import websocket

from live_edit import LiveText

log = logging.getLogger("dictation")

URL = "wss://api.openai.com/v1/realtime?intent=transcription"
SAMPLE_RATE = 24000
SENTENCE_END = ".!?…"
CLAUSE_END = SENTENCE_END + ",;:—"
# синхронный перевод: когда отдавать переводчику очередной кусок
EN_MIN_WORDS = 2       # кусок до запятой/точки — не короче стольких слов
EN_MAX_WORDS = 6       # без знаков препинания — переводим каждые столько слов
EN_PAUSE = 0.6         # пауза в речи (нет новых слов) — переводим всё, что есть


def is_live(model):
    """Потоковые модели: текст идёт по ходу речи, без деления на фразы (server_vad не поддерживают)."""
    return model.startswith(("gpt-live-transcribe", "gpt-realtime-whisper"))


class RealtimeSession:
    """Одна сессия = одно удержание клавиши. Подключается заранее, чтобы не ждать при нажатии.

    Порядок вывода: сегменты (items) печатаются строго в порядке commit,
    в обычном режиме — по мере прихода дельт, в режиме перевода — короткими кусками (см. _ready)
    с учётом уже переведённого, как синхронный переводчик.
    У потоковых моделей (is_live) вся сессия — один сегмент, дельты идут ещё до commit.
    """

    def __init__(self, cfg, api_key):
        self.cfg = cfg
        self.api_key = api_key
        self.created = time.time()
        self.ws = None
        self.send_lock = threading.Lock()
        self.ready = threading.Event()
        self.dead = False
        self.started = False

        self.audio_q = queue.Queue()
        self.audio = []                 # копия всего звука на случай отката на REST
        self.cond = threading.Condition()
        self.order = []
        self.items = {}
        self.final_sent = False
        self.final_sent_at = 0.0
        self.final_committed = False
        self.finishing = False
        self.cleanup = None                # задаёт App: функция правки текста или None
        self.can_edit = lambda: True
        self.prior = None                  # задаёт App: () -> текст перед курсором
        self.on_usage = None               # задаёт App: учёт расхода, вызывается с usage сегмента
        self.usage_seen = False

        threading.Thread(target=self._connect, daemon=True, name="rt-connect").start()

    # --- соединение

    def _session_update(self):
        model = self.cfg["transcribe_model"]
        tr = {"model": model}
        if self.cfg.get("language"):
            tr["languages" if is_live(model) else "language"] = (
                [self.cfg["language"]] if is_live(model) else self.cfg["language"])
        if self.cfg.get("transcribe_prompt"):
            tr["prompt"] = self.cfg["transcribe_prompt"]
        if is_live(model):
            tr["delay"] = self.cfg.get("live_delay") or "low"
            turn = None
        else:
            turn = {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300,
                    "silence_duration_ms": int(self.cfg["vad_silence_ms"])}
        inp = {
            "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
            "transcription": tr,
            "turn_detection": turn,
        }
        if self.cfg.get("noise_reduction"):
            inp["noise_reduction"] = {"type": self.cfg["noise_reduction"]}
        return {"type": "session.update",
                "session": {"type": "transcription", "audio": {"input": inp}}}

    def _send(self, obj):
        with self.send_lock:
            self.ws.send(json.dumps(obj))

    def _connect(self):
        try:
            t0 = time.time()
            self.ws = websocket.create_connection(
                URL, header=[f"Authorization: Bearer {self.api_key}"], timeout=10)
            self.ws.settimeout(None)
            self._send(self._session_update())
            log.info("realtime: подключено за %.2fs", time.time() - t0)
        except Exception as e:
            log.warning("realtime: не удалось подключиться: %s", e)
            self._mark_dead()
            return
        self._rx()

    def _mark_dead(self):
        with self.cond:
            self.dead = True
            self.cond.notify_all()
        self.ready.set()

    def usable(self, max_age):
        return not self.dead and not self.started and time.time() - self.created < max_age

    def close(self):
        self.dead = True
        ws = self.ws

        def _close():
            try:
                ws.close(timeout=1)
            except Exception:
                pass
        if ws:
            # закрытие ждёт ответа сервера — не держим из-за этого остальное
            threading.Thread(target=_close, daemon=True).start()

    def _rx(self):
        while True:
            try:
                raw = self.ws.recv()
            except Exception:
                break
            if not raw:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            t = m.get("type", "")
            with self.cond:
                if t == "session.updated":
                    if self.final_sent:
                        # ответ на повторный session.update: commit уже обработан сервером
                        self.final_committed = True
                    self.ready.set()
                elif t == "input_audio_buffer.committed":
                    self._add_item(m["item_id"])
                elif t == "conversation.item.input_audio_transcription.delta":
                    # у потоковых моделей дельты приходят раньше commit
                    it = self._add_item(m["item_id"])
                    it["text"] += m.get("delta", "")
                    it["t"] = time.time()
                elif t == "conversation.item.input_audio_transcription.completed":
                    it = self.items.setdefault(m["item_id"], _item())
                    it["final"] = m.get("transcript", "") or ""
                    it["done"] = True
                    # потоковые модели оплачиваются поминутно — App считает по длительности звука
                    if (m.get("usage") and self.on_usage and self.started
                            and not is_live(self.cfg["transcribe_model"])):
                        self.usage_seen = True
                        try:
                            self.on_usage(m["usage"])
                        except Exception:
                            log.exception("учёт расхода")
                elif t == "conversation.item.input_audio_transcription.failed":
                    it = self.items.setdefault(m["item_id"], _item())
                    it["done"] = True
                    log.warning("realtime: сегмент не распознан: %s", m.get("error"))
                elif t == "error":
                    err = m.get("error", {})
                    if err.get("code") != "input_audio_buffer_commit_empty":
                        log.warning("realtime error: %s", err)
                    if not self.ready.is_set():
                        self.dead = True
                        self.ready.set()
                self.cond.notify_all()
        self._mark_dead()

    def _translate_piece(self, src, src_done, out_done, last):
        return _translate_piece_impl(self.translate, src, src_done, out_done, last)

    def _add_item(self, iid):
        if iid not in self.items:
            self.items[iid] = _item()
            self.order.append(iid)
        return self.items[iid]

    # --- запись

    def begin(self, get_mode, emit, translate, on_done):
        self.started = True
        self.get_mode, self.emit, self.translate, self.on_done = get_mode, emit, translate, on_done
        self.live = LiveText(self.cleanup, self.can_edit, self.prior)
        threading.Thread(target=self._sender, daemon=True, name="rt-send").start()
        threading.Thread(target=self._output, daemon=True, name="rt-out").start()

    def push(self, pcm):
        if not self.finishing:
            self.audio.append(pcm)
            self.audio_q.put(pcm)

    def finish(self):
        self.finishing = True
        self.audio_q.put(None)

    def _sender(self):
        if not self.ready.wait(10):
            log.warning("realtime: сервер не ответил")
            self._mark_dead()
        done = False
        while not done:
            chunks = [self.audio_q.get()]
            while True:
                try:
                    chunks.append(self.audio_q.get_nowait())
                except queue.Empty:
                    break
            if None in chunks:
                done = True
                chunks = chunks[:chunks.index(None)]
            if self.dead or not chunks:
                continue
            try:
                self._send({"type": "input_audio_buffer.append",
                            "audio": base64.b64encode(b"".join(chunks)).decode()})
            except Exception as e:
                log.warning("realtime: ошибка отправки: %s", e)
                self._mark_dead()
        if self.dead:
            return
        try:
            with self.cond:
                self.final_sent = True
                self.final_sent_at = time.time()
            self._send({"type": "input_audio_buffer.commit"})
            # сервер обрабатывает события по порядку: ответ на этот update придёт
            # только после commit, значит после него новых сегментов не будет
            self._send(self._session_update())
        except Exception as e:
            log.warning("realtime: ошибка commit: %s", e)
            self._mark_dead()

    def _output(self):
        idx = 0
        typed_any = False
        failed = False
        src_done = out_done = ""      # для перевода: что уже переведено и как
        while True:
            with self.cond:
                while True:
                    if idx < len(self.order):
                        it = self.items[self.order[idx]]
                        if it["done"] or len(_ready(it, self.get_mode())) > it["emitted"]:
                            break
                        if self.get_mode() == "en" and it["text"]:
                            self.cond.wait(0.1)   # ждём паузу в речи — проверяем почаще
                            continue
                    elif self.final_committed:
                        break
                    if self.dead:
                        failed = True
                        break
                    self.cond.wait(0.5)
                    if self.final_sent and time.time() - self.final_sent_at > 15:
                        log.warning("realtime: таймаут ожидания результата")
                        failed = True
                        break
                if failed or idx >= len(self.order):
                    break
                it = self.items[self.order[idx]]
                en = self.get_mode() == "en"
                if it["done"] and it["emitted"] == 0 and not it["text"]:
                    it["text"] = it["final"]
                ready = _ready(it, "en" if en else "native")
                chunk = ready[it["emitted"]:]
                it["emitted"] = len(ready)
                item_done = it["done"] and it["emitted"] >= len(it["text"])

            if en and chunk.strip():
                src = chunk
                chunk = self._translate_piece(src, src_done, out_done, item_done)
                src_done += src
                out_done += (" " if out_done and chunk and chunk[0] not in CLAUSE_END else "") + chunk
            if chunk:
                if not it["started"]:
                    chunk = chunk.lstrip()
                    if chunk:
                        it["started"] = True
                        if chunk[0] not in CLAUSE_END:
                            chunk = self.live.lead() + chunk
                if chunk:
                    self.live.append(chunk, item_done, editable=not en)
                    typed_any = True
            if item_done:
                self.live.mark_done()
                idx += 1
            elif en and chunk:
                it["started"] = False   # следующий переведённый кусок — через пробел

        self.live.finish()
        if typed_any and self.cfg.get("add_trailing_space", True):
            self.emit(" ")
        self.close()
        self.on_done(self, typed_any, failed)


def _translate_piece_impl(translate, src, src_done, out_done, last):
    """Перевод очередного куска для синхронного перевода. Знаки препинания и заглавные буквы на
    стыках кусков берём из исходного текста, а не у модели: она видит обрывок и норовит поставить
    точку в конце и начать с маленькой буквы невпопад."""
    body = src.strip()
    lead = ""
    while body and body[0] in CLAUSE_END:     # точка/запятая от прошлой фразы пришла с опозданием
        lead += body[0]
        body = body[1:].lstrip()
    if lead and out_done.rstrip()[-1:] in CLAUSE_END:
        lead = ""
    if not body:
        return lead
    before = (src_done.strip() + lead).rstrip()
    new_sentence = not before or before[-1] in SENTENCE_END
    try:
        out = translate(body, (src_done + lead, out_done + lead)).strip()
    except Exception as e:
        log.warning("перевод не удался: %s", e)
        out = body
    if not last and body[-1] not in CLAUSE_END:
        out = out.rstrip(".!?…;: ")           # фраза не закончена — точку поставит исходник
    if out:
        if new_sentence:
            out = out[0].upper() + out[1:]
        elif out[0].isupper() and not out.startswith(("I ", "I'")) and out.split()[0].lower() in _LOWER_OK:
            out = out[0].lower() + out[1:]
    return lead + (" " if lead and out else "") + out


# служебные слова, которые посреди предложения точно пишутся с маленькой буквы
_LOWER_OK = {"and", "but", "or", "so", "because", "the", "a", "an", "it", "this", "that", "which", "who",
             "while", "when", "if", "then", "please", "to", "of", "in", "on", "at", "for", "with",
             "as", "is", "are", "was", "were", "we", "you", "they", "he", "she", "my", "your", "our"}


def _ready(it, mode):
    """Сколько текста сегмента можно выводить. Обычно — всё пришедшее. Для перевода — до конца
    фразы/придаточного, или каждые EN_MAX_WORDS слов, или всё, если говорящий замолчал."""
    text = it["text"]
    if mode != "en" or it["done"] or time.time() - it["t"] >= EN_PAUSE:
        return text
    start = it["emitted"]
    # последнее слово может ещё дописываться — берём только до последнего пробела
    complete = text[:text.rfind(" ") + 1] if " " in text[start:] else text[:start]
    words = complete[start:].split()
    cut = start
    for i in range(start, len(complete) - 1):
        if complete[i] in CLAUSE_END and complete[i + 1] == " " and len(text[start:i].split()) >= EN_MIN_WORDS:
            cut = i + 1
    if cut == start and len(words) >= EN_MAX_WORDS:
        cut = len(complete)
    return text[:cut]


def _item():
    return {"text": "", "final": "", "done": False, "emitted": 0, "started": False, "t": time.time()}
