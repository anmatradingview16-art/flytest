# -*- coding: utf-8 -*-
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

from flask import Flask, request, jsonify, Response
import requests
from bs4 import BeautifulSoup

# =========================
# Konfigūracija (DEFAULT)
# =========================
# Nuo šiol intervalas NĖRA “hardcode”:
# - default yra čia
# - bet realiai jis gali būti pakeistas per UI (POST /api/range)
# - ir išsisaugo į aruodas_state.json (persist)
DEFAULT_START_NUM = 3000001
DEFAULT_END_NUM = 3000033
DEFAULT_STEP = 2  # tik nelyginiai -> STEP=2

START_NUM = DEFAULT_START_NUM
END_NUM = DEFAULT_END_NUM
STEP = DEFAULT_STEP

# Apsauga nuo per didelio intervalo (UI gali pasirinkti bet ką)
MAX_RANGE_ITEMS = int(os.getenv("MAX_RANGE_ITEMS", "120000"))

# Keičiamas rate limit (per UI mygtukus)
MIN_INTERVAL_SECONDS = 2.0   # default

# Jitter padarom proporcingą MIN_INTERVAL_SECONDS,
# kad pasirinkus 0.1s / 0.2s realus intervalas nebūtų ~0.3–1.0s.
JITTER_FRAC = (0.05, 0.25)  # 10%..40% nuo MIN_INTERVAL_SECONDS
JITTER_SECONDS = (MIN_INTERVAL_SECONDS * JITTER_FRAC[0], MIN_INTERVAL_SECONDS * JITTER_FRAC[1])

# UI mygtukai
ALLOWED_RATE_LIMITS = [0.05, 0.1, 0.2, 0.5, 1.0, 2.0]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# Persistencijos failas:
# - Lokaliai: paliekam šalia skripto
# - Render.com: rekomenduojama mount'inti diską (pvz. /var/data) ir nustatyti STATE_DIR=/var/data
DEFAULT_STATE_FILE = Path(__file__).with_name("aruodas_state.json")
STATE_FILE_ENV = (os.getenv("STATE_FILE") or "").strip()
STATE_DIR_ENV = (os.getenv("STATE_DIR") or "").strip()

if STATE_FILE_ENV:
    STATE_FILE = Path(STATE_FILE_ENV)
elif STATE_DIR_ENV:
    STATE_FILE = Path(STATE_DIR_ENV) / "aruodas_state.json"
else:
    STATE_FILE = DEFAULT_STATE_FILE

# =========================
# HTTP sesija
# =========================
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",  # requests automatiškai išpakuoja gzip/deflate
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
})

_last_request_at = 0.0
_rate_lock = threading.Lock()

# Vienu metu leidžiam tik vieną "fetch" – saugiau Session'ui ir rate limit'ui
FETCH_LOCK = threading.Lock()

# Cache atmintyje
CACHE = {}        # id -> parsed result (be raw_html)
RAW_CACHE = {}    # id -> raw_html (tik tiems, kuriuos tikrinai; NEPERSISTINAM)
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
    - STEP kol kas palaikomas tik 2 (tik nelyginiai).
    - jei start/end lyginiai, pakoreguoja į nelyginius.
    - riboja max įrašų skaičių.
    """
    if step != 2:
        raise ValueError("Šiuo metu palaikomas tik STEP=2 (tik nelyginiai ID).")

    if start > end:
        raise ValueError("start negali būti didesnis už end.")

    # automatinė korekcija į nelyginius
    if start % 2 == 0:
        start += 1
    if end % 2 == 0:
        end -= 1

    if start > end:
        raise ValueError("Po nelyginių korekcijos intervalas tuščias.")

    cnt = range_count(start, end, step)
    if cnt > MAX_RANGE_ITEMS:
        raise ValueError(f"Per didelis intervalas: {cnt} įrašų. Max: {MAX_RANGE_ITEMS}.")

    return start, end, step


def in_range_and_odd(n: int) -> bool:
    return START_NUM <= n <= END_NUM and (n % 2 == 1)


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
        raise ValueError("Netinkamas ID formatas. Pvz: 1-2890001")
    return s


def parse_range_value(v) -> int:
    """Priimam start/end kaip:
    - int
    - '3000001'
    - '1-3000001'
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

    raise ValueError("Netinkamas start/end formatas. Naudok skaičių (pvz 3000001) arba ID (pvz 1-3000001).")


def rate_limit():
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
        # tavo taisyklė: jei miestas ne Vilnius – miesto/rajono nerodom
        district = None
        city = None

    result["inserted_date"] = inserted
    result["city"] = city
    result["district"] = district
    return result


