#!/usr/bin/env python3
"""
GeoGuessr Duels Analytics
Установка: pip install requests pycountry
Запуск:    python geoguessr_duels.py
"""

import requests
import json
import re
import time
import os
import sys
from collections import defaultdict
from datetime import datetime

try:
    import pycountry
except ImportError:
    sys.exit("❌ pip install pycountry")


# ── Конфиг ───────────────────────────────────────────────────────────────────

NCFA_COOKIE   = ""
MAX_DUELS     = 500
REQUEST_DELAY = 0.25
CACHE_FILE    = "duels_cache.json"
OUTPUT_HTML   = "duels_report.html"
MIN_ROUNDS    = 3


# ── Вспомогательное ───────────────────────────────────────────────────────────

_cc_cache: dict[str, str] = {}

def cc_to_name(cc: str) -> str:
    if not cc or cc == "Unknown":
        return "Unknown"
    cc = cc.upper()
    if cc in _cc_cache:
        return _cc_cache[cc]
    try:
        c = pycountry.countries.get(alpha_2=cc)
        name = c.name if c else cc
    except Exception:
        name = cc
    _cc_cache[cc] = name
    return name


# ── API ───────────────────────────────────────────────────────────────────────

FEED_URL    = "https://www.geoguessr.com/api/v4/feed/private"
DUELS_URL   = "https://game-server.geoguessr.com/api/duels"
PROFILE_URL = "https://www.geoguessr.com/api/v3/profiles"

def make_session(ncfa: str) -> requests.Session:
    s = requests.Session()
    s.cookies.set("_ncfa", ncfa, domain=".geoguessr.com")
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    })
    return s

def get_profile(session) -> tuple[str, str]:
    """Возвращает (user_id, username)."""
    r = session.get(PROFILE_URL)
    r.raise_for_status()
    data = r.json()
    # Профиль вложен в data["user"]
    user = data.get("user", data)
    uid  = user.get("id") or user.get("userId", "")
    nick = user.get("nick") or user.get("name", "unknown")
    return uid, nick

def get_duel_ids_from_feed(session, max_duels: int) -> list[str]:
    """
    Листает ленту через cursor-пагинацию.
    payload каждого entry — JSON-строка, которую нужно распарсить отдельно.
    """
    ids = []
    cursor = None
    page = 0

    while len(ids) < max_duels:
        params = {"count": 100}
        if cursor:
            params["paginationToken"] = cursor
        r = session.get(FEED_URL, params=params)
        r.raise_for_status()
        data    = r.json()
        entries = data.get("entries", [])

        if not entries:
            print("  Лента закончилась.")
            break

        new = 0
        for entry in entries:
            raw_payload = entry.get("payload", "")
            # payload — это JSON-строка, парсим её
            try:
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            except Exception:
                continue

            # payload может быть списком (несколько игр в одном entry)
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                p = item.get("payload", item)  # иногда ещё один уровень вложенности
            # Все дуэли — Team/1v1 отфильтруем позже по данным самой игры
                if p.get("gameMode") == "Duels":
                    gid = p.get("gameId")
                    if gid and gid not in ids:
                        ids.append(gid)
                        new += 1

        cursor = data.get("paginationToken")
        page += 1
        print(f"  стр. {page}: +{new} дуэлей ({len(ids)} всего)...")

        if not cursor:
            break

        time.sleep(REQUEST_DELAY)

    return ids[:max_duels]

def get_duel(game_id: str, session: requests.Session) -> dict | None:
    try:
        r = session.get(f"{DUELS_URL}/{game_id}", timeout=10)
        if r.status_code in (404, 403):
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ⚠️  {game_id}: {e}")
        return None


# ── Парсинг дуэли ─────────────────────────────────────────────────────────────

def detect_game_mode(duel: dict) -> str:
    # options.movementOptions.forbidMoving — основной путь
    movement = (
        duel.get("options", {}).get("movementOptions")
        or duel.get("movementOptions")
        or {}
    )
    if movement.get("forbidMoving"):
        if movement.get("forbidRotating") or movement.get("forbidZooming"):
            return "NMPZ"
        return "NoMove"
    return "Move"

