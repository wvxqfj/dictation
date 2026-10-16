"""Учёт: сколько надиктовано, сколько это стоило в OpenAI и сколько времени сэкономлено.

Хранится по дням в stats.json рядом с программой. Цены — по прайсу OpenAI (USD за 1M токенов),
можно переопределить в config.json ключом "prices" в том же формате.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("dictation")

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json")

# USD за 1M токенов: audio — звук на входе, input — текст на входе, output — текст на выходе
PRICES = {
    "gpt-4o-transcribe":      {"audio": 6.00, "input": 2.50, "output": 10.00, "per_min": 0.006},
    "gpt-4o-mini-transcribe": {"audio": 3.00, "input": 1.25, "output": 5.00, "per_min": 0.003},
    "whisper-1":              {"per_min": 0.006},
    "gpt-live-transcribe":    {"per_min": 0.017},
    "gpt-4.1":      {"input": 2.00, "cached": 0.50, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "cached": 0.10, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "cached": 0.025, "output": 0.40},
    "gpt-4o":       {"input": 2.50, "cached": 1.25, "output": 10.00},
    "gpt-4o-mini":  {"input": 0.15, "cached": 0.075, "output": 0.60},
}

KINDS = ("transcribe", "cleanup", "translate", "rewrite")


def _empty_day():
    return {"sessions": 0, "seconds": 0.0, "chars": 0, "words": 0,
            "cost": {k: 0.0 for k in KINDS}}


class Stats:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.data = {"days": {}}
        try:
            with open(PATH, encoding="utf-8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("stats.json повреждён — начинаю заново")

    def _price(self, model):
        return {**PRICES.get(model, {}), **self.cfg.get("prices", {}).get(model, {})}

    def _day(self):
        d = self.data["days"].setdefault(time.strftime("%Y-%m-%d"), _empty_day())
        for k in KINDS:
            d["cost"].setdefault(k, 0.0)
        return d

    def _save(self):
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, PATH)

    def _add_cost(self, kind, usd):
        if usd <= 0:
            return
        with self.lock:
            self._day()["cost"][kind] += usd
            self._save()

    # --- что потрачено

    def chat_usage(self, kind, model, usage):
        """usage из ответа chat.completions."""
        if not usage:
            return
        p = self._price(model)
        if not p:
            log.warning("нет цены для модели %s — расход не учтён", model)
            return
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        fresh = (usage.prompt_tokens or 0) - cached
        usd = (fresh * p.get("input", 0) + cached * p.get("cached", p.get("input", 0))
               + (usage.completion_tokens or 0) * p.get("output", 0)) / 1e6
        self._add_cost(kind, usd)

    def transcribe_usage(self, model, usage):
        """usage из события realtime ...transcription.completed (словарь, тип tokens)."""
        p = self._price(model)
        det = usage.get("input_token_details") or {}
        audio = det.get("audio_tokens", usage.get("input_tokens", 0))
        text = det.get("text_tokens", 0)
        usd = (audio * p.get("audio", 0) + text * p.get("input", 0)
               + usage.get("output_tokens", 0) * p.get("output", 0)) / 1e6
        self._add_cost("transcribe", usd)

    def transcribe_seconds(self, model, seconds):
        """Когда сервер не прислал токены — считаем по длительности звука."""
        self._add_cost("transcribe", seconds / 60 * self._price(model).get("per_min", 0.006))

    # --- что надиктовано

    def session(self, seconds, text):
        text = text.strip()
        if not text:
            return
        with self.lock:
            d = self._day()
            d["sessions"] += 1
            d["seconds"] += seconds
            d["chars"] += len(text)
            d["words"] += len(text.split())
            self._save()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))
