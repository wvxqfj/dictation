"""Словарь терминов: локальная модель пишет английские слова как слышит («лоузе», «опен эй ай»,
«клаудко»), а нужно «Lolz», «OpenAI», «Claude Code». Здесь — замены после распознавания и список
терминов для подсказок OpenAI (распознавание и живая правка).

Свои термины — настройка «Мои слова» (`vocabulary`): через «;» или с новой строки,
«Написание = как слышится, ещё вариант», например «Proxyline = проксилайн; Avito = авито».
Термин без «=» только попадает в подсказки OpenAI."""
import logging
import re
import threading

log = logging.getLogger("dictation")

# Термины из истории диктовок и чатов. Варианты — как их пишет распознавание (бывает и латиницей:
# «open I», «Deep Sic», «PSH», «А PI»), без падежного
# окончания (оно добавляется само: «на лоузе» → «на Lolz»); пробел в варианте = пробел, дефис или ничего.
BUILTIN = [
    ("OpenAI", "опен эй ай, оупен эй ай, опен ай, оупен ай, опенай, опенэйай, open ai, open эй ай, open i, open a, open y, openie"),
    ("ChatGPT", "чат джи пи ти, чат джипити, чат гпт, чат gpt"),
    ("GPT", "джи пи ти, джипити, гпт"),
    ("API", "эй пи ай, апи, апишк, а pi, a pi, эй pi"),
    ("AI", "эй ай"),
    ("Lolzteam", "лолзтим, лоузтим, лолз тим, лоуз тим"),
    ("Lolz", "лолз, лоуз, лолс, лоулз, lols, louz"),
    ("Claude Code", "клод код, клауд код, клаудкод, клаудко, клодкод, cloud code, claude код, lod код"),
    ("Claude", "клод, клауд"),
    ("Anthropic", "антропик, энтропик"),
    ("Copilot", "копилот, капилот, копайлот, капайлот"),
    ("GitHub", "гитхаб, гит хаб, гитхап, githabe, githab"),
    ("Telegram", "телеграм"),
    ("Steam Guard", "стим гуард, стим гард, стимгуард, стимгард"),
    ("Steam", "стим"),
    ("G2G", "джи ту джи, джитуджи, джи два джи"),
    ("Qwen", "квен, qen, qan"),
    ("DeepSeek", "дипсик, дип сик, deep sic, deep see, deep seek"),
    ("Gemini", "джемини, гемини"),
    ("Python", "пайтон"),
    ("JSON", "джейсон, джсон"),
    ("SSH", "эс эс аш, эс эс эйч, эсэсаш, ссш, ссх, psh, sah, saz"),
    ("RDP", "эр ди пи, ардипи, рдп"),
    ("IP", "ай пи, айпишник, айпи"),
    ("URL", "ю ар эл, юрл, урл"),
    ("MCP", "эм си пи, эмсипи, мцп"),
    ("CRM", "си ар эм, црм"),
    ("VPN", "ви пи эн, випиэн, впн"),
    ("LLM", "эл эл эм, ллм"),
    ("HTML", "эйч ти эм эл"),
    ("Wi-Fi", "вай фай, вайфай"),
    ("Windows", "виндоус, виндовс"),
    ("Google", "гугл"),
    ("YouTube", "ютуб"),
    ("Instagram", "инстаграм"),
    ("TikTok", "тикток, тик ток"),
    ("WhatsApp", "ватсап, вотсап, вацап"),
    ("Excel", "эксел, эксель"),
    ("Docker", "докер"),
]
# падежные окончания, которые съедаются вместе со словом; дефис после слова можно («апи-ключ» → «API-ключ»)
_END = r"(?:а|я|е|у|ю|ом|ем|ой|ы|и|ов|ам|ами|ах|ка|ки|ке|ку|кой|ками)?"

_lock = threading.Lock()
_rules = []       # [(regex, написание)] — длинные варианты раньше коротких
_terms = []       # все написания — для подсказок OpenAI


def _parse(text):
    """«A = a1, a2; B» → [("A", "a1, a2"), ("B", "")]."""
    out = []
    for item in re.split(r"[;\n]+", text or ""):
        term, _, variants = item.partition("=")
        if term.strip():
            out.append((term.strip(), variants.strip()))
    return out


def _variant_re(v):
    parts = [re.escape(p) for p in v.lower().split()]
    return r"[\s-]*".join(parts)


def configure(user_text=""):
    """Пересобрать правила: встроенный словарь + «Мои слова» из настроек (они важнее)."""
    rules, terms = [], []
    for term, variants in _parse(user_text) + BUILTIN:
        if term not in terms:
            terms.append(term)
        for v in (x.strip() for x in variants.split(",")):
            if v:
                rules.append((len(v), re.compile(rf"(?<![\w-]){_variant_re(v)}{_END}(?!\w)", re.I), term))
    rules.sort(key=lambda r: -r[0])   # «клод код» раньше «клод», «опен эй ай» раньше «эй ай»
    with _lock:
        _rules[:] = [(r, t) for _, r, t in rules]
        _terms[:] = terms


def fix(text):
    """Заменить услышанные варианты терминов на правильное написание."""
    if not text:
        return text
    with _lock:
        rules = list(_rules)
    for rx, term in rules:
        text = rx.sub(term, text)
    return text


def terms():
    with _lock:
        return list(_terms)


def cleanup_hint():
    """Добавка к промпту живой правки OpenAI."""
    t = terms()
    if not t:
        return ""
    return ("\nТермины, которые говорящий часто использует, — пиши их ровно так (латиницей), даже если "
            "распознавание записало их кириллицей или исказило (например, «лоузе» → «Lolz», "
            "«опен эй ай» → «OpenAI», «клаудко» → «Claude Code»): " + ", ".join(t) + ".")


def transcribe_prompt(user_prompt=""):
    """Подсказка для распознавания OpenAI: своя подсказка + список терминов."""
    parts = [p for p in ((user_prompt or "").strip(), ", ".join(terms())) if p]
    return ". ".join(parts)


configure()