def find_my_team(duel: dict, my_id: str):
    teams = duel.get("teams", [])
    for i, team in enumerate(teams):
        for p in team.get("players", []):
            if p.get("playerId") == my_id:
                opp = teams[1 - i] if len(teams) == 2 else None
                return team, opp, p
    return None, None, None

def parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # Нормализуем в UTC: убираем +00:00 или Z, оставляем до мс
        s = re.sub(r'\+00:00$', 'Z', s)
        s = re.sub(r'(\.\d{3})\d*Z$', r'\1Z', s)
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        return None

def parse_duel(duel: dict, my_id: str) -> list[dict] | None:
    if not duel:
        return None
    # Пропускаем Team Duels
    if duel.get("options", {}).get("isTeamDuels"):
        return None
    rounds_data = duel.get("rounds", [])
    if not rounds_data:
        return None

    game_id   = duel.get("gameId", "")
    game_mode = detect_game_mode(duel)
    my_team, opp_team, me = find_my_team(duel, my_id)
    if not my_team:
        return None

    my_guesses     = me.get("guesses", [])
    guess_by_round = {g["roundNumber"]: g for g in my_guesses}

    # Гессы оппонента для сравнения скоров по раундам
    opp_guess_by_round = {}
    if opp_team:
        for opp_player in opp_team.get("players", []):
            for g in opp_player.get("guesses", []):
                rn = g["roundNumber"]
                # берём лучший гесс команды оппонента в раунде
                if rn not in opp_guess_by_round or g["score"] > opp_guess_by_round[rn]["score"]:
                    opp_guess_by_round[rn] = g

    results = []
    for i, rnd in enumerate(rounds_data):
        rnum  = i + 1
        guess = guess_by_round.get(rnum)
        if not guess:
            continue

        cc      = (rnd.get("panorama") or {}).get("countryCode", "") or ""
        score   = guess.get("score", 0)
        dist_km = guess.get("distance", 0) / 1000

        time_sec = None
        t0 = parse_dt(rnd.get("startTime", ""))
        t1 = parse_dt(guess.get("created", ""))
        if t0 and t1:
            dt = (t1 - t0).total_seconds()
            if 0 < dt < 300:
                time_sec = dt

        opp_guess = opp_guess_by_round.get(rnum)
        round_won = bool(opp_guess and score > opp_guess.get("score", 0))

        results.append({
            "actual_cc":      cc.upper() if cc else "Unknown",
            "actual_country": cc_to_name(cc),
            "score":          score,
            "dist_km":        round(dist_km, 1),
            "time_sec":       time_sec,
            "round_won":      round_won,
            "game_id":        game_id,
            "game_mode":      game_mode,
        })

    return results if results else None


# ── Статистика ────────────────────────────────────────────────────────────────

def score_bucket(score: int) -> str:
    if score >= 4900: return "5k"
    if score >= 4000: return "4k+"
    if score >= 3000: return "3k+"
    if score >= 2000: return "2k+"
    return "<2k"

