"""
Monitor naborow PSP (woj. slaskie) - niezalezny od Google.

Co robi:
1. Pobiera strone KW PSP Katowice z lista naborow
2. Wykrywa nowe ogloszenia (porownujac z data.csv) i dopisuje je
3. Douzupelnia brakujace pola (miasto/data/stanowisko) w JUZ zapisanych
   wierszach, jesli link nadal jest widoczny na stronie - nigdy nie
   nadpisuje pol, ktore juz maja jakas wartosc (np. recznie wpisana
   zdawalnosc czy wynagrodzenie)
4. Wysyla powiadomienie na Telegram o nowych naborach (opcjonalne)
5. Generuje dashboard.html (tabele + wykresy Chart.js)
"""

import csv
import html as html_lib
import os
import re
import json
from datetime import datetime

import requests
from bs4 import BeautifulSoup

URL = "https://www.gov.pl/web/kwpsp-katowice/nabory-do-kpm-psp-wojslaskiego"
DATA_CSV = "data.csv"
DASHBOARD_HTML = "dashboard.html"

FIELDNAMES = [
    "miasto", "data_ogloszenia", "liczba_stanowisk", "stanowisko_docelowe",
    "wymagania", "link", "i_etap_chetni", "ii_etap_zdalo", "zdawalnosc_pct",
    "iv_etap", "v_etap", "wynagrodzenie_brutto", "wynagrodzenie_netto",
    "uwagi", "udzial",
]

# Nagłówek "Ogłoszenie(a) zamieszczone DD.MM.RRRR roku:" - zawsze format cyfrowy
DATE_RE = re.compile(r"zamieszczon\w*\s+(\d{1,2}[.\s]\d{1,2}[.\s]\d{4})", re.I)

# Miasto: albo skrót "PSP w X", albo pełna forma "Straży Pożarnej w X"
CITY_RE = re.compile(
    r"(?:PSP|Straży\s+Pożarnej)\s+w\s+"
    r"([A-ZŚŻŹĆŁÓĄĘŃ][\wąćęłńóśźż\-]+(?:\s+[A-ZŚŻŹĆŁÓĄĘŃ][\wąćęłńóśźż\-]+){0,2})",
    re.I,
)

# Stanowisko: to co jest w nawiasie po słowie "stażyst(a/y)"
POSITION_RE = re.compile(r"sta[żz]yst\w*\s*\(([^)]+)\)", re.I)

# Liczba stanowisk (best-effort)
WORD_NUM = {"dwa": 2, "trzy": 3, "cztery": 4, "pięć": 5}
COUNT_RE = re.compile(
    r"liczba stanowisk[:\-–]?\s*(\d+)"
    r"|na\s+(\d+)\s+stanowisk"
    r"|na\s+(dwa|trzy|cztery|pięć)\s+stanowisk",
    re.I,
)

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
}


def fetch_page():
    resp = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text


def extract_content(html_text):
    """Zwraca (raw_content_html, plain_text) - sekcja miedzy naglowkiem a stopka historii."""
    start = html_text.find("Nabory do KP/M PSP woj")
    section = html_text[start if start > -1 else 0:]
    end = section.find("Informacje o publikacji dokumentu")
    content = section[:end] if end > -1 else section
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", content))
    plain = re.sub(r"\s+", " ", plain)
    return content, plain


def find_links(content):
    seen, links = set(), []
    for m in re.finditer(r'<a[^>]+href="(https?://www\.gov\.pl/web/(?:km|kp)psp[^"]+)"', content):
        href = m.group(1).split("#")[0].rstrip("/")
        if href not in seen:
            seen.add(href)
            links.append((href, m.start()))
    return links


def plain_pos_for(content, raw_idx):
    """Przyblizona pozycja w plain-texcie odpowiadajaca pozycji raw_idx w content (z tagami)."""
    prefix = content[:raw_idx]
    return len(html_lib.unescape(re.sub(r"<[^>]+>", " ", prefix)))


def nearest_before(matches, pos):
    """Zwraca ostatni match (re.Match) ktorego .start() <= pos, albo None."""
    best = None
    for m in matches:
        if m.start() <= pos:
            best = m
        else:
            break
    return best