def fetch_and_parse(id_str: str) -> tuple[dict, str]:
    url = f"https://www.aruodas.lt/{id_str}/"

    # Vienu metu tik 1 requestas (apsauga nuo paralelių)
    with FETCH_LOCK:
        rate_limit()
        r = SESSION.get(url, timeout=25, allow_redirects=True)

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


# =========================
# Persistencija (istorija)
# =========================
def _safe_float(x, default: float) -> float:
    try:
        return float(x)
    except Exception:
        return default


def recompute_jitter():
    """Perskaičiuoja jitter pagal esamą MIN_INTERVAL_SECONDS."""
    global JITTER_SECONDS
    JITTER_SECONDS = (
        float(MIN_INTERVAL_SECONDS) * float(JITTER_FRAC[0]),
        float(MIN_INTERVAL_SECONDS) * float(JITTER_FRAC[1]),
    )


def load_state_from_disk():
    """Užkrauna CACHE + config (rate limit) + range iš aruodas_state.json, jei yra."""
    global MIN_INTERVAL_SECONDS, START_NUM, END_NUM, STEP

    if not STATE_FILE.exists():
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    cfg = (data or {}).get("config") or {}
    min_int = _safe_float(cfg.get("min_interval"), MIN_INTERVAL_SECONDS)
    if min_int in ALLOWED_RATE_LIMITS:
        MIN_INTERVAL_SECONDS = min_int
        recompute_jitter()

    rng = (data or {}).get("range") or {}
    try:
        start = int(rng.get("start", START_NUM))
        end = int(rng.get("end", END_NUM))
        step = int(rng.get("step", STEP))
        start, end, step = normalize_range(start, end, step)
        START_NUM, END_NUM, STEP = start, end, step
    except Exception:
        # jei range sugadintas – paliekam default/ankstesnį
        pass

    cached = (data or {}).get("cache") or {}
    if isinstance(cached, dict):
        with CACHE_LOCK:
            # paliekam tik dict įrašus
            for k, v in cached.items():
                if isinstance(k, str) and isinstance(v, dict) and "id" in v:
                    CACHE[k] = v


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
        # jei nepavyko – tyliai (nenorim crash)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def get_cached_items_for_current_range_locked():
    """Grąžina list'ą su cache įrašais tik iš esamo intervalo (CALL ONLY UNDER CACHE_LOCK)."""
    items = []
    for id_str, entry in CACHE.items():
        try:
            n = id_num(id_str)
            if in_range_and_odd(n):
                items.append(entry)
        except Exception:
            continue
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
    input[type="text"], input[type="number"] { padding: 6px 8px; font-size: 14px; }
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
    .hit { color: #0b63d1; font-weight: 700; } /* sugiharos mėlynai */
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
    .sep { width: 100%; height: 1px; background: #eee; margin: 4px 0; }
  </style>
</head>
<body>
  <h2 style="margin:0 0 6px 0;">Aruodas ID tikrintuvas</h2>

  <div class="bar">
    <span class="pill" id="rangePill">ID intervalas: <b class="mono" id="pillStart">1-__START__</b> … <b class="mono" id="pillEnd">1-__END__</b> (tik nelyginiai)</span>
    <span class="pill" id="countPill">Kiekis: <b class="mono" id="countVal">—</b></span>
    <span class="pill">User-Agent: <span class="mono">__UA__</span></span>
    <span class="pill" id="ratePill">Rate limit: min <b class="mono" id="rateVal">__MIN__</b>s + jitter</span>
    <span class="pill" id="autoPill">Auto: OFF</span>
  </div>

  <div class="bar">
    <span class="muted">Nustatyk ID intervalą (galima keisti jau pasikrovus puslapiui):</span>
    <input id="rangeStart" type="number" step="1" value="__START__" />
    <input id="rangeEnd" type="number" step="1" value="__END__" />
    <button id="btnGenerate">Generuoti sąrašą</button>
    <small class="muted">Pastaba: tik nelyginiai, STEP=2. Jei įvesi lyginį – serveris automatiškai pakoreguos į nelyginį.</small>
  </div>

  <div class="bar">
    <span class="muted">Rate limit pasirinkimas:</span>
    <button class="rate-btn" data-rate="0.05">0.05s</button>
    <button class="rate-btn" data-rate="0.1">0.1s</button>
    <button class="rate-btn" data-rate="0.2">0.2s</button>
    <button class="rate-btn" data-rate="0.5">0.5s</button>
    <button class="rate-btn" data-rate="1">1s</button>
    <button class="rate-btn" data-rate="2">2s</button>
    <small class="muted">Pasirinkimas keičia serverio limitą ir išsisaugo (persist).</small>
  </div>

  <div class="bar">
    <input id="idInput" type="text" value="1-__START__" />
    <button id="btnCheck">Tikrinti ID</button>
    <button id="btnRandom">Atsitiktinis ID</button>
    <button id="btnForce">Tikrinti (force)</button>
    <button id="btnAutoToggle">▶ Auto (OFF)</button>
    <small class="muted">Auto režimas eina per visus dar netikrintus ID. Sustos, jei gaus <b>ERROR</b>.</small>
  </div>

  <div class="grid">
    <div>
      <h3 style="margin: 8px 0;">✅ Rasti skelbimai (FOUND)</h3>
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
      <div class="muted" style="margin-top:6px;">Rūšiuojama automatiškai (naujausi viršuje).</div>
    </div>

    <div>
      <h3 style="margin: 8px 0;">📋 Visi ID</h3>
      <div class="bar">
        <input id="filter" type="text" placeholder="Filtras (pvz. 3123)" />
        <button id="btnClearFilter">Valyti filtrą</button>
        <button id="btnShowOnlyBad">Rodyti tik raudonus/oranžinius</button>
        <button id="btnShowAll">Rodyti visus</button>
      </div>
      <small class="muted">Pastaba: ERROR viršuje, po to CHALLENGE, po to NOT_FOUND (raudoni).</small>
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

// State krovimo apsauga (fix nuo race bug'ų)
let stateLoading = false;
let statePromise = null;

// Patikrintų ID rinkinys (kad auto praleistų jau tikrintus)
const checkedIds = new Set();

function totalCount(){
  return Math.floor((END-START)/STEP)+1;
}
function checkedCount(){
  return checkedIds.size;
}

function numFromId(id) { const m=/^1-(\d+)$/.exec(id); return m?parseInt(m[1],10):NaN; }
function makeId(n){ return "1-"+String(n); }
function randomOddInRange(){
  const count = Math.floor((END-START)/STEP)+1;
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

  // jei ID input tuščias arba ne intervale -> nustatom į pirmą
  const idInput = document.getElementById("idInput");
  if(idInput){
    const cur = idInput.value.trim();
    const n = numFromId(cur);
    if(!cur || !Number.isFinite(n) || n < START || n > END){
      idInput.value = makeId(START);
    }
  }

  autoNextNum = START;
}

function buildAllRow(id){
  const tr=document.createElement("tr");
  tr.dataset.id=id; tr.dataset.num=String(numFromId(id)); tr.className="unknown";
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
  tr.dataset.id=id; tr.dataset.num=String(numFromId(id)); tr.dataset.date=data.inserted_date||""; tr.className="found";

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
  if(!idCell.querySelector('a[href^="/raw"]')){
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

// ERROR viršuje, tada CHALLENGE, tada NOT_FOUND
function moveAllRowByStatus(tr, status){
  const body = document.getElementById("allBody");
  const cls = statusToClass(status);

  const order = ["error", "challenge", "notfound"];
  if(!order.includes(cls)) return;

  const idx = order.indexOf(cls);
  const beforeSet = new Set(order.slice(0, idx));
  let insertAfter = null;

  const rows = Array.from(body.querySelectorAll("tr"));
  for(const r of rows){
    if(beforeSet.has(r.className)) insertAfter = r;
  }

  if(insertAfter){
    body.insertBefore(tr, insertAfter.nextSibling);
  } else {
    body.insertBefore(tr, body.firstChild);
  }
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
  pill.textContent = `${base} (${prog})${extraText ? " – " + extraText : ""}`;
}

function applyResultToUi(data){
  const idNorm=data.id;
  checkedIds.add(idNorm);

  const allRow=document.querySelector(`#allBody tr[data-id="${CSS.escape(idNorm)}"]`);

  // PATAISA: jei sugiharos rasta, laikom kaip "rastas" net jei CHALLENGE
  const putToFound = (data.status === "FOUND") || (data.sugiharos_found === true);

  if(putToFound){
    if(allRow) allRow.remove();
    const existing=document.querySelector(`#foundBody tr[data-id="${CSS.escape(idNorm)}"]`);
    if(existing) existing.remove();
    document.getElementById("foundBody").appendChild(buildFoundRow(idNorm,data));
    sortFoundTable();
  } else {
    if(allRow){
      updateAllRow(allRow,data);
      moveAllRowByStatus(allRow, data.status);
    }
  }

  updateAutoPill();
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
      sugiharos_snippet_html: null
    };
  }

  if(!resp || !resp.ok || data.error){
    data = data || {};
    if(!data.id) data.id = id;
    if(!data.status) data.status = "ERROR";
    if(!data.error) data.error = data.error || "Klaida tikrinant ID";
    applyResultToUi(data);
    applyFilter();
    if(!silent){
      alert(data.error || "Klaida tikrinant ID");
    }
    return data;
  }

  applyResultToUi(data);
  applyFilter();
  return data;
}

function initAllTable(){
  const body=document.getElementById("allBody");
  body.innerHTML = "";
  const frag=document.createDocumentFragment();
  for(let n=START;n<=END;n+=STEP) frag.appendChild(buildAllRow(makeId(n)));
  body.appendChild(frag);
}

// Persisted state + dynamic range užkrovimas
async function reloadEverything(){
  if(stateLoading && statePromise) return statePromise;

  stateLoading = true;
  setControlsDisabled(true, !autoRunning); // auto išjungiam tik jei auto ne running
  setAutoButtonUi();
  updateAutoPill("kraunama…");

  statePromise = (async()=>{
    try{
      // reset UI
      checkedIds.clear();
      document.getElementById("foundBody").innerHTML = "";
      document.getElementById("allBody").innerHTML = "";

      const resp = await fetch("/api/state");
      const state = await resp.json();

      const cfg = state.config || {};
      if(cfg.min_interval !== undefined && cfg.min_interval !== null){
        updateRateUi(cfg.min_interval);
      }

      const rng = state.range || {};
      if(rng.start && rng.end && rng.step){
        START = parseInt(rng.start,10);
        END   = parseInt(rng.end,10);
        STEP  = parseInt(rng.step,10);
      }

      updateRangeUi();
      initAllTable();

      const items = state.items || [];
      for(const item of items){
        applyResultToUi(item);
      }

      sortFoundTable();
      applyFilter();
      updateAutoPill();
    } finally {
      stateLoading = false;

      if(autoRunning){
        // Auto režime control'ai turi likti disabled, bet STOP turi veikti
        setControlsDisabled(true, false);
      } else {
        // Normaliai viską atrakinti (įskaitant Auto)
        setControlsDisabled(false, true);
      }

      setAutoButtonUi();
      updateAutoPill();
    }
  })();

  return statePromise;
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

// Auto UI / toggle
function setControlsDisabled(disabled, disableAuto=false){
  // auto mygtukas – kartais reikia išjungti (kai kraunam state / keičiam range),
  // bet kai autoRunning=true – auto mygtukas turi likti aktyvus STOP'ui.
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

  ["filter","btnClearFilter","btnShowOnlyBad","btnShowAll","idInput","rangeStart","rangeEnd"].forEach(id=>{
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

// suranda kitą netikrintą ID, pradedant nuo fromNum (ir apsukant ratą)
function findNextUnchecked(fromNum){
  const total = totalCount();
  let n = fromNum;

  for(let i=0;i<total;i++){
    if(n > END) n = START;
    const id = makeId(n);
    if(!checkedIds.has(id)){
      return {id, n};
    }
    n += STEP;
  }
  return null;
}

async function runAuto(){
  if(autoRunning) return;

  // jei dar kraunam state – palaukiam (fix nuo “sustoja” bug’o)
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
    const next = findNextUnchecked(n);
    if(!next){
      updateAutoPill("baigta (viskas patikrinta)");
      break;
    }

    updateAutoPill(`tikrinamas ${next.id}`);

    const res = await checkId(next.id, false, true);

    // Sustabdom auto, jei gavom ERROR
    if(!res || res.status === "ERROR" || res.error){
      updateAutoPill(`SUSTABDYTA: ERROR ties ${next.id}`);
      break;
    }

    n = next.n + STEP;
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

// Click handleriai
document.addEventListener("click",async(e)=>{
  const btn=e.target.closest("button[data-action='check']");
  if(btn) await checkId(btn.dataset.id,false,false);

  const rbtn = e.target.closest("button.rate-btn");
  if(rbtn){
    const val = parseFloat(rbtn.dataset.rate);
    await setRate(val);
  }
});

document.getElementById("btnCheck").addEventListener("click",async()=>{ await checkId(document.getElementById("idInput").value.trim(),false,false); });
document.getElementById("btnForce").addEventListener("click",async()=>{ await checkId(document.getElementById("idInput").value.trim(),true,false); });
document.getElementById("btnRandom").addEventListener("click",async()=>{
  const id=makeId(randomOddInRange());
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

// Generuoti sąrašą pagal naują intervalą
document.getElementById("btnGenerate").addEventListener("click", async()=>{
  if(autoRunning){
    alert("Sustabdyk Auto prieš keičiant intervalą.");
    return;
  }

  // kad neliktų “race” – kol keičiam range ir kraunam state, išjungiam viską (įskaitant Auto)
  setControlsDisabled(true, true);

  const s = document.getElementById("rangeStart").value;
  const e = document.getElementById("rangeEnd").value;

  // reset filter režimų (kad vartotojui būtų aišku, jog naujas sąrašas)
  document.getElementById("filter").value = "";
  showOnlyBad = false;

  const ok = await setRangeOnServer(s, e);
  if(!ok){
    setControlsDisabled(false, true);
    return;
  }

  // per naują užsikraunam state + naują range + atstatom lenteles
  await reloadEverything();
});

document.getElementById("filter").addEventListener("input",applyFilter);
document.getElementById("btnClearFilter").addEventListener("click",()=>{ document.getElementById("filter").value=""; showOnlyBad=false; applyFilter(); });
document.getElementById("btnShowOnlyBad").addEventListener("click",()=>{ showOnlyBad=true; applyFilter(); });
document.getElementById("btnShowAll").addEventListener("click",()=>{ showOnlyBad=false; applyFilter(); });

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
statePromise = reloadEverything();
</script>
</body></html>
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
    )
    return Response(html_page, mimetype="text/html; charset=utf-8")


@app.get("/api/state")
def api_state():
    with CACHE_LOCK:
        items = get_cached_items_for_current_range_locked()
        cfg = {
            "min_interval": MIN_INTERVAL_SECONDS,
            "allowed_rates": ALLOWED_RATE_LIMITS,
            "state_file": str(STATE_FILE),
            "max_range_items": MAX_RANGE_ITEMS,
        }
        rng = {
            "start": START_NUM,
            "end": END_NUM,
            "step": STEP,
            "count": range_count(START_NUM, END_NUM, STEP),
        }
    return jsonify({"config": cfg, "range": rng, "items": items})


@app.get("/api/config")
def api_config_get():
    return jsonify({
        "min_interval": MIN_INTERVAL_SECONDS,
        "allowed_rates": ALLOWED_RATE_LIMITS,
    })


@app.post("/api/config")
def api_config_set():
    global MIN_INTERVAL_SECONDS
    payload = request.get_json(silent=True) or {}
    val = payload.get("min_interval", None)
    try:
        f = float(val)
    except Exception:
        return jsonify({"error": "min_interval turi būti skaičius (0.1 / 0.2 / 0.5 / 1 / 2)."}), 400

    if f not in ALLOWED_RATE_LIMITS:
        return jsonify({"error": f"Leidžiamos reikšmės: {ALLOWED_RATE_LIMITS}"}), 400

    MIN_INTERVAL_SECONDS = f
    recompute_jitter()
    with CACHE_LOCK:
        save_state_to_disk_locked()
    return jsonify({"min_interval": MIN_INTERVAL_SECONDS})


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
        save_state_to_disk_locked()

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
        if not in_range_and_odd(n):
            return jsonify({"error": "ID ne intervale arba ne nelyginis", "id": id_str}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    with CACHE_LOCK:
        if not force and id_str in CACHE:
            return jsonify(CACHE[id_str])

    try:
        out, raw_html = fetch_and_parse(id_str)
        with CACHE_LOCK:
            CACHE[id_str] = out
            RAW_CACHE[id_str] = raw_html[:500_000]  # apsauga nuo per didelės atminties
            save_state_to_disk_locked()
        return jsonify(out)
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
            save_state_to_disk_locked()
        return jsonify(err), 200


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

    # text/plain – kad naršyklė nepaleistų jokių skriptų
    return Response(raw_html, mimetype="text/plain; charset=utf-8")


@app.post("/api/debug/parse")
def api_debug_parse():
    payload = request.get_json(silent=True) or {}
    html_text = payload.get("html", "") or ""
    final_url = payload.get("final_url", "") or ""
    parsed = parse_html(html_text, final_url=final_url, http_status=None)
    return jsonify(parsed)


if __name__ == "__main__":
    # Lokalus paleidimas (Render'e paleidžiama su gunicorn)
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    app.run(host=host, port=port, debug=False)
