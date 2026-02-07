# -*- coding: utf-8 -*-
"""
Šis įrankis skirtas tik testavimui / asmeniniams eksperimentams.
NENAUDOKITE PRODUKCINĖJE APLINKOJE.

Prieš naudodami įsivertinkite:
- ar turite teisę / leidimą tikrinti trečiųjų šalių puslapius tokiu būdu,
- ar nepažeidžiate taisyklių,
- ar neapkraunate serverių.

OPTIMIZACIJOS (2026):
- „Visi ID“ lentelė rodoma puslapiais (nebekuriama 50k+ DOM eilučių).
- Batch dydžiai UI papildyti (100..1000) + serverio MAX_BATCH_IDS default=1000.
- RAW_CACHE apribotas (LRU) – kad serveris nepradėtų valgyti RAM.
- Persistencijos (state) išsaugojimas į diską „throttle“ (nebepersistinama po kiekvieno ID).
- Pridėtas /api/cache_batch: UI puslapio statusus užkrauna iš cache be fetch į tikslą.
- PRIDĖTA: greičio rodymas (kiek realių fetch'ų per minutę) UI.
- PRIDĖTA: iki 3 lygiagrečių užklausų į tikslinę svetainę (ThreadPoolExecutor + semaphore).

ŠI VERSIJA:
- TIKRINA VISUS ID IŠ EILĖS (tiek lyginius, tiek nelyginius) -> STEP=1.
"""

import re
import random
import time
import threading
import html
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, Response
import requests
from bs4 import BeautifulSoup

# =========================
# Konfigūracija (DEFAULT)
# =========================
DEFAULT_START_NUM = 3000000
DEFAULT_END_NUM = 3000033
DEFAULT_STEP = 1  # VISI -> STEP=1

START_NUM = DEFAULT_START_NUM
END_NUM = DEFAULT_END_NUM
STEP = DEFAULT_STEP

# Apsauga nuo per didelio intervalo (UI gali pasirinkti bet ką)
MAX_RANGE_ITEMS = int(os.getenv("MAX_RANGE_ITEMS", "500000"))

# Maksimalus batch dydis (kiek ID galima paduoti į /api/check_batch vienu kartu)
MAX_BATCH_IDS = int(os.getenv("MAX_BATCH_IDS", "1000"))

# Cache batch (be fetch į tikslą)
MAX_CACHE_BATCH_IDS = int(os.getenv("MAX_CACHE_BATCH_IDS", str(max(2000, MAX_BATCH_IDS))))

# Tikslinės svetainės lygiagretumas (kiek max vienu metu fetch'inti į aruodas.lt)
TARGET_CONCURRENCY = int(os.getenv("TARGET_CONCURRENCY", "10"))
if TARGET_CONCURRENCY < 1:
    TARGET_CONCURRENCY = 1
if TARGET_CONCURRENCY > 10:
    TARGET_CONCURRENCY = 10

# Keičiamas rate limit (per UI mygtukus)
MIN_INTERVAL_SECONDS = 2.0  # default

# Jitter: proporcingas + lubos
JITTER_FRAC = (0.02, 0.15)         # 2%..15% nuo MIN_INTERVAL_SECONDS
JITTER_CAP_SECONDS = (0.02, 0.15)  # absoliučios lubos sekundėmis

JITTER_SECONDS = (
    min(JITTER_CAP_SECONDS[0], MIN_INTERVAL_SECONDS * JITTER_FRAC[0]),
    min(JITTER_CAP_SECONDS[1], MIN_INTERVAL_SECONDS * JITTER_FRAC[1]),
)