def clean_position(raw):
    if not raw:
        return ""
    raw = raw.strip()
    raw = re.sub(r"^docelowo\s*[:\-–]?\s*", "", raw, flags=re.I)
    raw = re.sub(r"^stanowisko\s+docelowe\s*[:\-–]?\s*", "", raw, flags=re.I)
    raw = raw.split(",")[0].strip()
    return raw


def extract_count(snippet):
    m = COUNT_RE.search(snippet)
    if not m:
        return ""
    if m.group(1):
        return m.group(1)
    if m.group(2):
        return m.group(2)
    if m.group(3):
        return str(WORD_NUM.get(m.group(3).lower(), ""))
    return ""


def parse_all(content, plain):
    """Zwraca dict: link -> {miasto, data_ogloszenia, stanowisko_docelowe, liczba_stanowisk}"""
    date_matches = list(DATE_RE.finditer(plain))
    city_matches = list(CITY_RE.finditer(plain))
    pos_matches = list(POSITION_RE.finditer(plain))

    result = {}
    for href, raw_idx in find_links(content):
        p = plain_pos_for(content, raw_idx)

        dm = nearest_before(date_matches, p)
        cm = nearest_before(city_matches, p)
        # stanowisko zwykle jest PRZED linkiem w tym samym akapicie
        pm = nearest_before(pos_matches, p)
        # jesli najblizszy wczesniejszy jest za daleko (>1200 znakow), to raczej nalezy do innego akapitu
        if pm and (p - pm.start()) > 1200:
            pm = None

        # kontekst do wyszukania liczby stanowisk (500 znakow wstecz)
        snippet = plain[max(0, p - 800):p]

        result[href] = {
            "miasto": cm.group(1).strip() if cm else "",
            "data_ogloszenia": dm.group(1).strip() if dm else "",
            "stanowisko_docelowe": clean_position(pm.group(1)) if pm else "",
            "liczba_stanowisk": extract_count(snippet),
        }
    return result


def load_existing():
    if not os.path.exists(DATA_CSV):
        return []
    with open(DATA_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_all(rows):
    with open(DATA_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in FIELDNAMES})


def send_telegram(new_rows):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Brak TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID - pomijam powiadomienie.")
        return
    lines = [
        f"🚒 Nowy nabór: {r['miasto'] or '?'} — {r['stanowisko_docelowe'] or '?'}\n{r['link']}"
        for r in new_rows
    ]
    text = "\n\n".join(lines)
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except requests.RequestException as e:
        print("Nie udało się wysłać powiadomienia Telegram:", e)


def parse_any_date(d):
    if not d:
        return None
    m = re.match(r"(\d{1,2})[.\s](\d{1,2})[.\s](\d{4})", d)
    if m:
        return int(m.group(3)), int(m.group(2))
    m = re.match(r"(\d{1,2})\s+([a-złąęśćźżó]+)\s+(\d{4})", d, re.I)
    if m and m.group(2).lower() in MONTHS_PL:
        return int(m.group(3)), MONTHS_PL[m.group(2).lower()]
    return None


