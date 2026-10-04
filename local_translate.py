"""Локальный перевод на английский: OPUS-MT tc-big (русский, украинский, белорусский → английский)
через CTranslate2. Фраза в 10–15 слов переводится за ~0,1 с на процессоре, без интернета."""
import logging
import os
import re
import threading
import time

log = logging.getLogger("dictation")

# Helsinki-NLP/opus-mt-tc-big-zle-en, сконвертированная в формат CTranslate2 (int8, ~250 МБ).
# Ревизия зафиксирована: эту сборку проверяли, а чужой репозиторий может поменяться.
REPO = "luigi000/opus-mt-tc-big-zle-en-ct2-int8"
REVISION = "987aa3c46cf871ea683bcc7a540e3053d372cb71"
FILES = ["config.json", "model.bin", "shared_vocabulary.json", "source.spm", "target.spm"]
BEAM = 1                  # жадный поиск: вдвое быстрее луча 4, на диктовке разница едва заметна
MAX_CONTEXT_WORDS = 80    # незаконченное предложение длиннее — новый кусок переводим без него
SENTENCE_END = ".!?…"
CLAUSE_END = SENTENCE_END + ",;:—"
_END = re.compile(r"[.!?…]+(?=\s|$)")


class LocalTranslator:
    """Модель грузится один раз в фоне и живёт, пока запущено приложение."""

    def __init__(self):
        self.ready = threading.Event()
        self.error = None
        threading.Thread(target=self._load, daemon=True, name="mt-load").start()

    def _load(self):
        try:
            import ctranslate2
            import sentencepiece as spm
            t0 = time.time()
            path = _download()
            # половина ядер: вторая половина остаётся распознаванию, которое идёт одновременно
            self.model = ctranslate2.Translator(path, device="cpu", compute_type="int8", inter_threads=1,
                                                intra_threads=max(1, min(8, (os.cpu_count() or 2) // 2)))
            self.sp_src = spm.SentencePieceProcessor(model_file=os.path.join(path, "source.spm"))
            self.sp_tgt = spm.SentencePieceProcessor(model_file=os.path.join(path, "target.spm"))
            self._run("Привет.", "")   # прогрев
            log.info("локальный переводчик загружен за %.1fs", time.time() - t0)
        except Exception as e:
            log.exception("локальный переводчик не загрузился")
            self.error = e
        self.ready.set()

    def translate(self, text, context=None):
        """Перевести text. context = (исходный текст, его перевод) — то, что уже напечатано. Если там
        осталось незаконченное предложение, модель получает его исходник целиком, а готовый английский —
        как начало ответа, и переводит text как продолжение, а не как отдельный обрывок."""
        if not self.ready.wait(120):
            raise RuntimeError("локальный переводчик ещё загружается")
        if self.error:
            raise RuntimeError(f"локальный переводчик недоступен: {self.error}")
        text = text.strip()
        if not text:
            return ""
        src_ctx, out_ctx = _sentence_context(*(context or ("", "")))
        if out_ctx:
            out = self._run(f"{src_ctx} {text}", out_ctx)
            if len(self.sp_tgt.encode(out)) < 0.4 * len(self.sp_src.encode(text)):
                # с готовым началом модель иногда считает предложение законченным и бросает новый кусок
                log.info("перевод продолжения оборвался (%r) — перевожу кусок отдельно", out)
                out = self._run(text, "")
                if text[0].islower() and out[:1].isupper() and not re.match(r"I\b|[A-Z]{2}", out):
                    out = out[0].lower() + out[1:]   # середина предложения
        else:
            out = self._run(text, "")
        return _match_end(text, out)

    def _run(self, src, prefix):
        tokens = self.sp_src.encode(src, out_type=str) + ["</s>"]
        pre = self.sp_tgt.encode(prefix, out_type=str) if prefix else []
        res = self.model.translate_batch([tokens], target_prefix=[pre] if pre else None, beam_size=BEAM,
                                         max_decoding_length=len(pre) + 2 * len(tokens) + 10)
        return self.sp_tgt.decode(res[0].hypotheses[0][len(pre):]).strip()


def _download():
    from huggingface_hub import snapshot_download
    try:
        return snapshot_download(REPO, revision=REVISION, allow_patterns=FILES, local_files_only=True)
    except Exception:
        log.info("скачиваю модель перевода %s (~250 МБ)", REPO)
        return snapshot_download(REPO, revision=REVISION, allow_patterns=FILES)


def _sentence_context(src, out):
    """Незаконченное предложение в конце уже переведённого: (исходник, перевод) или ("", "").
    Предложения сопоставляем по счёту: n-й конец предложения в исходнике — n-й в переводе."""
    src_ends = [m.end() for m in _END.finditer(src)]
    out_ends = [m.end() for m in _END.finditer(out)]
    if len(out_ends) < len(src_ends):
        return "", ""
    s = src[src_ends[-1]:] if src_ends else src
    o = out[out_ends[len(src_ends) - 1]:] if src_ends else out
    s, o = s.strip(), o.strip()
    if not s or not o or len(s.split()) > MAX_CONTEXT_WORDS:
        return "", ""
    return s, o


def _match_end(src, out):
    """Знак в конце перевода — как у исходника: модель по привычке ставит точку и там,
    где фраза оборвалась паузой или кончается запятой."""
    end = src.rstrip()[-1:]
    if not out or end in SENTENCE_END:
        return out
    out = out.rstrip(CLAUSE_END + " ")
    return out + end if out and end in ",;:" else out
