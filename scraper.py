"""
Monitor naborów PSP (woj. śląskie) - niezależny od Google.

Co robi:
1. Pobiera stronę KW PSP Katowice z listą naborów
2. Wykrywa nowe ogłoszenia (porównując z data.csv)
3. Dopisuje nowe wiersze do data.csv (miasto, data, stanowisko, link - reszta do ręcznego uzupełnienia)
4. Wysyła powiadomienie na Telegram o nowych naborach
5. Generuje dashboard.html (tabele + wykresy, Chart.js) na podstawie wszystkich danych

Uruchamiane cyklicznie przez GitHub Actions (patrz .github/workflows/check-nabory.yml)
"""

import csv
import json
import os
import re
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

CITY_RE = re.compile(
    r"PSP\s+w\s+([A-ZŚŻŹĆŁÓĄĘŃ][\wąćęłńóśźż\-]+(?:\s+[A-ZŚŻŹĆŁÓĄĘŃ][\wąćęłńóśźż\-]+){0,2})"
)
DATE_RE = re.compile(
    r"zamieszczon\w*\s+(\d{1,2}[.\s]\d{1,2}[.\s]\d{4}|\d{1,2}\s+\w+\s+\d{4})", re.I
)
POSITION_RE = re.compile(
    r"docelowo\s*[:\-–]?\s*([a-ząćęłńóśźż\s\-]+?)(?:\)|,|\s+w\s+(?:Jednostce|Komendzie|Sekcji|podległych))",
    re.I,
)


def fetch_page():
    resp = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text


def extract_content_tags(html):
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(
        lambda tag: tag.name in ("h1", "h2") and "Nabory do KP/M PSP" in tag.get_text()
    )
    if not heading:
        return list(soup.find_all("a"))
    tags = []
    for sib in heading.find_all_next():
        text = sib.get_text(strip=True)
        if text.startswith("Informacje o publikacji dokumentu"):
            break
        tags.append(sib)
    return tags


def find_nabor_links(content_tags):
    links, seen = [], set()
    for tag in content_tags:
        if tag.name == "a" and tag.get("href"):
            href = tag["href"].split("#")[0]
            if re.search(r"gov\.pl/web/(km|kp)psp", href) and href not in seen:
                seen.add(href)
                links.append((href, tag))
    return links


def parse_entry(href, anchor_tag):
    context, node = "", anchor_tag
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        context = node.get_text(" ", strip=True)
        if len(context) > 200:
            break

    date_m = DATE_RE.search(context)
    city_m = CITY_RE.findall(context)
    pos_m = POSITION_RE.search(context)

    return {
        "miasto": city_m[-1] if city_m else "",
        "data_ogloszenia": date_m.group(1) if date_m else "",
        "liczba_stanowisk": "",
        "stanowisko_docelowe": pos_m.group(1).strip() if pos_m else "",
        "wymagania": "",
        "link": href,
        "i_etap_chetni": "", "ii_etap_zdalo": "", "zdawalnosc_pct": "",
        "iv_etap": "", "v_etap": "", "wynagrodzenie_brutto": "",
        "wynagrodzenie_netto": "", "uwagi": "", "udzial": "",
    }


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

        d = r.get("data_ogloszenia") or ""
        dm = re.match(r"(\d{1,2})[.\s](\d{1,2})[.\s](\d{4})", d)
        if dm:
            key = f"{dm.group(3)}-{int(dm.group(2)):02d}"
            by_month[key] = by_month.get(key, 0) + 1

    city_labels = sorted(by_city, key=lambda c: -by_city[c]["count"])
    city_counts = [by_city[c]["count"] for c in city_labels]
    zd_labels = [c for c in city_labels if by_city[c]["zd_n"] > 0]
    zd_values = [round(by_city[c]["zd_sum"] / by_city[c]["zd_n"], 1) for c in zd_labels]
    br_labels = [c for c in city_labels if by_city[c]["br_n"] > 0]
    br_values = [round(by_city[c]["br_sum"] / by_city[c]["br_n"]) for c in br_labels]
    month_labels = sorted(by_month)
    month_values = [by_month[m] for m in month_labels]

    html = (
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
        f.write(html)


DASHBOARD_TEMPLATE = """<!doctype html>
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
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 32px; }
  .card { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .card h2 { font-size: 15px; margin: 0 0 12px; }
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
renderTable(DATA);

document.getElementById('search').addEventListener('input', (e) => {
  const q = e.target.value.toLowerCase();
  renderTable(DATA.filter(r => JSON.stringify(r).toLowerCase().includes(q)));
});
</script>
</body>
</html>
"""


def main():
    html = fetch_page()
    content_tags = extract_content_tags(html)
    links = find_nabor_links(content_tags)

    existing = load_existing()
    existing_links = {r["link"] for r in existing}

    new_rows = [parse_entry(href, tag) for href, tag in links if href not in existing_links]

    if new_rows:
        all_rows = existing + new_rows
        save_all(all_rows)
        send_telegram(new_rows)
        print(f"Dodano {len(new_rows)} nowych naborów.")
    else:
        all_rows = existing
        print("Brak nowych naborów.")

    build_dashboard(all_rows)


if __name__ == "__main__":
    main()