ALLOWED_RATE_LIMITS = [0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# Persistencijos failas:
DEFAULT_STATE_FILE = Path(__file__).with_name("aruodas_state.json")
STATE_FILE_ENV = (os.getenv("STATE_FILE") or "").strip()
STATE_DIR_ENV = (os.getenv("STATE_DIR") or "").strip()

if STATE_FILE_ENV:
    STATE_FILE = Path(STATE_FILE_ENV)
elif STATE_DIR_ENV:
    STATE_FILE = Path(STATE_DIR_ENV) / "aruodas_state.json"
else:
    STATE_FILE = DEFAULT_STATE_FILE

# Persistencijos optimizacija: nerašyti į diską po kiekvieno ID.
STATE_SAVE_MIN_INTERVAL_SECONDS = float(os.getenv("STATE_SAVE_MIN_INTERVAL_SECONDS", "5"))
STATE_SAVE_EVERY_N = int(os.getenv("STATE_SAVE_EVERY_N", "50"))
_last_state_save_mono = 0.0
_dirty_since_save = 0

# =========================
# HTTP / concurrency
# =========================
_last_request_at = 0.0
_rate_lock = threading.Lock()

TARGET_SEM = threading.BoundedSemaphore(TARGET_CONCURRENCY)
EXECUTOR = ThreadPoolExecutor(max_workers=TARGET_CONCURRENCY)

_thread_local = threading.local()

_SESSION_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


def get_session() -> requests.Session:
    # requests.Session nėra idealu share'inti tarp thread'ų -> thread-local
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(_SESSION_HEADERS)
        _thread_local.session = s
    return s


# Cache atmintyje
CACHE: dict[str, dict] = {}  # id -> parsed result (be raw_html)

# RAW_CACHE: LRU
RAW_CACHE = OrderedDict()  # id -> raw_html (tik tiems, kuriuos tikrinai; NEPERSISTINAM)
RAW_CACHE_MAX_ITEMS = int(os.getenv("RAW_CACHE_MAX_ITEMS", "200"))
RAW_CACHE_MAX_BYTES = int(os.getenv("RAW_CACHE_MAX_BYTES", "500000"))

CACHE_LOCK = threading.Lock()

NOT_FOUND_MARKERS = [
    "Šiame puslapyje nėra informacijos, kurios jūs ieškote",
    "Siame puslapyje nera informacijos, kurios jus ieskote",
    "block-404",
]

CHALLENGE_MARKERS = [
    "Just a moment",
    "Enable JavaScript and cookies to continue",
    "cdn-cgi/challenge-platform",
    "_cf_chl_opt",
]

# =========================
# Pagalbinės funkcijos
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def range_count(start: int, end: int, step: int) -> int:
    if start > end:
        return 0
    return ((end - start) // step) + 1


def normalize_range(start: int, end: int, step: int) -> tuple[int, int, int]:
    """Normalizuoja intervalą:
    - STEP palaikomas tik 1 (visi).
    - riboja max įrašų skaičių.
    """
    if step != 1:
        raise ValueError("Šiuo metu palaikomas tik STEP=1 (visi ID).")

    if start > end:
        raise ValueError("start negali būti didesnis už end.")

    cnt = range_count(start, end, step)
    if cnt > MAX_RANGE_ITEMS:
        raise ValueError(f"Per didelis intervalas: {cnt} įrašų. Max: {MAX_RANGE_ITEMS}.")

    return start, end, step


def in_range(n: int) -> bool:
    return START_NUM <= n <= END_NUM


def id_num(id_str: str) -> int:
    return int(id_str.split("-", 1)[1])


def normalize_id(id_like: str) -> str:
    s = (id_like or "").strip()
    if not s:
        raise ValueError("Trūksta ID")
    # priimam: "1-2890001", "1-2890001/", "https://www.aruodas.lt/1-2890001/"
    s = s.replace("https://", "").replace("http://", "")
    s = s.replace("www.aruodas.lt/", "")
    s = s.strip("/")
    if not re.fullmatch(r"1-\d+", s):
        raise ValueError("Netinkamas ID formatas. Pvz: 1-3000000")
    return s


def parse_range_value(v) -> int:
    """Priimam start/end kaip:
    - int
    - '3000000'
    - '1-3000000'
    """
    if v is None:
        raise ValueError("Trūksta start arba end reikšmės.")

    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)

    s = str(v).strip()
    if not s:
        raise ValueError("Tuščia start/end reikšmė.")

    if re.fullmatch(r"1-\d+", s):
        return id_num(s)
    if re.fullmatch(r"\d+", s):
        return int(s)

    raise ValueError("Netinkamas start/end formatas. Naudok skaičių (pvz 3000000) arba ID (pvz 1-3000000).")


def _safe_float(x, default: float) -> float:
    try:
        return float(x)
    except Exception:
        return default


def recompute_jitter():
    """Jitter = procentas nuo MIN_INTERVAL, bet su absoliučiom lubom (sekundėmis)."""
    global JITTER_SECONDS

    mi = float(MIN_INTERVAL_SECONDS)

    jmin = min(float(JITTER_CAP_SECONDS[0]), mi * float(JITTER_FRAC[0]))
    jmax = min(float(JITTER_CAP_SECONDS[1]), mi * float(JITTER_FRAC[1]))

    if jmin < 0:
        jmin = 0.0
    if jmax < jmin:
        jmax = jmin

    JITTER_SECONDS = (jmin, jmax)


def is_allowed_rate(x: float) -> bool:
    try:
        xf = float(x)
    except Exception:
        return False
    return any(abs(xf - r) < 1e-9 for r in ALLOWED_RATE_LIMITS)


def snap_rate(x: float) -> float:
    xf = float(x)
    return min(ALLOWED_RATE_LIMITS, key=lambda r: abs(r - xf))


def rate_limit():
    """Globalus rate-limit (bendras visiems thread'ams)."""
    global _last_request_at
    with _rate_lock:
        now = time.monotonic()
        earliest = _last_request_at + float(MIN_INTERVAL_SECONDS)
        if now < earliest:
            time.sleep((earliest - now) + random.uniform(*JITTER_SECONDS))
        _last_request_at = time.monotonic()


def detect_status(html_text: str, http_status: int | None = None) -> str:
    low = (html_text or "").lower()

    if http_status == 404:
        return "NOT_FOUND"
    if any(m.lower() in low for m in NOT_FOUND_MARKERS):
        return "NOT_FOUND"
    if any(m.lower() in low for m in CHALLENGE_MARKERS):
        return "CHALLENGE"
    return "FOUND"


def extract_inserted_date(text: str) -> str | None:
    m = re.search(r"Įdėtas\s*(\d{4}-\d{2}-\d{2})", text, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"Idetas\s*(\d{4}-\d{2}-\d{2})", text, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def extract_title_text(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return " ".join(h1.stripped_strings)
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def parse_city_district_from_h1(title_text: str) -> tuple[str | None, str | None]:
    parts = [p.strip() for p in title_text.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def parse_city_district_from_url(final_url: str) -> tuple[str | None, str | None]:
    if not final_url:
        return None, None
    path = urlparse(final_url).path.strip("/")
    if not path:
        return None, None
    slug = path.split("/")[-1].strip("/")
    low = slug.lower()

    if "vilniuje" in low:
        tokens = low.split("-")
        try:
            i = tokens.index("vilniuje")
            district = tokens[i + 1] if i + 1 < len(tokens) else None
        except ValueError:
            district = None
        return "Vilnius", district

    return None, None


def make_snippet_html(source_text: str, word: str = "sugiharos", radius: int = 80) -> str | None:
    if not source_text:
        return None
    low = source_text.lower()
    idx = low.find(word.lower())
    if idx == -1:
        return None

    start = max(0, idx - radius)
    end = min(len(source_text), idx + len(word) + radius)
    snippet = source_text[start:end]

    esc = html.escape(snippet)
    esc = re.sub(r"(?i)(sugiharos)", r'<span class="hit">\1</span>', esc)

    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(source_text) else ""
    return prefix + esc + suffix


def parse_html(html_text: str, final_url: str = "", http_status: int | None = None) -> dict:
    status = detect_status(html_text, http_status=http_status)

    sug_snippet = make_snippet_html(html_text, "sugiharos", radius=120)
    sug_found = sug_snippet is not None

    result = {
        "status": status,  # FOUND / NOT_FOUND / CHALLENGE
        "inserted_date": None,
        "city": None,
        "district": None,
        "final_url": final_url or None,
        "sugiharos_found": sug_found,
        "sugiharos_snippet_html": sug_snippet,
    }

    if status != "FOUND":
        # jei challenge, bent miestą/rajoną pabandom iš URL
        city2, dist2 = parse_city_district_from_url(final_url)
        if city2:
            result["city"] = city2
        if dist2:
            result["district"] = dist2
        return result

    soup = BeautifulSoup(html_text, "html.parser")
    title_text = extract_title_text(soup)
    city, district = parse_city_district_from_h1(title_text)

    text = soup.get_text("\n", strip=True)
    inserted = extract_inserted_date(text)

    if not city:
        if re.search(r"\bvilni(?:us|uje|aus)\b", text, flags=re.IGNORECASE):
            city = "Vilnius"
        else:
            city2, _ = parse_city_district_from_url(final_url)
            city = city2 or city

    if city and city.strip().lower() == "vilnius":
        if not district:
            _, dist2 = parse_city_district_from_url(final_url)
            district = dist2 or district
    else:
        # taisyklė: jei miestas ne Vilnius – miesto/rajono nerodom
        district = None
        city = None

    result["inserted_date"] = inserted
    result["city"] = city
    result["district"] = district
    return result


def fetch_and_parse(id_str: str) -> tuple[dict, str]:
    """Fetch + parse vienam ID. Leidžia iki TARGET_CONCURRENCY paralelinių fetch'ų."""
    url = f"https://www.aruodas.lt/{id_str}/"

    with TARGET_SEM:
        rate_limit()
        session = get_session()
        r = session.get(url, timeout=25, allow_redirects=True)

    if not r.encoding:
        r.encoding = "utf-8"
    html_text = r.text

    parsed = parse_html(html_text, final_url=r.url, http_status=r.status_code)

    out = {
        "id": id_str,
        "checked_at": now_iso(),
        "http_status": r.status_code,
        **parsed,
    }
    return out, html_text


def _raw_cache_put_locked(id_str: str, raw_html: str):
    """LRU raw cache – kad RAM nesprogtų tikrinant tūkstančius ID."""
    if RAW_CACHE_MAX_ITEMS <= 0:
        return
    RAW_CACHE[id_str] = (raw_html or "")[:RAW_CACHE_MAX_BYTES]
    RAW_CACHE.move_to_end(id_str)
    while len(RAW_CACHE) > RAW_CACHE_MAX_ITEMS:
        RAW_CACHE.popitem(last=False)


# =========================
# Persistencija (istorija)
# =========================
def load_state_from_disk():
    """Užkrauna CACHE + config (rate limit) + range iš aruodas_state.json, jei yra."""
    global MIN_INTERVAL_SECONDS, START_NUM, END_NUM, STEP
    global _last_state_save_mono, _dirty_since_save

    if not STATE_FILE.exists():
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    cfg = (data or {}).get("config") or {}
    min_int = _safe_float(cfg.get("min_interval"), MIN_INTERVAL_SECONDS)
    if is_allowed_rate(min_int):
        MIN_INTERVAL_SECONDS = snap_rate(min_int)
        recompute_jitter()

    rng = (data or {}).get("range") or {}
    try:
        start = int(rng.get("start", START_NUM))
        end = int(rng.get("end", END_NUM))
        step = int(rng.get("step", STEP))
        start, end, step = normalize_range(start, end, step)
        START_NUM, END_NUM, STEP = start, end, step
    except Exception:
        pass

    cached = (data or {}).get("cache") or {}
    if isinstance(cached, dict):
        with CACHE_LOCK:
            for k, v in cached.items():
                if isinstance(k, str) and isinstance(v, dict) and "id" in v:
                    CACHE[k] = v

    _last_state_save_mono = time.monotonic()
    _dirty_since_save = 0


def save_state_to_disk_locked():
    """Išsaugo CACHE + config + range į aruodas_state.json (CALL ONLY UNDER CACHE_LOCK)."""
    tmp = STATE_FILE.with_suffix(".tmp")
    payload = {
        "version": 1,
        "saved_at": now_iso(),
        "config": {
            "min_interval": MIN_INTERVAL_SECONDS,
            "jitter": [float(JITTER_SECONDS[0]), float(JITTER_SECONDS[1])],
            "allowed_rates": ALLOWED_RATE_LIMITS,
        },
        "range": {
            "start": START_NUM,
            "end": END_NUM,
            "step": STEP,
        },
        "cache": CACHE,
    }
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def mark_state_dirty_locked(force: bool = False):
    """Throttle disk writes (CALL ONLY UNDER CACHE_LOCK)."""
    global _dirty_since_save, _last_state_save_mono

    _dirty_since_save += 1
    if force:
        save_state_to_disk_locked()
        _dirty_since_save = 0
        _last_state_save_mono = time.monotonic()
        return

    now = time.monotonic()
    if _dirty_since_save >= STATE_SAVE_EVERY_N or (now - _last_state_save_mono) >= STATE_SAVE_MIN_INTERVAL_SECONDS:
        save_state_to_disk_locked()
        _dirty_since_save = 0
        _last_state_save_mono = now


def _iter_cached_in_current_range_locked():
    for id_str, entry in CACHE.items():
        try:
            n = id_num(id_str)
            if in_range(n):
                yield id_str, entry
        except Exception:
            continue


def get_cached_ids_for_current_range_locked() -> list[str]:
    ids = []
    for id_str, _ in _iter_cached_in_current_range_locked():
        ids.append(id_str)
    return ids


def get_cached_stats_for_current_range_locked() -> dict:
    stats = {
        "checked": 0,
        "found": 0,
        "not_found": 0,
        "challenge": 0,
        "error": 0,
        "bad_total": 0,
    }
    for _, entry in _iter_cached_in_current_range_locked():
        stats["checked"] += 1
        st = (entry or {}).get("status")
        sug = (entry or {}).get("sugiharos_found") is True
        if st == "FOUND" or sug:
            stats["found"] += 1
        if st == "NOT_FOUND":
            stats["not_found"] += 1
        elif st == "CHALLENGE":
            stats["challenge"] += 1
        elif st == "ERROR":
            stats["error"] += 1

    stats["bad_total"] = stats["not_found"] + stats["challenge"] + stats["error"]
    return stats


def get_cached_items_for_current_range_locked(mode: str = "all") -> list[dict]:
    mode = (mode or "all").strip().lower()
    items = []

    for _, entry in _iter_cached_in_current_range_locked():
        if not isinstance(entry, dict):
            continue

        st = entry.get("status")
        sug = entry.get("sugiharos_found") is True

        if mode == "none":
            continue
        if mode == "found":
            if st == "FOUND" or sug:
                items.append(entry)
        elif mode == "bad":
            if st in ("ERROR", "CHALLENGE", "NOT_FOUND"):
                items.append(entry)
        else:
            items.append(entry)

    return items


# užkraunam state iš karto startuojant
load_state_from_disk()

# =========================
# Flask
# =========================
app = Flask(__name__)

INDEX_HTML = r"""<!doctype html>
<html lang="lt">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Aruodas ID tikrintuvas</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 16px; }
    .bar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom: 12px; }
    input[type="text"], input[type="number"], select { padding: 6px 8px; font-size: 14px; }
    input[type="text"] { width: 170px; }
    input[type="number"] { width: 140px; }
    button { padding: 7px 10px; font-size: 14px; cursor:pointer; }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    small { color:#666; }
    .grid { display:grid; grid-template-columns: 1fr; gap: 14px; }
    @media (min-width: 1000px) { .grid { grid-template-columns: 1fr 1fr; } }
    table { width:100%; border-collapse: collapse; table-layout: fixed; }
    th, td { border: 1px solid #ddd; padding: 6px 8px; font-size: 13px; vertical-align: top; }
    th { background: #f7f7f7; position: sticky; top:0; z-index: 1; }
    td.nowrap { white-space:nowrap; }
    tr.found { background: #eaffea; }
    tr.notfound { background: #ffecec; }
    tr.challenge { background: #fff4db; }
    tr.error { background: #f5d6ff; }
    tr.unknown { background: #fbfbfb; }
    a { color: inherit; }
    .status { font-weight: 700; }
    .hit { color: #0b63d1; font-weight: 700; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .note { color:#444; word-break: break-word; }
    .pill { display:inline-block; padding:2px 6px; border:1px solid #ccc; border-radius:999px; font-size:12px; background:#fff; }
    textarea { width: 100%; height: 180px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .card { border:1px solid #ddd; border-radius:8px; padding:10px; }
    .muted { color:#777; }
    .rate-btn { border: 1px solid #bbb; background: #fff; border-radius: 8px; }
    .rate-btn.active { border-color: #111; font-weight: 700; }
    #btnAutoToggle { border-radius: 10px; }
    #btnAutoToggle.running { font-weight: 800; }
    .warn { background:#fff4db; border:1px solid #f0d28a; padding:10px; border-radius:8px; }
  </style>
</head>
<body>
  <h2 style="margin:0 0 6px 0;">Aruodas ID tikrintuvas</h2>

  <div class="warn" style="margin: 8px 0 12px 0;">
    <b>⚠️ ĮSPĖJIMAS:</b> tik testavimui/eksperimentams. <b>Nenaudoti produkcinėje aplinkoje.</b>
    <div class="muted" style="margin-top:4px;">Pastaba: turi iki <b>__CONC__</b> lygiagrečių užklausų į tikslą (su global rate-limit).</div>
  </div>

  <div class="bar">
    <span class="pill" id="rangePill">ID intervalas: <b class="mono" id="pillStart">1-__START__</b> … <b class="mono" id="pillEnd">1-__END__</b> (visi, STEP=1)</span>
    <span class="pill" id="countPill">Kiekis: <b class="mono" id="countVal">—</b></span>
    <span class="pill">User-Agent: <span class="mono">__UA__</span></span>
    <span class="pill" id="ratePill">Rate limit: min <b class="mono" id="rateVal">__MIN__</b>s + jitter</span>
    <span class="pill" id="autoPill">Auto: OFF</span>
    <span class="pill" id="speedPill">Greitis: <b class="mono" id="speedVal">0</b>/min</span>
  </div>

  <div class="bar">
    <span class="muted">Nustatyk ID intervalą:</span>
    <input id="rangeStart" type="number" step="1" value="__START__" />
    <input id="rangeEnd" type="number" step="1" value="__END__" />
    <button id="btnGenerate">Generuoti sąrašą</button>
    <small class="muted">Ši versija tikrina VISUS ID (lyginius+nelyginius) – STEP=1.</small>
  </div>

  <div class="bar">
    <span class="muted">Rate limit:</span>
    <button class="rate-btn" data-rate="0.02">0.02s</button>
    <button class="rate-btn" data-rate="0.05">0.05s</button>
    <button class="rate-btn" data-rate="0.1">0.1s</button>
    <button class="rate-btn" data-rate="0.2">0.2s</button>
    <button class="rate-btn" data-rate="0.5">0.5s</button>
    <button class="rate-btn" data-rate="1">1s</button>
    <button class="rate-btn" data-rate="2">2s</button>
    <small class="muted">Keičia serverio limitą ir išsisaugo (persist).</small>
  </div>

  <div class="bar">
    <span class="muted">Auto batch (IDs per API call):</span>
    <select id="batchSize">
      <option value="1">1</option>
      <option value="5">5</option>
      <option value="10">10</option>
      <option value="20">20</option>
      <option value="50" selected>50</option>
      <option value="100">100</option>
      <option value="200">200</option>
      <option value="250">250</option>
      <option value="500">500</option>
      <option value="1000">1000</option>
    </select>
    <small class="muted">Serveris vykdo iki __CONC__ fetch'ų į tikslą.</small>
  </div>

  <div class="bar">
    <input id="idInput" type="text" value="1-__START__" />
    <button id="btnCheck">Tikrinti ID</button>
    <button id="btnRandom">Atsitiktinis ID</button>
    <button id="btnForce">Tikrinti (force)</button>
    <button id="btnAutoToggle">▶ Auto (OFF)</button>
    <small class="muted">Auto eina per netikrintus ID iš eilės. Sustos, jei gaus <b>ERROR</b>.</small>
  </div>

  <div class="grid">
    <div>
      <h3 style="margin: 8px 0;">✅ Rasti skelbimai (FOUND arba sugiharos hit)</h3>
      <table>
        <thead>
          <tr>
            <th style="width:120px;">ID</th>
            <th style="width:90px;">Įdėtas</th>
            <th style="width:120px;">Miestas</th>
            <th style="width:140px;">Rajonas</th>
            <th>Pastabos</th>
          </tr>
        </thead>
        <tbody id="foundBody"></tbody>
      </table>
      <div class="muted" style="margin-top:6px;">Rūšiuojama (naujausi viršuje).</div>
    </div>

    <div>
      <h3 style="margin: 8px 0;">📋 Visi ID (puslapiais)</h3>

      <div class="bar">
        <span class="muted">Puslapis:</span>
        <button id="btnPrevPage">◀</button>
        <span class="pill mono" id="pagePill">—</span>
        <button id="btnNextPage">▶</button>

        <span class="muted">Eilutės/pusl.:</span>
        <select id="pageSize">
          <option value="100">100</option>
          <option value="250">250</option>
          <option value="500" selected>500</option>
          <option value="1000">1000</option>
        </select>

        <span class="muted">Šokti į ID:</span>
        <input id="jumpTo" type="text" value="1-__START__" style="width:170px;" />
        <button id="btnJump">Eiti</button>

        <small class="muted">Dideliems intervalams čia rodoma tik dalis – nebekuriama 50k+ eilučių DOM’e.</small>
      </div>

      <div class="bar">
        <input id="filter" type="text" placeholder="Filtras (pvz. 3123)" />
        <button id="btnClearFilter">Valyti filtrą</button>
        <button id="btnShowOnlyBad">Rodyti tik raudonus/oranžinius</button>
        <button id="btnShowAll">Rodyti visus</button>
      </div>

      <small class="muted">Pastaba: filtras taikomas tik dabartiniam puslapiui.</small>

      <table>
        <thead>
          <tr>
            <th style="width:120px;">ID</th>
            <th style="width:90px;">Status</th>
            <th style="width:90px;">Įdėtas</th>
            <th style="width:120px;">Miestas</th>
            <th style="width:140px;">Rajonas</th>
            <th>Pastabos</th>
          </tr>
        </thead>
        <tbody id="allBody"></tbody>
      </table>
    </div>
  </div>

  <h3 style="margin: 14px 0 6px 0;">🧪 Debug (įklijuok HTML atsakymą ir pasitikrink parserį)</h3>
  <div class="card">
    <div class="bar">
      <input id="debugUrl" type="text" placeholder="(nebūtina) final_url pvz. https://www.aruodas.lt/..." style="width:420px;" />
      <button id="btnDebugParse">Parse DEBUG</button>
    </div>
    <textarea id="debugHtml" placeholder="Čia įklijuok visą HTML (kurį gavai su curl ar kitu metodu)"></textarea>
    <div id="debugOut" class="note" style="margin-top:8px;"></div>
  </div>

<script>
// Dinaminis range (keičiamas po page load)
let START = __START__;
let END   = __END__;
let STEP  = __STEP__;
let showOnlyBad = false;

// Auto režimo būsena
let autoRunning = false;
let autoStopRequested = false;
let autoNextNum = START;

// State krovimo apsauga
let stateLoading = false;
let statePromise = null;

// Patikrintų ID rinkinys (kad auto praleistų jau tikrintus)
const checkedIds = new Set();

// Lokalus rezultato cache (tik tai, ką UI jau parsisiuntė iš serverio)
const resultsMap = new Map(); // id -> data

// ===== Greičio skaičiavimas (checks per minute) =====
// Skaičiuojam tik realius fetch į aruodas.lt: data.from_cache === false
const speedWindow = []; // timestamps (ms)
function recordSpeed(data){
  if(data && data.from_cache === false){
    const now = Date.now();
    speedWindow.push(now);
    while(speedWindow.length && speedWindow[0] < now - 60000){
      speedWindow.shift();
    }
  }
}
function updateSpeedUi(){
  const now = Date.now();
  while(speedWindow.length && speedWindow[0] < now - 60000){
    speedWindow.shift();
  }
  const el = document.getElementById("speedVal");
  if(el) el.textContent = String(speedWindow.length);
}
setInterval(updateSpeedUi, 1000);

// Auto batch dydis (IDs per API call)
const AUTO_BATCH_OPTIONS = [1,5,10,20,50,100,200,250,500,1000];
let AUTO_BATCH_SIZE = parseInt(localStorage.getItem("autoBatchSize") || "50", 10);
if(!Number.isFinite(AUTO_BATCH_SIZE) || AUTO_BATCH_SIZE <= 0) AUTO_BATCH_SIZE = 50;
if(!AUTO_BATCH_OPTIONS.includes(AUTO_BATCH_SIZE)) AUTO_BATCH_SIZE = 50;

// „Visi ID“ puslapiavimas
const PAGE_SIZE_OPTIONS = [100,250,500,1000];
let PAGE_SIZE = parseInt(localStorage.getItem("pageSize") || "500", 10);
if(!Number.isFinite(PAGE_SIZE) || PAGE_SIZE <= 0) PAGE_SIZE = 500;
if(!PAGE_SIZE_OPTIONS.includes(PAGE_SIZE)) PAGE_SIZE = 500;

let currentPage = 0;
let pageLoadToken = 0;

function totalCount(){
  return Math.floor((END-START)/STEP)+1;
}
function totalPages(){
  return Math.max(1, Math.ceil(totalCount()/PAGE_SIZE));
}
function checkedCount(){
  return checkedIds.size;
}

function numFromId(id) { const m=/^1-(\d+)$/.exec(id); return m?parseInt(m[1],10):NaN; }
function makeId(n){ return "1-"+String(n); }

function randomInRange(){
  const count = totalCount();
  const k = Math.floor(Math.random()*count);
  return START + k*STEP;
}

function statusToClass(s){
  if(s==="FOUND") return "found";
  if(s==="NOT_FOUND") return "notfound";
  if(s==="CHALLENGE") return "challenge";
  if(s==="ERROR") return "error";
  return "unknown";
}
function statusLabel(s){
  if(s==="FOUND") return "FOUND";
  if(s==="NOT_FOUND") return "NĖRA";
  if(s==="CHALLENGE") return "CHALLENGE";
  if(s==="ERROR") return "ERROR";
  return "—";
}
function safeText(x){ return (x===null||x===undefined)?"":String(x); }

function updatePagePill(extraText=""){
  const pill = document.getElementById("pagePill");
  if(!pill) return;
  const tp = totalPages();
  const cur = Math.min(tp, Math.max(1, currentPage+1));
  pill.textContent = `${cur}/${tp} (ps=${PAGE_SIZE})${extraText ? " – " + extraText : ""}`;
}

function updateRangeUi(){
  const rs = document.getElementById("rangeStart");
  const re = document.getElementById("rangeEnd");
  if(rs) rs.value = String(START);
  if(re) re.value = String(END);

  const ps = document.getElementById("pillStart");
  const pe = document.getElementById("pillEnd");
  if(ps) ps.textContent = "1-" + String(START);
  if(pe) pe.textContent = "1-" + String(END);

  const cnt = totalCount();
  const cv = document.getElementById("countVal");
  if(cv) cv.textContent = String(cnt);

  const idInput = document.getElementById("idInput");
  if(idInput){
    const cur = idInput.value.trim();
    const n = numFromId(cur);
    if(!cur || !Number.isFinite(n) || n < START || n > END){
      idInput.value = makeId(START);
    }
  }

  const jump = document.getElementById("jumpTo");
  if(jump){
    const cur = jump.value.trim();
    const n = numFromId(cur);
    if(!cur || !Number.isFinite(n) || n < START || n > END){
      jump.value = makeId(START);
    }
  }

  autoNextNum = START;
  currentPage = 0;
  updatePagePill();
}

function buildAllRow(id){
  const tr=document.createElement("tr");
  tr.dataset.id=id;
  tr.dataset.num=String(numFromId(id));
  tr.className="unknown";
  tr.innerHTML=`
    <td class="nowrap mono">
      <a href="https://www.aruodas.lt/${id}/" target="_blank" rel="noopener">${id}</a>
      <div><button data-action="check" data-id="${id}">Tikrinti</button></div>
    </td>
    <td class="status">—</td>
    <td class="mono"></td>
    <td></td><td></td>
    <td class="note"></td>`;
  return tr;
}

function buildFoundRow(id,data){
  const tr=document.createElement("tr");
  tr.dataset.id=id;
  tr.dataset.num=String(numFromId(id));
  tr.dataset.date=data.inserted_date||"";
  tr.className="found";

  const pill = (data.status && data.status !== "FOUND")
    ? `<span class="pill mono">${statusLabel(data.status)}</span> `
    : "";

  tr.innerHTML=`
    <td class="nowrap mono">
      <a href="https://www.aruodas.lt/${id}/" target="_blank" rel="noopener">${id}</a>
      <div><a class="mono" href="/raw?id=${encodeURIComponent(id)}" target="_blank" rel="noopener">raw</a></div>
    </td>
    <td class="mono">${safeText(data.inserted_date||"")}</td>
    <td>${safeText(data.city||"")}</td>
    <td>${safeText(data.district||"")}</td>
    <td class="note">${pill}${data.sugiharos_snippet_html||""}</td>`;
  return tr;
}

function updateAllRow(tr,data){
  tr.className=statusToClass(data.status);
  const tds=tr.querySelectorAll("td");
  tds[1].textContent=statusLabel(data.status);
  tds[2].textContent=data.inserted_date||"";
  tds[3].textContent=data.city||"";
  tds[4].textContent=data.district||"";

  if(data.status==="ERROR" && data.error){
    tds[5].textContent = data.error;
  } else {
    tds[5].innerHTML = data.sugiharos_snippet_html||"";
  }

  const idCell=tds[0];
  if(!idCell.querySelector('a[href^="/raw"]') && (data.status && data.status!=="—")){
    const div=document.createElement("div");
    div.innerHTML=`<a class="mono" href="/raw?id=${encodeURIComponent(data.id||tr.dataset.id)}" target="_blank" rel="noopener">raw</a>`;
    idCell.appendChild(div);
  }
}

function sortFoundTable(){
  const body=document.getElementById("foundBody");
  const rows=Array.from(body.querySelectorAll("tr"));
  rows.sort((a,b)=>{
    const da=a.dataset.date||"", db=b.dataset.date||"";
    if(da!==db) return db.localeCompare(da);
    return (parseInt(b.dataset.num,10)-parseInt(a.dataset.num,10));
  });
  for(const r of rows) body.appendChild(r);
}

function applyFilter(){
  const q=document.getElementById("filter").value.trim();
  const rows=document.querySelectorAll("#allBody tr");
  for(const tr of rows){
    const id=tr.dataset.id, cls=tr.className;
    const isBad=(cls==="notfound"||cls==="challenge"||cls==="error");
    const passQ=!q||id.includes(q)||tr.dataset.num.includes(q);
    const passBad=!showOnlyBad||isBad;
    tr.style.display=(passQ&&passBad)?"":"none";
  }
}

function updateAutoPill(extraText=""){
  const pill = document.getElementById("autoPill");
  if(!pill) return;
  const base = autoRunning ? "Auto: ON" : "Auto: OFF";
  const prog = `${checkedCount()}/${totalCount()}`;
  const bs = `batch=${AUTO_BATCH_SIZE}`;
  pill.textContent = `${base} (${prog}, ${bs})${extraText ? " – " + extraText : ""}`;
}

function chunkArray(arr, size){
  const out=[];
  for(let i=0;i<arr.length;i+=size){
    out.push(arr.slice(i,i+size));
  }
  return out;
}

function pageIds(pageIndex){
  const total = totalCount();
  const startIndex = pageIndex * PAGE_SIZE;
  const out = [];

  for(let i=0;i<PAGE_SIZE;i++){
    const idx = startIndex + i;
    if(idx >= total) break;
    const n = START + idx*STEP;
    out.push(makeId(n));
  }
  return out;
}

function renderAllPage(){
  const body=document.getElementById("allBody");
  body.innerHTML = "";
  const ids = pageIds(currentPage);
  const frag=document.createDocumentFragment();

  for(const id of ids){
    const tr = buildAllRow(id);
    const cached = resultsMap.get(id);
    if(cached) updateAllRow(tr, cached);
    frag.appendChild(tr);
  }
  body.appendChild(frag);

  updatePagePill();
  applyFilter();
}

async function fetchCacheBatch(ids){
  let resp, data;
  try{
    resp = await fetch("/api/cache_batch", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ ids })
    });
    data = await resp.json();
  } catch(err){
    return [];
  }

  if(!resp || !resp.ok || !data || data.error){
    return [];
  }
  return data.items || [];
}

async function loadCacheForVisiblePage(){
  const token = ++pageLoadToken;
  const ids = pageIds(currentPage);

  // tik tie, kurie jau patikrinti (kad nerizikuotume fetch'int į tikslą)
  const need = ids.filter(id => checkedIds.has(id) && !resultsMap.has(id));
  if(!need.length){
    updatePagePill();
    return;
  }

  updatePagePill("kraunama…");

  const chunks = chunkArray(need, 500);

  for(const chunk of chunks){
    if(token !== pageLoadToken) return;
    const items = await fetchCacheBatch(chunk);
    for(const item of items){
      applyResultToUi(item, false);
    }
  }

  sortFoundTable();
  applyFilter();
  updateAutoPill();
  updatePagePill();
}

function setPage(p){
  const tp = totalPages();
  currentPage = Math.max(0, Math.min(tp-1, p));
  renderAllPage();
  loadCacheForVisiblePage();
}

function jumpToId(idLike){
  const s = (idLike||"").trim();
  if(!s) return;

  let n = NaN;
  const m = /^1-(\d+)$/.exec(s);
  if(m) n = parseInt(m[1],10);
  else if(/^\d+$/.test(s)) n = parseInt(s,10);

  if(!Number.isFinite(n)){
    alert("Netinkamas ID / skaičius. Pvz: 1-3000000");
    return;
  }
  if(n < START || n > END){
    alert("ID ne intervale.");
    return;
  }

  const idx = Math.floor((n - START) / STEP);
  const page = Math.floor(idx / PAGE_SIZE);
  setPage(page);

  const jump = document.getElementById("jumpTo");
  if(jump) jump.value = makeId(n);

  setTimeout(()=>{
    const id = makeId(n);
    const row = document.querySelector(`#allBody tr[data-id="${CSS.escape(id)}"]`);
    if(row){
      row.style.outline = "2px solid #0b63d1";
      setTimeout(()=>{ row.style.outline = ""; }, 1200);
    }
  }, 50);
}

function applyResultToUi(data, doSort=true){
  recordSpeed(data);
  updateSpeedUi();

  const idNorm=data.id;
  checkedIds.add(idNorm);
  resultsMap.set(idNorm, data);

  const putToFound = (data.status === "FOUND") || (data.sugiharos_found === true);

  if(putToFound){
    const existing=document.querySelector(`#foundBody tr[data-id="${CSS.escape(idNorm)}"]`);
    if(existing) existing.remove();
    document.getElementById("foundBody").appendChild(buildFoundRow(idNorm,data));
    if(doSort) sortFoundTable();
  }

  const allRow=document.querySelector(`#allBody tr[data-id="${CSS.escape(idNorm)}"]`);
  if(allRow){
    updateAllRow(allRow, data);
  }
}

async function checkId(id, force=false, silent=false){
  let resp, data;
  try{
    resp = await fetch(`/api/check?id=${encodeURIComponent(id)}&force=${force?"1":"0"}`);
    data = await resp.json();
  } catch(err){
    data = {
      id: id,
      checked_at: new Date().toISOString(),
      status: "ERROR",
      error: String(err),
      http_status: null,
      inserted_date: null,
      city: null,
      district: null,
      final_url: null,
      sugiharos_found: false,
      sugiharos_snippet_html: null,
      from_cache: false
    };
  }

  if(!resp || !resp.ok || data.error){
    data = data || {};
    if(!data.id) data.id = id;
    if(!data.status) data.status = "ERROR";
    if(!data.error) data.error = data.error || "Klaida tikrinant ID";
    if(data.from_cache === undefined) data.from_cache = false;
    applyResultToUi(data, true);
    applyFilter();
    updateAutoPill();
    if(!silent){
      alert(data.error || "Klaida tikrinant ID");
    }
    return data;
  }

  applyResultToUi(data, true);
  applyFilter();
  updateAutoPill();
  return data;
}

async function checkBatch(ids, force=false, silent=false, stopOnError=false){
  let resp, data;
  try{
    resp = await fetch("/api/check_batch", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        ids: ids,
        force: force ? 1 : 0,
        stop_on_error: stopOnError ? 1 : 0
      })
    });
    data = await resp.json();
  } catch(err){
    const msg = String(err);
    if(!silent) alert(msg);
    return [{
      id: (ids && ids[0]) ? ids[0] : "1-0",
      checked_at: new Date().toISOString(),
      status: "ERROR",
      error: msg,
      http_status: null,
      inserted_date: null,
      city: null,
      district: null,
      final_url: null,
      sugiharos_found: false,
      sugiharos_snippet_html: null,
      from_cache: false
    }];
  }

  if(!resp || !resp.ok || !data || data.error){
    const msg = (data && data.error) ? data.error : "Klaida tikrinant batch";
    if(!silent) alert(msg);
    return [{
      id: (ids && ids[0]) ? ids[0] : "1-0",
      checked_at: new Date().toISOString(),
      status: "ERROR",
      error: msg,
      http_status: null,
      inserted_date: null,
      city: null,
      district: null,
      final_url: null,
      sugiharos_found: false,
      sugiharos_snippet_html: null,
      from_cache: false
    }];
  }

  return data.items || [];
}

function updateRateUi(minInterval){
  const el=document.getElementById("rateVal");
  if(el) el.textContent = String(minInterval);

  const btns = document.querySelectorAll("button.rate-btn");
  btns.forEach(b => {
    const r = parseFloat(b.dataset.rate);
    b.classList.toggle("active", Math.abs(r - parseFloat(minInterval)) < 1e-9);
  });
}

async function setRate(minInterval){
  const resp = await fetch("/api/config", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({min_interval: minInterval})
  });
  const data = await resp.json();
  if(!resp.ok || data.error){
    alert(data.error || "Nepavyko pakeisti rate limit");
    return;
  }
  updateRateUi(data.min_interval);
}

async function setRangeOnServer(startVal, endVal){
  const resp = await fetch("/api/range", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({start: startVal, end: endVal})
  });
  const data = await resp.json();
  if(!resp.ok || data.error){
    alert(data.error || "Nepavyko nustatyti intervalo");
    return false;
  }
  return true;
}

function setControlsDisabled(disabled, disableAuto=false){
  const autoBtn = document.getElementById("btnAutoToggle");
  if(autoBtn){
    autoBtn.disabled = disableAuto ? disabled : false;
  }

  const ids = ["btnCheck","btnRandom","btnForce","btnGenerate"];
  ids.forEach(id=>{
    const el = document.getElementById(id);
    if(el) el.disabled = disabled;
  });

  document.querySelectorAll("button.rate-btn").forEach(b => b.disabled = disabled);
  document.querySelectorAll("button[data-action='check']").forEach(b => b.disabled = disabled);

  [
    "filter","btnClearFilter","btnShowOnlyBad","btnShowAll",
    "idInput","rangeStart","rangeEnd","batchSize",
    "btnPrevPage","btnNextPage","pageSize","jumpTo","btnJump"
  ].forEach(id=>{
    const el = document.getElementById(id);
    if(el) el.disabled = disabled;
  });
}

function setAutoButtonUi(){
  const btn = document.getElementById("btnAutoToggle");
  if(!btn) return;
  btn.classList.toggle("running", autoRunning);
  btn.textContent = autoRunning ? "⏸ Auto (STOP)" : "▶ Auto (OFF)";
}

function findNextUncheckedBatch(fromNum, limit){
  const total = totalCount();
  let n = fromNum;
  const out = [];

  for(let i=0;i<total && out.length<limit;i++){
    if(n > END) n = START; // wrap – jei kažką praleidai, vis tiek viską patikrins
    const id = makeId(n);
    if(!checkedIds.has(id)){
      out.push({id, n});
    }
    n += STEP;
  }

  return out.length ? out : null;
}

async function runAuto(){
  if(autoRunning) return;

  if(stateLoading && statePromise){
    await statePromise;
  }

  autoRunning = true;
  autoStopRequested = false;

  setControlsDisabled(true, false);
  setAutoButtonUi();
  updateAutoPill("start");

  let n = autoNextNum;

  while(autoRunning && !autoStopRequested){
    const batch = findNextUncheckedBatch(n, AUTO_BATCH_SIZE);
    if(!batch){
      updateAutoPill("baigta (viskas patikrinta)");
      break;
    }

    const ids = batch.map(x => x.id);
    updateAutoPill(`tikrinama batch: ${ids[0]} … (${ids.length})`);

    const items = await checkBatch(ids, false, true, true);

    for(const item of items){
      applyResultToUi(item, false);
    }
    sortFoundTable();
    applyFilter();
    updateAutoPill();

    const bad = items.find(it => (it && (it.status === "ERROR" || it.error)));
    if(bad){
      const bid = bad.id || (ids[0] || "");
      updateAutoPill(`SUSTABDYTA: ERROR ties ${bid}`);
      break;
    }

    const processed = items.length;
    if(processed <= 0){
      updateAutoPill("SUSTABDYTA: tuščias batch atsakymas");
      break;
    }
    const last = batch[Math.min(processed, batch.length) - 1];
    n = last.n + STEP;
    autoNextNum = n;

    await new Promise(r => setTimeout(r, 30));
  }

  autoRunning = false;
  autoStopRequested = false;

  setControlsDisabled(false, true);
  setAutoButtonUi();
  updateAutoPill();
}

function stopAuto(){
  if(!autoRunning) return;
  autoStopRequested = true;
  updateAutoPill("stabdoma…");
}

async function reloadEverything(){
  if(stateLoading && statePromise) return statePromise;

  stateLoading = true;
  setControlsDisabled(true, !autoRunning);
  setAutoButtonUi();
  updateAutoPill("kraunama…");
  updatePagePill("kraunama…");

  statePromise = (async()=>{
    try{
      checkedIds.clear();
      resultsMap.clear();
      speedWindow.length = 0;
      updateSpeedUi();

      document.getElementById("foundBody").innerHTML = "";
      document.getElementById("allBody").innerHTML = "";

      const resp = await fetch("/api/state?items=found&include_ids=1");
      const state = await resp.json();

      const cfg = state.config || {};
      if(cfg.min_interval !== undefined && cfg.min_interval !== null){
        updateRateUi(cfg.min_interval);
      }

      const rng = state.range || {};
      if(rng.start !== undefined && rng.end !== undefined && rng.step !== undefined){
        START = parseInt(rng.start,10);
        END   = parseInt(rng.end,10);
        STEP  = parseInt(rng.step,10);
      }

      const ids = state.checked_ids || [];
      for(const id of ids){
        checkedIds.add(id);
      }

      updateRangeUi();

      const items = state.items || [];
      for(const item of items){
        applyResultToUi(item, false);
      }
      sortFoundTable();

      renderAllPage();
      await loadCacheForVisiblePage();

      applyFilter();
      updateAutoPill();
    } finally {
      stateLoading = false;

      if(autoRunning){
        setControlsDisabled(true, false);
      } else {
        setControlsDisabled(false, true);
      }

      setAutoButtonUi();
      updateAutoPill();
      updatePagePill();
    }
  })();

  return statePromise;
}

document.addEventListener("click",async(e)=>{
  const btn=e.target.closest("button[data-action='check']");
  if(btn) await checkId(btn.dataset.id,false,false);

  const rbtn = e.target.closest("button.rate-btn");
  if(rbtn){
    const val = parseFloat(rbtn.dataset.rate);
    await setRate(val);
  }
});

document.getElementById("btnCheck").addEventListener("click",async()=>{
  await checkId(document.getElementById("idInput").value.trim(),false,false);
});
document.getElementById("btnForce").addEventListener("click",async()=>{
  await checkId(document.getElementById("idInput").value.trim(),true,false);
});
document.getElementById("btnRandom").addEventListener("click",async()=>{
  const id=makeId(randomInRange());
  document.getElementById("idInput").value=id;
  await checkId(id,false,false);
});

document.getElementById("btnAutoToggle").addEventListener("click", async()=>{
  if(autoRunning){
    stopAuto();
    return;
  }
  await runAuto();
});

const batchSel = document.getElementById("batchSize");
if(batchSel){
  batchSel.value = String(AUTO_BATCH_SIZE);
  batchSel.addEventListener("change", ()=>{
    const v = parseInt(batchSel.value, 10);
    if(Number.isFinite(v) && v > 0){
      AUTO_BATCH_SIZE = v;
      localStorage.setItem("autoBatchSize", String(v));
      updateAutoPill();
    }
  });
}

const pageSel = document.getElementById("pageSize");
if(pageSel){
  pageSel.value = String(PAGE_SIZE);
  pageSel.addEventListener("change", ()=>{
    const v = parseInt(pageSel.value, 10);
    if(Number.isFinite(v) && v > 0){
      const firstIdx = currentPage * PAGE_SIZE;
      PAGE_SIZE = v;
      localStorage.setItem("pageSize", String(v));
      const newPage = Math.floor(firstIdx / PAGE_SIZE);
      setPage(newPage);
    }
  });
}

document.getElementById("btnPrevPage").addEventListener("click", ()=>{
  setPage(currentPage - 1);
});
document.getElementById("btnNextPage").addEventListener("click", ()=>{
  setPage(currentPage + 1);
});
document.getElementById("btnJump").addEventListener("click", ()=>{
  jumpToId(document.getElementById("jumpTo").value.trim());
});
document.getElementById("jumpTo").addEventListener("keydown", (e)=>{
  if(e.key === "Enter"){
    jumpToId(document.getElementById("jumpTo").value.trim());
  }
});

document.getElementById("btnGenerate").addEventListener("click", async()=>{
  if(autoRunning){
    alert("Sustabdyk Auto prieš keičiant intervalą.");
    return;
  }

  setControlsDisabled(true, true);

  const s = document.getElementById("rangeStart").value;
  const e = document.getElementById("rangeEnd").value;

  document.getElementById("filter").value = "";
  showOnlyBad = false;

  const ok = await setRangeOnServer(s, e);
  if(!ok){
    setControlsDisabled(false, true);
    return;
  }

  await reloadEverything();
});

document.getElementById("filter").addEventListener("input",applyFilter);
document.getElementById("btnClearFilter").addEventListener("click",()=>{
  document.getElementById("filter").value="";
  showOnlyBad=false;
  applyFilter();
});
document.getElementById("btnShowOnlyBad").addEventListener("click",()=>{
  showOnlyBad=true;
  applyFilter();
});
document.getElementById("btnShowAll").addEventListener("click",()=>{
  showOnlyBad=false;
  applyFilter();
});

document.getElementById("btnDebugParse").addEventListener("click", async()=>{
  const html=document.getElementById("debugHtml").value;
  const final_url=document.getElementById("debugUrl").value.trim();
  const out=document.getElementById("debugOut");
  out.textContent="Skaičiuoju…";
  const resp=await fetch("/api/debug/parse",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({html,final_url})});
  const data=await resp.json();
  out.innerHTML=`
    <div><b>Status:</b> <span class="mono">${safeText(data.status)}</span></div>
    <div><b>Įdėtas:</b> <span class="mono">${safeText(data.inserted_date||"")}</span></div>
    <div><b>Miestas:</b> ${safeText(data.city||"")}</div>
    <div><b>Rajonas:</b> ${safeText(data.district||"")}</div>
    <div><b>Sugiharos:</b> ${data.sugiharos_found?"TAIP":"NE"}</div>
    <div style="margin-top:6px;"><b>Snippet:</b><div class="note">${data.sugiharos_snippet_html||""}</div></div>`;
});

// init
applyFilter();
updateAutoPill();
setAutoButtonUi();
updatePagePill();
updateSpeedUi();

statePromise = reloadEverything();
</script>
</body>
</html>
"""

@app.get("/")
def index():
    html_page = (
        INDEX_HTML
        .replace("__START__", str(START_NUM))
        .replace("__END__", str(END_NUM))
        .replace("__STEP__", str(STEP))
        .replace("__UA__", html.escape(USER_AGENT))
        .replace("__MIN__", str(MIN_INTERVAL_SECONDS))
        .replace("__CONC__", str(TARGET_CONCURRENCY))
    )
    return Response(html_page, mimetype="text/html; charset=utf-8")


@app.get("/api/state")
def api_state():
    items_mode = (request.args.get("items") or "all").strip().lower()
    if items_mode not in ("all", "found", "bad", "none"):
        items_mode = "all"

    include_ids = (request.args.get("include_ids", "1") != "0")

    try:
        offset = int(request.args.get("offset", "0"))
    except Exception:
        offset = 0
    try:
        limit = int(request.args.get("limit", "0"))
    except Exception:
        limit = 0
    if offset < 0:
        offset = 0
    if limit < 0:
        limit = 0

    with CACHE_LOCK:
        stats = get_cached_stats_for_current_range_locked()

        items = get_cached_items_for_current_range_locked(items_mode)
        if offset:
            items = items[offset:]
        if limit:
            items = items[:limit]

        cfg = {
            "min_interval": MIN_INTERVAL_SECONDS,
            "allowed_rates": ALLOWED_RATE_LIMITS,
            "state_file": str(STATE_FILE),
            "max_range_items": MAX_RANGE_ITEMS,
            "max_batch_ids": MAX_BATCH_IDS,
            "max_cache_batch_ids": MAX_CACHE_BATCH_IDS,
            "target_concurrency": TARGET_CONCURRENCY,
            "jitter_seconds": [float(JITTER_SECONDS[0]), float(JITTER_SECONDS[1])],
            "raw_cache_max_items": RAW_CACHE_MAX_ITEMS,
            "raw_cache_max_bytes": RAW_CACHE_MAX_BYTES,
            "state_save_min_interval_seconds": STATE_SAVE_MIN_INTERVAL_SECONDS,
            "state_save_every_n": STATE_SAVE_EVERY_N,
        }
        rng = {
            "start": START_NUM,
            "end": END_NUM,
            "step": STEP,
            "count": range_count(START_NUM, END_NUM, STEP),
        }

        payload = {"config": cfg, "range": rng, "stats": stats, "items": items}
        if include_ids:
            payload["checked_ids"] = get_cached_ids_for_current_range_locked()

    return jsonify(payload)


@app.post("/api/cache_batch")
def api_cache_batch():
    """Gražina tik CACHE įrašus (be fetch į tikslą)."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids", None)

    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids turi būti sąrašas (pvz. {ids:[\"1-3000000\", ...]})."}), 400
    if len(ids) > MAX_CACHE_BATCH_IDS:
        return jsonify({"error": f"Per didelis cache batch: {len(ids)}. Max: {MAX_CACHE_BATCH_IDS}."}), 400

    norm_ids: list[str] = []
    try:
        for x in ids:
            id_str = normalize_id(str(x))
            norm_ids.append(id_str)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    with CACHE_LOCK:
        out = []
        for id_str in norm_ids:
            entry = CACHE.get(id_str)
            if isinstance(entry, dict):
                d = dict(entry)
                d["from_cache"] = True
                out.append(d)

    return jsonify({"items": out, "count": len(out)})


@app.get("/api/config")
def api_config_get():
    return jsonify({
        "min_interval": MIN_INTERVAL_SECONDS,
        "allowed_rates": ALLOWED_RATE_LIMITS,
        "jitter_seconds": [float(JITTER_SECONDS[0]), float(JITTER_SECONDS[1])],
        "target_concurrency": TARGET_CONCURRENCY,
    })


@app.post("/api/config")
def api_config_set():
    global MIN_INTERVAL_SECONDS
    payload = request.get_json(silent=True) or {}
    val = payload.get("min_interval", None)
    try:
        f = float(val)
    except Exception:
        return jsonify({"error": "min_interval turi būti skaičius (0.02 / 0.05 / 0.1 / 0.2 / 0.5 / 1 / 2)."}), 400

    if not is_allowed_rate(f):
        return jsonify({"error": f"Leidžiamos reikšmės: {ALLOWED_RATE_LIMITS}"}), 400

    MIN_INTERVAL_SECONDS = snap_rate(f)
    recompute_jitter()

    with CACHE_LOCK:
        mark_state_dirty_locked(force=True)

    return jsonify({
        "min_interval": MIN_INTERVAL_SECONDS,
        "jitter_seconds": [float(JITTER_SECONDS[0]), float(JITTER_SECONDS[1])],
        "target_concurrency": TARGET_CONCURRENCY,
    })


@app.get("/api/range")
def api_range_get():
    return jsonify({
        "range": {
            "start": START_NUM,
            "end": END_NUM,
            "step": STEP,
            "count": range_count(START_NUM, END_NUM, STEP),
        }
    })


@app.post("/api/range")
def api_range_set():
    global START_NUM, END_NUM, STEP

    payload = request.get_json(silent=True) or {}

    try:
        start = parse_range_value(payload.get("start", None))
        end = parse_range_value(payload.get("end", None))
        step_raw = payload.get("step", STEP)
        step = int(step_raw)
        start, end, step = normalize_range(start, end, step)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    START_NUM, END_NUM, STEP = start, end, step

    with CACHE_LOCK:
        mark_state_dirty_locked(force=True)

    return jsonify({
        "range": {
            "start": START_NUM,
            "end": END_NUM,
            "step": STEP,
            "count": range_count(START_NUM, END_NUM, STEP),
        }
    })


@app.get("/api/check")
def api_check():
    id_like = request.args.get("id", "")
    force = request.args.get("force", "0") == "1"

    try:
        id_str = normalize_id(id_like)
        n = id_num(id_str)
        if not in_range(n):
            return jsonify({"error": "ID ne intervale", "id": id_str}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    with CACHE_LOCK:
        if not force and id_str in CACHE:
            d = dict(CACHE[id_str])
            d["from_cache"] = True
            return jsonify(d)

    try:
        out, raw_html = fetch_and_parse(id_str)
        with CACHE_LOCK:
            CACHE[id_str] = out
            _raw_cache_put_locked(id_str, raw_html)
            mark_state_dirty_locked(force=False)

        d = dict(out)
        d["from_cache"] = False
        return jsonify(d)
    except Exception as e:
        err = {
            "id": id_str,
            "checked_at": now_iso(),
            "status": "ERROR",
            "error": str(e),
            "http_status": None,
            "inserted_date": None,
            "city": None,
            "district": None,
            "final_url": None,
            "sugiharos_found": False,
            "sugiharos_snippet_html": None,
        }
        with CACHE_LOCK:
            CACHE[id_str] = err
            mark_state_dirty_locked(force=False)

        d = dict(err)
        d["from_cache"] = False
        return jsonify(d), 200


@app.post("/api/check_batch")
def api_check_batch():
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids", None)
    force = str(payload.get("force", "0")).lower() in ("1", "true", "yes", "y")
    stop_on_error = str(payload.get("stop_on_error", "0")).lower() in ("1", "true", "yes", "y")

    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids turi būti sąrašas (pvz. {ids:[\"1-3000000\", ...]})."}), 400

    if len(ids) > MAX_BATCH_IDS:
        return jsonify({"error": f"Per didelis batch: {len(ids)}. Max: {MAX_BATCH_IDS}."}), 400

    norm_ids: list[str] = []
    try:
        for x in ids:
            id_str = normalize_id(str(x))
            n = id_num(id_str)
            if not in_range(n):
                raise ValueError(f"ID ne intervale: {id_str}")
            norm_ids.append(id_str)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    results: list[dict] = []
    dirty = False
    stopped_early = False

    # pipeline su iki TARGET_CONCURRENCY lygiagrečių fetch'ų
    futures: dict[int, object] = {}
    next_to_submit = 0

    def submit_until_full():
        nonlocal next_to_submit
        while next_to_submit < len(norm_ids) and len(futures) < TARGET_CONCURRENCY:
            id_str2 = norm_ids[next_to_submit]

            if not force:
                with CACHE_LOCK:
                    cached2 = CACHE.get(id_str2)
                if cached2 is not None:
                    next_to_submit += 1
                    continue

            futures[next_to_submit] = EXECUTOR.submit(fetch_and_parse, id_str2)
            next_to_submit += 1

    submit_until_full()

    for i, id_str in enumerate(norm_ids):
        if not force:
            with CACHE_LOCK:
                cached = CACHE.get(id_str)
            if cached is not None:
                d = dict(cached)
                d["from_cache"] = True
                results.append(d)
                submit_until_full()
                continue

        fut = futures.pop(i, None)
        if fut is None:
            fut = EXECUTOR.submit(fetch_and_parse, id_str)

        try:
            out, raw_html = fut.result()
            with CACHE_LOCK:
                CACHE[id_str] = out
                _raw_cache_put_locked(id_str, raw_html)
            dirty = True

            d = dict(out)
            d["from_cache"] = False
            results.append(d)

        except Exception as e:
            err = {
                "id": id_str,
                "checked_at": now_iso(),
                "status": "ERROR",
                "error": str(e),
                "http_status": None,
                "inserted_date": None,
                "city": None,
                "district": None,
                "final_url": None,
                "sugiharos_found": False,
                "sugiharos_snippet_html": None,
            }
            with CACHE_LOCK:
                CACHE[id_str] = err
            dirty = True

            d = dict(err)
            d["from_cache"] = False
            results.append(d)

            if stop_on_error:
                stopped_early = True
                for f in futures.values():
                    try:
                        f.cancel()
                    except Exception:
                        pass
                futures.clear()
                break

        submit_until_full()

    if dirty:
        with CACHE_LOCK:
            mark_state_dirty_locked(force=False)

    return jsonify({
        "items": results,
        "count": len(results),
        "stopped_early": bool(stopped_early),
    })


@app.get("/raw")
def raw():
    id_like = request.args.get("id", "")
    try:
        id_str = normalize_id(id_like)
    except Exception as e:
        return Response(str(e), mimetype="text/plain; charset=utf-8"), 400

    with CACHE_LOCK:
        raw_html = RAW_CACHE.get(id_str)

    if raw_html is None:
        return Response(
            "Nėra raw HTML (pirma paspausk 'Tikrinti'. Po serverio restarto raw neišsaugomas.)",
            mimetype="text/plain; charset=utf-8"
        ), 404

    return Response(raw_html, mimetype="text/plain; charset=utf-8")


@app.post("/api/debug/parse")
def api_debug_parse():
    payload = request.get_json(silent=True) or {}
    html_text = payload.get("html", "") or ""
    final_url = payload.get("final_url", "") or ""
    parsed = parse_html(html_text, final_url=final_url, http_status=None)
    return jsonify(parsed)


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    app.run(host=host, port=port, debug=False)