def compute_stats(rounds: list[dict]) -> dict:
    raw = defaultdict(lambda: {
        "rounds": 0, "won": 0,
        "total_score": 0, "total_dist": 0,
        "total_time": 0.0, "time_count": 0,
        "buckets": defaultdict(int),
        "scores": [],
    })
    for r in rounds:
        c = r["actual_country"]
        s = raw[c]
        s["rounds"]      += 1
        s["won"]         += 1 if r["round_won"] else 0
        s["total_score"] += r["score"]
        s["total_dist"]  += r["dist_km"]
        s["scores"].append(r["score"])
        if r["time_sec"] is not None:
            s["total_time"] += r["time_sec"]
            s["time_count"] += 1
        s["buckets"][score_bucket(r["score"])] += 1

    result = {}
    for country, s in raw.items():
        n      = s["rounds"]
        scores = sorted(s["scores"])
        median = scores[n // 2]
        country_pct = round(sum(1 for sc in scores if sc >= 4500) / n * 100, 1)
        result[country] = {
            "rounds":      n,
            "won":         s["won"],
            "avg_score":   round(s["total_score"] / n),
            "median":      median,
            "avg_dist":    round(s["total_dist"] / n, 1),
            "avg_time":    round(s["total_time"] / s["time_count"], 1) if s["time_count"] else None,
            "country_pct": country_pct,
            "buckets":     dict(s["buckets"]),
        }
    return result


# ── HTML ──────────────────────────────────────────────────────────────────────

def sparkbar(buckets: dict, total: int) -> str:
    order  = ["5k", "4k+", "3k+", "2k+", "<2k"]
    colors = ["#4ade80", "#86efac", "#facc15", "#fb923c", "#f87171"]
    parts  = []
    for label, color in zip(order, colors):
        count = buckets.get(label, 0)
        if not count:
            continue
        pct = count / total * 100
        parts.append(
            f'<div title="{label}: {count}×" '
            f'style="width:{pct:.1f}%;background:{color};height:8px;display:inline-block"></div>'
        )
    return f'<div style="display:flex;width:110px;border-radius:3px;overflow:hidden">{"".join(parts)}</div>'

def wr_color(pct: float) -> str:
    return "#4ade80" if pct >= 70 else "#facc15" if pct >= 45 else "#f87171"

def sc_color(avg: int) -> str:
    return "#4ade80" if avg >= 4200 else "#facc15" if avg >= 3000 else "#f87171"

def make_tab_html(stats: dict, rounds: list[dict]) -> str:
    if not rounds:
        return "<p class='empty'>Нет данных</p>"

    total_r = len(rounds)
    total_w = sum(1 for r in rounds if r["round_won"])
    avg_sc  = round(sum(r["score"] for r in rounds) / total_r)
    avg_d   = round(sum(r["dist_km"] for r in rounds) / total_r, 1)
    wr      = round(total_w / total_r * 100, 1)

    qualified = [(c, s) for c, s in stats.items() if s["rounds"] >= MIN_ROUNDS]

    best_sc  = max(qualified, key=lambda x: x[1]["avg_score"],   default=None)
    worst_sc = min(qualified, key=lambda x: x[1]["avg_score"],   default=None)
    trap     = min(qualified, key=lambda x: x[1]["country_pct"], default=None)
    most     = max(qualified, key=lambda x: x[1]["rounds"],      default=None)

    def card(label, val, color="#38bdf8"):
        return f'<div class="s-card"><div class="sv" style="color:{color}">{val}</div><div class="sl">{label}</div></div>'

    cards = [
        card("Rounds",       total_r),
        card("Avg Score",    f"{avg_sc:,}"),
        card("Avg Distance", f"{avg_d} km"),
        card("Round Win Rate", f"{wr}%", wr_color(wr)),
    ]
    if best_sc:  cards.append(card(f"Best ({best_sc[1]['avg_score']:,})",   best_sc[0],  "#4ade80"))
    if worst_sc: cards.append(card(f"Worst ({worst_sc[1]['avg_score']:,})", worst_sc[0], "#f87171"))
    if trap:     cards.append(card(f"Trap ({trap[1]['country_pct']}%)",     trap[0],     "#fb923c"))
    if most:     cards.append(card(f"Most played ({most[1]['rounds']}r)",   most[0],     "#94a3b8"))

    summary = f'<div class="summary">{"".join(cards)}</div>'

    by_rounds = sorted(qualified, key=lambda x: -x[1]["rounds"])
    rows = []
    for rank, (country, s) in enumerate(by_rounds, 1):
        at = f"{s['avg_time']:.0f}s" if s["avg_time"] is not None else "—"
        rows.append(f"""
        <tr>
          <td class="num dim">{rank}</td>
          <td class="bold">{country}</td>
          <td class="num">{s['rounds']}</td>
          <td class="num"><span style="color:{sc_color(s['avg_score'])};font-weight:700">{s['avg_score']:,}</span></td>
          <td class="num">{s['median']:,}</td>
          <td class="num"><span style="color:{wr_color(s['country_pct'])}">{s['country_pct']}%</span></td>
          <td class="num">{s['avg_dist']} km</td>
          <td class="num">{at}</td>
          <td>{sparkbar(s['buckets'], s['rounds'])}</td>
        </tr>""")

    table = f"""
    <table data-sortable>
      <thead><tr>
        <th class="num">#</th>
        <th>Country</th>
        <th class="num" title="Количество раундов">Rnd</th>
        <th class="num" title="Средний географический скор (0–5000)">Avg Score</th>
        <th class="num" title="Медианный скор">Median</th>
        <th class="num" title="% раундов со скором ≥4500 (прокси: угадал страну)">Country%</th>
        <th class="num" title="Среднее расстояние до правильной точки">Avg Dist</th>
        <th class="num" title="Среднее время гесса">Avg Time</th>
        <th title="Распределение скоров">Distrib.</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>"""

    return summary + table

def generate_html(move_rounds, nomove_rounds, username) -> str:
    move_html   = make_tab_html(compute_stats(move_rounds),   move_rounds)
    nomove_html = make_tab_html(compute_stats(nomove_rounds), nomove_rounds)
    total       = len(move_rounds) + len(nomove_rounds)
    generated   = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GeoGuessr Duels — {username}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;padding:2rem}}
  h1{{font-size:1.7rem;color:#f8fafc;margin-bottom:.2rem}}
  .sub{{color:#475569;font-size:.85rem;margin-bottom:1.75rem}}
  .tabs{{display:flex;gap:.5rem;margin-bottom:1.5rem}}
  .tab{{padding:.6rem 1.6rem;border-radius:8px;border:1px solid #334155;background:#1e293b;color:#94a3b8;cursor:pointer;font-size:.9rem;font-weight:600}}
  .tab.active{{background:#3b82f6;border-color:#3b82f6;color:#fff}}
  .tab-content{{display:none}}
  .tab-content.active{{display:block}}
  .summary{{display:flex;flex-wrap:wrap;gap:.75rem;margin-bottom:1.5rem}}
  .s-card{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:.85rem 1.1rem;text-align:center;min-width:110px}}
  .sv{{font-size:1.3rem;font-weight:700;color:#38bdf8}}
  .sl{{font-size:.72rem;color:#64748b;margin-top:.15rem}}
  table{{width:100%;border-collapse:collapse;font-size:.84rem}}
  th{{text-align:left;padding:.55rem .65rem;background:#1e293b;color:#64748b;font-size:.71rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;cursor:pointer;user-select:none;white-space:nowrap}}
  th:hover{{color:#94a3b8}}
  th.sorted{{color:#38bdf8}}
  th.num,td.num{{text-align:right}}
  td{{padding:.48rem .65rem;border-bottom:1px solid #1e293b55;vertical-align:middle;white-space:nowrap}}
  td.bold{{font-weight:500}}
  td.dim{{color:#475569}}
  tr:hover td{{background:#1e293b66}}
  .empty{{color:#475569;padding:3rem;text-align:center;font-style:italic}}
  .legend{{margin-top:1.5rem;color:#334155;font-size:.75rem;line-height:1.8}}
  @media(max-width:700px){{body{{padding:1rem}}.sv{{font-size:1rem}}}}
</style>
</head>
<body>
<h1>🌍 GeoGuessr Duels</h1>
<p class="sub">@{username} · {total} rounds · {generated}</p>
<div class="tabs">
  <div class="tab active" onclick="switchTab('move',this)">🚶 Move <span style="opacity:.6;font-weight:400">({len(move_rounds)}r)</span></div>
  <div class="tab"        onclick="switchTab('nomove',this)">🧊 NoMove <span style="opacity:.6;font-weight:400">({len(nomove_rounds)}r)</span></div>
</div>
<div id="tab-move"   class="tab-content active">{move_html}</div>
<div id="tab-nomove" class="tab-content">{nomove_html}</div>
<div class="legend">
  Country% — доля раундов со score ≥ 4500 (прокси для «угадал страну»).<br>
  Distribution: <span style="color:#4ade80">■ 5k</span>
  <span style="color:#86efac">■ 4k+</span>
  <span style="color:#facc15">■ 3k+</span>
  <span style="color:#fb923c">■ 2k+</span>
  <span style="color:#f87171">■ &lt;2k</span>
  &nbsp;·&nbsp; Клик на заголовок — сортировка.
</div>
<script>
function switchTab(name,el){{
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  el.classList.add('active');
}}
document.querySelectorAll('table[data-sortable]').forEach(table=>{{
  const ths=table.querySelectorAll('th');
  let lastCol=2,asc=false;
  ths.forEach((th,col)=>{{
    th.addEventListener('click',()=>{{
      asc=lastCol===col?!asc:false; lastCol=col;
      ths.forEach(h=>{{h.classList.remove('sorted');h.textContent=h.textContent.replace(/ [↑↓]$/,'');}});
      th.classList.add('sorted'); th.textContent+=(asc?' ↑':' ↓');
      const tbody=table.querySelector('tbody');
      [...tbody.querySelectorAll('tr')].sort((a,b)=>{{
        const av=a.cells[col]?.textContent.trim().replace(/[,% sk+<]/gi,'')||'0';
        const bv=b.cells[col]?.textContent.trim().replace(/[,% sk+<]/gi,'')||'0';
        const an=parseFloat(av)||0,bn=parseFloat(bv)||0;
        if(an!==bn) return asc?an-bn:bn-an;
        return asc?av.localeCompare(bv):bv.localeCompare(av);
      }}).forEach(r=>tbody.appendChild(r));
    }});
  }});
}});
</script>
</body>
</html>"""


# ── Кэш ──────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global NCFA_COOKIE
    if not NCFA_COOKIE:
        print("DevTools → Application → Cookies → geoguessr.com → _ncfa")
        NCFA_COOKIE = input("_ncfa: ").strip()

    session = make_session(NCFA_COOKIE)

    print("🔐 Авторизация...")
    try:
        my_id, username = get_profile(session)
        print(f"✅ {username}  (id: {my_id})")
    except Exception as e:
        sys.exit(f"❌ {e}")

    print(f"\n📥 Ищу дуэли в ленте (до {MAX_DUELS})...")
    duel_ids = get_duel_ids_from_feed(session, MAX_DUELS)
    print(f"✅ {len(duel_ids)} дуэлей найдено")

    if not duel_ids:
        sys.exit("❌ Дуэлей не найдено.")

    cache = load_cache()
    all_rounds: list[dict] = []
    new_fetched = 0

    print("\n🎮 Загружаю данные дуэлей...")
    for i, gid in enumerate(duel_ids):
        if gid in cache:
            rounds = cache[gid]
        else:
            duel   = get_duel(gid, session)
            rounds = parse_duel(duel, my_id) if duel else None
            cache[gid] = rounds
            new_fetched += 1
            if new_fetched % 25 == 0:
                save_cache(cache)
                print(f"  [{i+1}/{len(duel_ids)}] кэш сохранён...")
            time.sleep(REQUEST_DELAY)

        if rounds:
            all_rounds.extend(rounds)

    save_cache(cache)

    if not all_rounds:
        sys.exit("❌ Нет раундов. Проверь _ncfa куку.")

    move_rounds   = [r for r in all_rounds if r["game_mode"] == "Move"]
    nomove_rounds = [r for r in all_rounds if r["game_mode"] == "NoMove"]

    print(f"\n✅ {len(all_rounds)} раундов: {len(move_rounds)} Move, {len(nomove_rounds)} NoMove")
    print(f"   (кэш: {len(duel_ids)-new_fetched} hit, новых: {new_fetched})")

    print(f"📝 Генерирую {OUTPUT_HTML}...")
    html = generate_html(move_rounds, nomove_rounds, username)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print("\n" + "─" * 45)
    for label, rounds in [("Move", move_rounds), ("NoMove", nomove_rounds)]:
        if not rounds: continue
        avg = round(sum(r["score"] for r in rounds) / len(rounds))
        wr  = round(sum(1 for r in rounds if r["round_won"]) / len(rounds) * 100, 1)
        print(f"{label:8}: {len(rounds):4} раундов  avg {avg:,}  win rate {wr}%")

    print(f"\n✅ Открой: {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