def build_dashboard(rows):
    by_city, by_month = {}, {}
    for r in rows:
        city = (r.get("miasto") or "").strip()
        if not city:
            continue
        c = by_city.setdefault(city, {"count": 0, "zd_sum": 0.0, "zd_n": 0, "br_sum": 0.0, "br_n": 0})
        c["count"] += 1

        zd = r.get("zdawalnosc_pct") or ""
        if zd:
            try:
                v = float(str(zd).replace("%", "").replace(",", ".").strip())
                c["zd_sum"] += v
                c["zd_n"] += 1
            except ValueError:
                pass

        br = r.get("wynagrodzenie_brutto") or ""
        if br:
            try:
                v = float(re.sub(r"[^\d,.-]", "", str(br)).replace(",", "."))
                c["br_sum"] += v
                c["br_n"] += 1
            except ValueError:
                pass

        ym = parse_any_date(r.get("data_ogloszenia") or "")
        if ym:
            key = f"{ym[0]}-{ym[1]:02d}"
            by_month[key] = by_month.get(key, 0) + 1

    city_labels = sorted(by_city, key=lambda c: -by_city[c]["count"])
    city_counts = [by_city[c]["count"] for c in city_labels]
    zd_labels = [c for c in city_labels if by_city[c]["zd_n"] > 0]
    zd_values = [round(by_city[c]["zd_sum"] / by_city[c]["zd_n"], 1) for c in zd_labels]
    br_labels = [c for c in city_labels if by_city[c]["br_n"] > 0]
    br_values = [round(by_city[c]["br_sum"] / by_city[c]["br_n"]) for c in br_labels]
    month_labels = sorted(by_month)
    month_values = [by_month[m] for m in month_labels]

    out = (
        DASHBOARD_TEMPLATE
        .replace("__DATA__", json.dumps(rows, ensure_ascii=False))
        .replace("__CITY_LABELS__", json.dumps(city_labels, ensure_ascii=False))
        .replace("__CITY_COUNTS__", json.dumps(city_counts))
        .replace("__ZD_LABELS__", json.dumps(zd_labels, ensure_ascii=False))
        .replace("__ZD_VALUES__", json.dumps(zd_values))
        .replace("__BR_LABELS__", json.dumps(br_labels, ensure_ascii=False))
        .replace("__BR_VALUES__", json.dumps(br_values))
        .replace("__MONTH_LABELS__", json.dumps(month_labels))
        .replace("__MONTH_VALUES__", json.dumps(month_values))
        .replace("__UPDATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    with open(DASHBOARD_HTML, "w", encoding="utf-8") as f:
        f.write(out)


DASHBOARD_TEMPLATE = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<title>Nabory PSP – woj. śląskie</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; padding: 24px; background: #f6f7f9; color: #1a1a1a; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .updated { color: #666; font-size: 13px; margin-bottom: 24px; }
  .card { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 32px; }
  .card h2 { font-size: 15px; margin: 0 0 12px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 32px; }
  .grid .card { margin-bottom: 0; }
  .latest-row { padding: 10px 0; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
  .latest-row:last-child { border-bottom: none; }
  .latest-meta { color: #666; font-size: 12px; margin-top: 2px; }
  table { width: 100%; border-collapse: collapse; background: white; border-radius: 12px; overflow: hidden; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; font-size: 13px; }
  th { background: #fafafa; }
  tr:hover { background: #fafafa; }
  input#search { padding: 8px 12px; width: 100%; max-width: 400px; margin-bottom: 12px; border: 1px solid #ddd; border-radius: 8px; }
  a { color: #c0392b; }
  @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
  <h1>🚒 Nabory PSP – woj. śląskie</h1>
  <div class="updated">Ostatnia aktualizacja: __UPDATED__ (dane odświeżane automatycznie co 12h przez GitHub Actions)</div>

  <div class="card">
    <h2>Najnowsze ogłoszenia</h2>
    <div id="latestList"></div>
  </div>

  <div class="grid">
    <div class="card"><h2>Liczba naborów wg miasta</h2><canvas id="chartCity"></canvas></div>
    <div class="card"><h2>Średnia zdawalność II ETAP (%)</h2><canvas id="chartZd"></canvas></div>
    <div class="card"><h2>Trend miesięczny liczby ogłoszeń</h2><canvas id="chartMonth"></canvas></div>
    <div class="card"><h2>Średnie wynagrodzenie brutto wg miasta</h2><canvas id="chartBr"></canvas></div>
  </div>

  <input id="search" placeholder="Szukaj (miasto, stanowisko...)">
  <table id="dataTable">
    <thead>
      <tr>
        <th>Miasto</th><th>Data</th><th>Stanowisk</th><th>Stanowisko docelowe</th>
        <th>Zdawalność II</th><th>Wynagrodzenie brutto</th><th>Link</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>

<script>
const DATA = __DATA__;

function parseDate(str) {
  if (!str) return null;
  let m = str.match(/(\d{1,2})[.\s](\d{1,2})[.\s](\d{4})/);
  if (m) return new Date(+m[3], +m[2]-1, +m[1]);
  const months = {'stycznia':0,'lutego':1,'marca':2,'kwietnia':3,'maja':4,'czerwca':5,'lipca':6,'sierpnia':7,'września':8,'października':9,'listopada':10,'grudnia':11};
  m = str.match(/(\d{1,2})\s+([a-złąęśćźżó]+)\s+(\d{4})/i);
  if (m && months[m[2].toLowerCase()] !== undefined) return new Date(+m[3], months[m[2].toLowerCase()], +m[1]);
  return null;
}

const SORTED = [...DATA].sort((a, b) => {
  const da = parseDate(a.data_ogloszenia), db = parseDate(b.data_ogloszenia);
  if (da && db) return db - da;
  if (da) return -1;
  if (db) return 1;
  return 0;
});

function bar(id, labels, values, color) {
  new Chart(document.getElementById(id), {
    type: 'bar',
    data: { labels, datasets: [{ data: values, backgroundColor: color }] },
    options: { plugins: { legend: { display: false } }, scales: { x: { ticks: { autoSkip: false, maxRotation: 60 } } } }
  });
}
function line(id, labels, values, color) {
  new Chart(document.getElementById(id), {
    type: 'line',
    data: { labels, datasets: [{ data: values, borderColor: color, tension: 0.3 }] },
    options: { plugins: { legend: { display: false } } }
  });
}

bar('chartCity', __CITY_LABELS__, __CITY_COUNTS__, '#c0392b');
bar('chartZd', __ZD_LABELS__, __ZD_VALUES__, '#2980b9');
line('chartMonth', __MONTH_LABELS__, __MONTH_VALUES__, '#27ae60');
bar('chartBr', __BR_LABELS__, __BR_VALUES__, '#8e44ad');

function renderLatest(rows) {
  document.getElementById('latestList').innerHTML = rows.slice(0, 5).map(r => `
    <div class="latest-row">
      <div>
        <strong>${r.miasto || '?'}</strong> — ${r.stanowisko_docelowe || 'brak danych o stanowisku'}
        <div class="latest-meta">${r.data_ogloszenia || 'brak daty'}</div>
      </div>
      <a href="${r.link}" target="_blank" rel="noopener">otwórz</a>
    </div>
  `).join('');
}

function renderTable(rows) {
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${r.miasto || ''}</td>
      <td>${r.data_ogloszenia || ''}</td>
      <td>${r.liczba_stanowisk || ''}</td>
      <td>${r.stanowisko_docelowe || ''}</td>
      <td>${r.zdawalnosc_pct || ''}</td>
      <td>${r.wynagrodzenie_brutto || ''}</td>
      <td><a href="${r.link}" target="_blank" rel="noopener">otwórz</a></td>
    </tr>
  `).join('');
}

renderLatest(SORTED);
renderTable(SORTED);

document.getElementById('search').addEventListener('input', (e) => {
  const q = e.target.value.toLowerCase();
  renderTable(SORTED.filter(r => JSON.stringify(r).toLowerCase().includes(q)));
});
</script>
</body>
</html>
"""


def main():
    html_text = fetch_page()
    content, plain = extract_content(html_text)
    parsed = parse_all(content, plain)  # link -> {miasto,data_ogloszenia,stanowisko_docelowe,liczba_stanowisk}

    existing = load_existing()
    existing_links = {r["link"] for r in existing}

    # 1) douzupelnij braki w istniejacych wierszach (nigdy nie nadpisuj niepustych pol)
    filled = 0
    for r in existing:
        info = parsed.get(r.get("link", "").rstrip("/"))
        if not info:
            continue
        for field in ("miasto", "data_ogloszenia", "stanowisko_docelowe", "liczba_stanowisk"):
            if not (r.get(field) or "").strip() and info.get(field):
                r[field] = info[field]
                filled += 1

    # 2) dopisz nowe nabory
    new_rows = []
    for href, info in parsed.items():
        if href not in existing_links:
            new_rows.append({
                "miasto": info["miasto"],
                "data_ogloszenia": info["data_ogloszenia"],
                "liczba_stanowisk": info["liczba_stanowisk"],
                "stanowisko_docelowe": info["stanowisko_docelowe"],
                "wymagania": "", "link": href,
                "i_etap_chetni": "", "ii_etap_zdalo": "", "zdawalnosc_pct": "",
                "iv_etap": "", "v_etap": "", "wynagrodzenie_brutto": "",
                "wynagrodzenie_netto": "", "uwagi": "", "udzial": "",
            })

    all_rows = existing + new_rows
    if new_rows or filled:
        save_all(all_rows)
        print(f"Dodano {len(new_rows)} nowych naborów, douzupełniono {filled} pól.")
    if new_rows:
        send_telegram(new_rows)
    if not new_rows and not filled:
        print("Brak zmian.")

    build_dashboard(all_rows)


if __name__ == "__main__":
    main()
