# --- dependency bootstrap (to avoid ModuleNotFoundError in bare environments) ---
def _ensure_dependencies():
    import importlib
    missing = []
    for mod in ["requests", "bs4", "playwright", "google-genai"]:
        try:
            if mod == "google-genai":
                importlib.import_module("google.genai")
            else:
                importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)

_ensure_dependencies()
# ------------------------------------------------------------------------------

import json, os, re, urllib.request
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from google import genai  # pip package: google-genai

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_GENAI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

SPORTYBET_API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"
CHAMPIONBET_API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
BETIKA_API = "https://api-ug.betika.com/v1/uo/matches?page=1&limit=10&tab=&sub_type_id=1,186,340&sport_id=3&sort_id=1&period_id=-1&esports=false"

HISTORY_FILE = "arbitrage_history.json"
FRESHNESS_FILE = "odds_freshness.json"

STAKE = 100000
STALE_ODDS_HOURS = 1

FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
FOOTBALL_API_BASE = "https://api.football-data.org/v4"
MAX_MATCH_AGE_HOURS = 24
INVALID_RETENTION_HOURS = 24

BAD_TEAM_NAMES = {"home", "away", "team a", "team b", "tbd", "unknown", "—", "-"}


def normalize(name):
    name = (name or "").lower().strip()
    name = re.sub(r"\b(fc|sc|cf|ac|united|city|sports|club|utd|football|soccer|women|men|u21|u23)\b", "", name)
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


def is_bad_team_name(name):
    if not name:
        return True
    n = normalize(name)
    return n in BAD_TEAM_NAMES or len(n) <= 1


def teams_match(name1, name2):
    n1, n2 = normalize(name1), normalize(name2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    if len(n1) > 3 and len(n2) > 3:
        if n1 in n2 or n2 in n1:
            return True
        w1 = n1.split()[0] if n1.split() else ""
        w2 = n2.split()[0] if n2.split() else ""
        if len(w1) > 4 and w1 == w2:
            return True
    return False


def match_key_similarity(key1, key2):
    p1, p2 = key1.split(" vs "), key2.split(" vs ")
    if len(p1) != 2 or len(p2) != 2:
        return False
    return teams_match(p1[0], p2[0]) and teams_match(p1[1], p2[1])


def clean_odd(v, min_odd=1.01, max_odd=50.0):
    try:
        if v is None:
            return None
        v = float(v)
        if min_odd <= v <= max_odd:
            return v
    except Exception:
        pass
    return None


def build_match_record(home_team, away_team, bookmaker, home, draw, away, sport="Football", competition=""):
    return {
        "match": f"{home_team} vs {away_team}",
        "home_team": home_team,
        "away_team": away_team,
        "match_key": f"{normalize(home_team)} vs {normalize(away_team)}",
        "bookmaker": bookmaker,
        "competition": competition,
        "home": home,
        "draw": draw,
        "away": away,
        "sport": sport,
    }


FINISHED_STATUSES = {"FINISHED", "FULL_TIME", "FT", "ENDED", "CANCELLED", "CANCELED", "POSTPONED", "SUSPENDED", "ABANDONED"}
LIVE_INDICATORS = {"LIVE", "IN_PLAY", "IN-PLAY", "PLAYING", "1ST HALF", "2ND HALF", "HT", "HALF TIME"}


def is_finished_status(value):
    if not value:
        return False
    v = str(value).upper()
    return any(x in v for x in FINISHED_STATUSES)


def is_live_status(value):
    if not value:
        return False
    v = str(value).upper()
    return any(x in v for x in LIVE_INDICATORS)


def has_live_score_fields(m):
    if not isinstance(m, dict):
        return False
    for key in ("SC", "S1", "S2", "CS", "score", "current_score", "live_score", "SetScore"):
        val = m.get(key)
        if val not in (None, "", 0, "0", "0-0", "0:0"):
            return True
    return False


def is_today_utc(dt):
    if not isinstance(dt, datetime):
        return True
    today = datetime.now(timezone.utc).date()
    return dt.astimezone(timezone.utc).date() == today


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def load_freshness():
    if os.path.exists(FRESHNESS_FILE):
        try:
            with open(FRESHNESS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_freshness(freshness):
    with open(FRESHNESS_FILE, "w") as f:
        json.dump(freshness, f, indent=2)


def filter_stale_odds(all_odds):
    previous = load_freshness()
    new_freshness, fresh_odds = {}, {}
    now = datetime.now(timezone.utc)

    for o in all_odds:
        key = f"{o.get('bookmaker')}|{o.get('match_key')}"
        snapshot = {"home": o.get("home"), "draw": o.get("draw"), "away": o.get("away")}
        old = previous.get(key)

        if not old:
            since_str = now.strftime("%Y-%m-%d %H:%M UTC")
            o["since"] = since_str
            new_freshness[key] = {"snapshot": snapshot, "since": since_str}
            fresh_odds[o.get("match_key")] = o
            continue

        old_snapshot = old.get("snapshot", {})
        since_str = old.get("since")

        if snapshot != old_snapshot:
            since_str = now.strftime("%Y-%m-%d %H:%M UTC")
            o["since"] = since_str
            new_freshness[key] = {"snapshot": snapshot, "since": since_str}
            fresh_odds[o.get("match_key")] = o
            continue

        try:
            since_dt = datetime.strptime(since_str, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            hours = (now - since_dt).total_seconds() / 3600
        except Exception:
            hours = 0

        if hours <= STALE_ODDS_HOURS:
            o["since"] = since_str
            new_freshness[key] = {"snapshot": snapshot, "since": since_str}
            fresh_odds[o.get("match_key")] = o

    save_freshness(new_freshness)
    return list(fresh_odds.values())


def get_match_status_and_kickoff_football_data(home_team, away_team, date_hint=None):
    if not FOOTBALL_API_KEY:
        return None

    if date_hint is None:
        today = datetime.now(timezone.utc).date()
        date_from = date_to = today.isoformat()
    else:
        date_from = date_to = date_hint

    url = f"{FOOTBALL_API_BASE}/matches"
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    params = {"dateFrom": date_from, "dateTo": date_to}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print("Football-Data.org error:", e)
        return None

    matches = data.get("matches", [])
    if not matches:
        return None

    def _norm_team_obj(tobj):
        return normalize(tobj.get("name", ""))

    target_home, target_away = normalize(home_team), normalize(away_team)

    for m in matches:
        home = m.get("homeTeam", {}) or {}
        away = m.get("awayTeam", {}) or {}
        if _norm_team_obj(home) == target_home and _norm_team_obj(away) == target_away:
            status = m.get("status", "").upper()
            kickoff_raw = m.get("utcDate")
            kickoff_dt = None
            if kickoff_raw:
                try:
                    kickoff_dt = datetime.fromisoformat(kickoff_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
                except Exception:
                    kickoff_dt = None
            return {"status": status, "kickoff": kickoff_dt}

    return None


def filter_opportunities_with_football_data(opps_list):
    if not FOOTBALL_API_KEY:
        return opps_list

    cleaned = []
    now = datetime.now(timezone.utc)

    for opp in opps_list:
        match_name = opp.get("match", "")
        parts = match_name.split(" vs ")
        home_team = parts[0].strip() if len(parts) == 2 else match_name
        away_team = parts[1].strip() if len(parts) == 2 else ""

        info = get_match_status_and_kickoff_football_data(home_team, away_team)
        if not info:
            cleaned.append(opp)
            continue

        status, kickoff = info["status"], info["kickoff"]

        if status in {"FINISHED", "CANCELLED", "POSTPONED", "SUSPENDED", "AWARDED"}:
            continue

        if kickoff and (now - kickoff) > timedelta(hours=MAX_MATCH_AGE_HOURS):
            continue

        cleaned.append(opp)

    return cleaned


def scrape_with_gemini(url, bookmaker_name, sport="Football"):
    odds = []
    if not gemini_client:
        print("Gemini API key not configured, skipping Gemini scrape.")
        return odds

    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        html = resp.text

        prompt = f"""
You are helping extract pre-match betting odds for {bookmaker_name} in Uganda.

SUPPORTED SPORTS:
- Football (soccer) 1X2 market (home / draw / away)
- Rugby main match winner market (home / away; usually 2-way, no draw)
- Basketball main match winner market (home / away; 2-way)
- Other sports: if clearly shown with home/draw/away or home/away odds, you may include them and set "sport" accordingly.

TASK:
From the HTML below, find all pre-match events and return ONLY valid JSON, no explanations.

Return this exact format:

{{
  "matches": [
    {{
      "home_team": "Team A",
      "away_team": "Team B",
      "home_odd": 2.10,
      "draw_odd": 3.00,        // null if not offered (e.g., rugby, basketball)
      "away_odd": 2.50,
      "competition": "League or competition name (if available)",
      "sport": "Football"      // or "Rugby", "Basketball", "Tennis", etc.
    }}
  ]
}}

RULES:
- Include only PRE-MATCH odds, not live matches.
- Ignore virtuals and casino games.
- If the market is clearly 2-way (no draw), set "draw_odd": null.
- If sport is not obvious, skip that event.

HTML:
{html}
"""

        response = gemini_client.models.generate_content(
            model="gemini-1.5-flash-latest",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
        )

        text = ""
        if getattr(response, "candidates", None):
            parts = response.candidates[0].content.parts or []
            text = "".join(getattr(p, "text", "") or "" for p in parts)
        else:
            text = getattr(response, "text", "") or ""

        json_start = text.find("{")
        if json_start > 0:
            text = text[json_start:]

        data = json.loads(text)
        matches = data.get("matches", []) if isinstance(data, dict) else []

        for m in matches:
            try:
                home_team, away_team = m.get("home_team"), m.get("away_team")
                if not home_team or not away_team:
                    continue
                if is_bad_team_name(home_team) or is_bad_team_name(away_team):
                    continue

                h = clean_odd(m.get("home_odd"))
                d = clean_odd(m.get("draw_odd"))
                a = clean_odd(m.get("away_odd"))
                if h is None or a is None:
                    continue

                competition = m.get("competition", "") or ""
                sport_name = m.get("sport", sport) or sport

                odds.append(
                    build_match_record(
                        home_team,
                        away_team,
                        bookmaker_name,
                        h,
                        d,
                        a,
                        sport_name,
                        competition,
                    )
                )
            except Exception:
                continue

    except Exception as e:
        print(f"Gemini scrape error for {bookmaker_name}: {e}")

    return odds


def scrape_ababet_gemini():
    return scrape_with_gemini("https://www.ababet.ug/", "AbaBet-Gemini", sport="Football")


# --- AI-assisted scrapers for additional Ugandan bookmakers (you can comment out any you don't want) ---

def scrape_betpawa_gemini():
    return scrape_with_gemini("https://www.betpawa.ug/", "BetPawa-Gemini", sport="Football")


def scrape_fortebet_gemini():
    return scrape_with_gemini("https://www.fortebet.ug/", "ForteBet-Gemini", sport="Football")


def scrape_gsb_gemini():
    return scrape_with_gemini("https://www.gsbu.ug/", "GSB-Gemini", sport="Football")


def scrape_betway_gemini():
    return scrape_with_gemini("https://www.betway.ug/", "Betway-Gemini", sport="Football")


def scrape_betwinner_gemini():
    return scrape_with_gemini("https://www.betwinner.co.ug/", "BetWinner-Gemini", sport="Football")


def scrape_premierbet_gemini():
    return scrape_with_gemini("https://www.premierbetuganda.com/", "PremierBet-Gemini", sport="Football")


def scrape_bungabet_gemini():
    return scrape_with_gemini("https://www.bungabet.ug/", "BungaBet-Gemini", sport="Football")


def scrape_bongobongo_gemini():
    return scrape_with_gemini("https://www.bongobongo.ug/", "BongoBongo-Gemini", sport="Football")


def scrape_mozzartbet_gemini():
    return scrape_with_gemini("https://www.mozzartbet.ug/", "MozzartBet-Gemini", sport="Football")


def scrape_betin_gemini():
    return scrape_with_gemini("https://www.betin.co.ug/", "Betin-Gemini", sport="Football")


def scrape_kagwirawo_gemini():
    return scrape_with_gemini("https://www.kagwirawo.ug/", "Kagwirawo-Gemini", sport="Football")


def scrape_sportpesa_gemini():
    return scrape_with_gemini("https://www.sportpesa.co.ug/", "SportPesa-Gemini", sport="Football")


def scrape_jackpotbet_gemini():
    return scrape_with_gemini("https://www.jackpotbet.ug/", "JackpotBet-Gemini", sport="Football")


def scrape_betlion_gemini():
    return scrape_with_gemini("https://www.betlion.ug/", "BetLion-Gemini", sport="Football")


def scrape_mbet_gemini():
    return scrape_with_gemini("https://www.mbet.ug/", "Mbet-Gemini", sport="Football")


def scrape_paripesa_gemini():
    return scrape_with_gemini("https://www.paripesa.ug/", "Paripesa-Gemini", sport="Football")


def scrape_linebet_gemini():
    return scrape_with_gemini("https://www.linebet.ug/", "LineBet-Gemini", sport="Football")


def scrape_betsofa_gemini():
    return scrape_with_gemini("https://www.betsofa.ug/", "Betsofa-Gemini", sport="Football")


def scrape_betwinner360_gemini():
    return scrape_with_gemini("https://www.betwinner360.ug/", "Betwinner360-Gemini", sport="Football")


def scrape_odibets_gemini():
    return scrape_with_gemini("https://www.odibets.ug/", "OdiBets-Gemini", sport="Football")


def scrape_thunderbet_gemini():
    return scrape_with_gemini("https://www.thunderbet.ug/", "ThunderBet-Gemini", sport="Football")


def scrape_topbet_gemini():
    return scrape_with_gemini("https://www.topbet.ug/", "TopBet-Gemini", sport="Football")


# ----------------------------------------------------------------------


def championbet_extract_1x2(match):
    bet_map = match.get("betMap", {}) or {}

    def pick_odd(market_keys):
        for k in market_keys:
            market = bet_map.get(str(k), {}) or {}
            if not isinstance(market, dict):
                continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None:
                        return odd
        return None

    return pick_odd([1, 4, 7]), pick_odd([2, 5, 8]), pick_odd([3, 6, 9])


def scrape_championbet():
    odds = []
    try:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Referer": "https://www.championbet.ug/mob/",
        }
        req = urllib.request.Request(CHAMPIONBET_API, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        matches = data.get("esMatches", []) if isinstance(data, dict) else []

        for m in matches:
            try:
                sport_token = str(
                    m.get("sportToken", "")
                    or m.get("sport", "")
                    or m.get("sportName", "")
                    or m.get("sport_name", "")
                )
                if "soccer" not in sport_token.lower() and "football" not in sport_token.lower():
                    continue

                status = (m.get("status") or m.get("matchStatus") or "").upper()
                if is_finished_status(status) or is_live_status(status) or has_live_score_fields(m):
                    continue

                start_raw = m.get("startTime") or m.get("startDate")
                if start_raw:
                    try:
                        dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
                        if not is_today_utc(dt):
                            continue
                    except Exception:
                        pass

                home_team = m.get("home") or m.get("homeTeam") or m.get("home_team") or m.get("team1") or ""
                away_team = m.get("away") or m.get("awayTeam") or m.get("away_team") or m.get("team2") or ""
                if not home_team or not away_team:
                    continue
                if is_bad_team_name(home_team) or is_bad_team_name(away_team):
                    continue

                h, d, a = championbet_extract_1x2(m)
                if h is not None and a is not None:
                    odds.append(
                        build_match_record(
                            home_team,
                            away_team,
                            "ChampionBet",
                            h,
                            d,
                            a,
                            "Football",
                            m.get("leagueName", "") or m.get("competition", "") or "",
                        )
                    )
            except Exception:
                continue

    except Exception as e:
        print(f"ChampionBet error: {e}")

    return odds


def scrape_betika():
    odds = []
    try:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Referer": "https://www.betika.com/",
            "Origin": "https://www.betika.com",
        }
        req = urllib.request.Request(BETIKA_API, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        matches = data.get("data", []) if isinstance(data, dict) else []

        for m in matches:
            try:
                status = (m.get("match_status") or m.get("status") or "").upper()
                if is_finished_status(status) or is_live_status(status) or has_live_score_fields(m):
                    continue

                home_team = m.get("home_team", "")
                away_team = m.get("away_team", "")
                if not home_team or not away_team:
                    continue
                if is_bad_team_name(home_team) or is_bad_team_name(away_team):
                    continue

                h = clean_odd(m.get("home_odd"))
                d = clean_odd(m.get("neutral_odd"))
                a = clean_odd(m.get("away_odd"))
                if h is not None and a is not None:
                    odds.append(
                        build_match_record(
                            home_team,
                            away_team,
                            "Betika",
                            h,
                            d,
                            a,
                            m.get("sport_name", "Football"),
                            m.get("competition_name", ""),
                        )
                    )
            except Exception:
                continue

    except Exception as e:
        print(f"Betika error: {e}")

    return odds


def scrape_ababet():
    odds = []
    try:
        r = requests.get(
            "https://www.ababet.ug/soccer/match_result?mobile=1",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for table in soup.find_all("table"):
            first_row = table.find("tr")
            if not first_row:
                continue
            headers_row = [c.get_text(" ", strip=True) for c in first_row.find_all(["th", "td"])]
            if "Home" not in headers_row or "Away" not in headers_row:
                continue

            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 5:
                    continue

                row = dict(zip(headers_row, cells[: len(headers_row)]))
                home_team = row.get("Home")
                away_team = row.get("Away")
                status_cell = (row.get("Status") or row.get("Score") or "").upper()

                if is_finished_status(status_cell) or is_live_status(status_cell):
                    continue
                if not home_team or not away_team:
                    continue
                if is_bad_team_name(home_team) or is_bad_team_name(away_team):
                    continue

                odds.append(
                    build_match_record(
                        home_team,
                        away_team,
                        "AbaBet",
                        row.get("1"),
                        row.get("X"),
                        row.get("2"),
                        "Football",
                        row.get("League", ""),
                    )
                )

    except Exception as e:
        print(f"AbaBet error: {e}")

    return odds


def scrape_betpawa():
    odds, seen_matches = [], set()
    urls = [
        "https://www.betpawa.ug/events?categoryId=2&marketId=1X2",
        "https://www.betpawa.ug/events/popular",
    ]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 12; Samsung Galaxy) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
                viewport={"width": 390, "height": 844},
                locale="en-UG",
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            for url in urls:
                try:
                    page.goto(url, timeout=60000)
                    page.wait_for_timeout(6000)

                    for link in page.query_selector_all('a[href*="/event/"], a[href*="/match/"]')[:60]:
                        try:
                            text = link.inner_text()
                            if "LIVE" in text.upper() or re.search(r"\b\d{1,3}:\d{2}\b", text):
                                continue

                            parts = [p.strip() for p in text.split("\n") if p.strip()]
                            teams, odd_values, competition = [], [], ""

                            for part in parts:
                                if re.match(r"^\d+\.\d+$", part):
                                    try:
                                        odd_values.append(float(part))
                                    except Exception:
                                        continue
                                elif len(part) > 2 and not any(
                                    x in part for x in ["Football", "Soccer", "Netball", "Tennis", "Basketball"]
                                ):
                                    teams.append(part)
                                elif any(
                                    x in part for x in ["Football", "Soccer", "Netball", "Tennis", "Basketball"]
                                ):
                                    competition = part

                            if len(teams) >= 2 and len(odd_values) >= 2:
                                home_team, away_team = teams[0], teams[1]
                                if is_bad_team_name(home_team) or is_bad_team_name(away_team):
                                    continue

                                mk = f"{home_team}vs{away_team}".lower().replace(" ", "")
                                if mk not in seen_matches:
                                    seen_matches.add(mk)
                                    odds.append(
                                        build_match_record(
                                            home_team,
                                            away_team,
                                            "BetPawa",
                                            odd_values[0],
                                            odd_values[1] if len(odd_values) >= 3 else None,
                                            odd_values[2] if len(odd_values) >= 3 else odd_values[1],
                                            "Football",
                                            competition,
                                        )
                                    )
                        except Exception:
                            continue
                except Exception:
                    continue

            browser.close()

    except Exception as e:
        print(f"BetPawa error: {e}")

    return odds


def scrape_fortebet():
    odds = []
    try:
        url = "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://desktop.fortebet.ug/prematch/landing",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        inner = data.get("data", {})
        events, markets, competitors = inner.get("event", {}), inner.get("markets", {}), inner.get("competitors", {})

        event_markets = {}
        for _, market in markets.items():
            eid = str(market.get("eventId", ""))
            event_markets.setdefault(eid, []).append(market)

        for eid, event in events.items():
            try:
                status = (event.get("status") or event.get("eventStatus") or "").upper()
                if is_finished_status(status) or is_live_status(status) or has_live_score_fields(event):
                    continue

                comp_ids = event.get("competitors", [])
                if len(comp_ids) < 2:
                    continue

                home_team = competitors.get(str(comp_ids[0]), {}).get("name", "")
                away_team = competitors.get(str(comp_ids[1]), {}).get("name", "")
                if not home_team or not away_team:
                    continue
                if is_bad_team_name(home_team) or is_bad_team_name(away_team):
                    continue

                h = d = a = None
                for market in event_markets.get(eid, []):
                    if market.get("marketId") == 1:
                        odds_map = market.get("odds", {})
                        odd_list = []
                        for _, v in odds_map.items():
                            if isinstance(v, dict) and "odds" in v:
                                odd_list.append((v.get("outcomeId", 0), clean_odd(v["odds"])))
                        odd_list = [(i, o) for i, o in odd_list if o is not None]
                        odd_list.sort(key=lambda x: x[0])
                        if len(odd_list) >= 3:
                            h, d, a = odd_list[0][1], odd_list[1][1], odd_list[2][1]
                        elif len(odd_list) == 2:
                            h, a = odd_list[0][1], odd_list[1][1]
                        break

                if h is not None and a is not None:
                    odds.append(build_match_record(home_team, away_team, "Fortebet", h, d, a, "Football"))

            except Exception:
                continue

    except Exception as e:
        print(f"Fortebet error: {e}")

    return odds


def scrape_sportybet():
    odds = []
    try:
        req = urllib.request.Request(
            SPORTYBET_API,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        if isinstance(data, list):
            print(f"SPORTYBET SAMPLE: {data[0] if data else 'none'}")

            for event in data:
                try:
                    status = (event.get("status") or event.get("state") or "").upper()
                    if is_finished_status(status) or is_live_status(status) or has_live_score_fields(event):
                        continue

                    home = event.get("home_team", "")
                    away = event.get("away_team", "")
                    if not home or not away:
                        continue
                    if is_bad_team_name(home) or is_bad_team_name(away):
                        continue

                    h = clean_odd(event.get("home_odd"))
                    d = clean_odd(event.get("draw_odd"))
                    a = clean_odd(event.get("away_odd"))
                    if h is not None and a is not None:
                        odds.append(
                            build_match_record(
                                home,
                                away,
                                "SportyBet",
                                h,
                                d,
                                a,
                                event.get("sport", "Football"),
                                event.get("competition", ""),
                            )
                        )
                except Exception:
                    continue

    except Exception as e:
        print(f"SportyBet error: {e}")

    return odds


def scrape_1xbet():
    odds = []
    try:
        url = "https://1xbet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=135&getEmpty=true&virtualSports=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = json.loads(raw.decode("utf-8-sig"))

        vals = data.get("Value", []) if isinstance(data, dict) else []
        print(f"1XBET SAMPLE: {vals[0] if vals else 'none'}")

        for match in vals:
            try:
                status = (match.get("SC") or match.get("STAT") or "").upper()
                if is_finished_status(status) or is_live_status(status) or has_live_score_fields(match):
                    continue

                home_team, away_team = match.get("O1"), match.get("O2")
                if not home_team or not away_team:
                    continue
                if is_bad_team_name(home_team) or is_bad_team_name(away_team):
                    continue

                h = d = a = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        h = c
                    elif t == "2":
                        a = c
                    elif t == "3":
                        d = c

                if h is not None and a is not None:
                    odds.append(build_match_record(home_team, away_team, "1xBet", h, d, a, "Football"))

            except Exception:
                continue

    except Exception as e:
        print(f"1xBet error: {e}")

    return odds


def scrape_22bet():
    odds = []
    try:
        url = "https://22bet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=151&getEmpty=true&virtualSports=true"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8-sig"))

        vals = data.get("Value", []) if isinstance(data, dict) else []
        print(f"22BET SAMPLE: {vals[0] if vals else 'none'}")

        for match in vals:
            try:
                status = (match.get("SC") or match.get("STAT") or "").upper()
                if is_finished_status(status) or is_live_status(status) or has_live_score_fields(match):
                    continue

                home_team, away_team = match.get("O1"), match.get("O2")
                if not home_team or not away_team:
                    continue
                if is_bad_team_name(home_team) or is_bad_team_name(away_team):
                    continue

                h = d = a = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        h = c
                    elif t == "2":
                        a = c
                    elif t == "3":
                        d = c

                if h is not None and a is not None:
                    odds.append(build_match_record(home_team, away_team, "22Bet", h, d, a, "Football"))

            except Exception:
                continue

    except Exception as e:
        print(f"22Bet error: {e}")

    return odds


def scrape_melbet():
    odds = []
    try:
        url = "https://melbet-046935.top/service-api/LineFeed/Get1x2_VZip?count=1000&lng=en&mode=4&country=191&partner=8&getEmpty=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-mobile-project-id": "0",
            "x-requested-with": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8-sig"))

        for match in data.get("Value", []) if isinstance(data, dict) else []:
            try:
                status = (match.get("SC") or match.get("STAT") or "").upper()
                if is_finished_status(status) or is_live_status(status) or has_live_score_fields(match):
                    continue

                home_team, away_team = match.get("O1"), match.get("O2")
                if not home_team or not away_team:
                    continue
                if is_bad_team_name(home_team) or is_bad_team_name(away_team):
                    continue

                h = d = a = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None:
                        continue
                    if t == "1":
                        h = c
                    elif t == "2":
                        a = c
                    elif t == "3":
                        d = c

                if h is not None and a is not None:
                    odds.append(build_match_record(home_team, away_team, "Melbet", h, d, a, "Football"))

            except Exception:
                continue

    except Exception as e:
        print(f"Melbet error: {e}")

    return odds


def find_arbitrage(all_odds):
    opportunities, sports_odds = [], {}
    for odd in all_odds:
        sports_odds.setdefault(odd.get("sport", "Football"), []).append(odd)

    for sport, sport_odds in sports_odds.items():
        exact_groups = {}
        for odd in sport_odds:
            exact_groups.setdefault(odd.get("match_key", ""), []).append(odd)

        merged_groups, processed = {}, set()
        keys = list(exact_groups.keys())

        for i, key1 in enumerate(keys):
            if key1 in processed:
                continue
            group = list(exact_groups[key1])
            processed.add(key1)
            for key2 in keys[i + 1:]:
                if key2 in processed:
                    continue
                if match_key_similarity(key1, key2):
                    group.extend(exact_groups[key2])
                    processed.add(key2)
            merged_groups[key1] = group

        for match_name, bookmakers in merged_groups.items():
            bookie_names = set(b["bookmaker"] for b in bookmakers)
            if len(bookie_names) < 2:
                continue

            bk_odds = {}
            for b in bookmakers:
                bk = b["bookmaker"]
                bk_odds.setdefault(bk, {"home": 0.0, "draw": 0.0, "away": 0.0})
                h, d, a = clean_odd(b.get("home")), clean_odd(b.get("draw")), clean_odd(b.get("away"))
                if h is not None and h > bk_odds[bk]["home"]:
                    bk_odds[bk]["home"] = h
                if d is not None and d > bk_odds[bk]["draw"]:
                    bk_odds[bk]["draw"] = d
                if a is not None and a > bk_odds[bk]["away"]:
                    bk_odds[bk]["away"] = a

            bk_list = list(bk_odds.keys())

            # Football / futsal: 3-way (home/draw/away). Others: 2-way (home/away).
            if sport in ["Football", "Futsal"]:
                best = None
                for bk_h in bk_list:
                    for bk_d in bk_list:
                        for bk_a in bk_list:
                            if len({bk_h, bk_d, bk_a}) < 3:
                                continue
                            h, d, a = bk_odds[bk_h]["home"], bk_odds[bk_d]["draw"], bk_odds[bk_a]["away"]
                            if not h or not d or not a:
                                continue
                            arb = (1 / h) + (1 / d) + (1 / a)
                            if arb < 1:
                                profit = round((1 - arb) * 100, 2)
                                if 1.0 <= profit <= 50.0 and (best is None or profit > best["profit_percent"]):
                                    stake_h = round(STAKE * (1 / h) / arb)
                                    stake_d = round(STAKE * (1 / d) / arb)
                                    stake_a = round(STAKE * (1 / a) / arb)
                                    best = {
                                        "match": match_name,
                                        "sport": sport,
                                        "type": "3-way",
                                        "profit_percent": profit,
                                        "profit_ugx": round(STAKE * (1 - arb)),
                                        "total_stake": STAKE,
                                        "arb_sum": round(arb, 4),
                                        "bets": [
                                            {
                                                "bookmaker": bk_h,
                                                "outcome": "Home",
                                                "selection": "Home",
                                                "odd": h,
                                                "stake": stake_h,
                                                "win": round(stake_h * h),
                                            },
                                            {
                                                "bookmaker": bk_d,
                                                "outcome": "Draw",
                                                "selection": "Draw",
                                                "odd": d,
                                                "stake": stake_d,
                                                "win": round(stake_d * d),
                                            },
                                            {
                                                "bookmaker": bk_a,
                                                "outcome": "Away",
                                                "selection": "Away",
                                                "odd": a,
                                                "stake": stake_a,
                                                "win": round(stake_a * a),
                                            },
                                        ],
                                    }
                if best:
                    opportunities.append(best)
            else:
                best = None
                for bk_h in bk_list:
                    for bk_a in bk_list:
                        if bk_h == bk_a:
                            continue
                        h, a = bk_odds[bk_h]["home"], bk_odds[bk_a]["away"]
                        if not h or not a:
                            continue
                        arb = (1 / h) + (1 / a)
                        if arb < 1:
                            profit = round((1 - arb) * 100, 2)
                            if 1.0 <= profit <= 50.0 and (best is None or profit > best["profit_percent"]):
                                stake_h = round(STAKE * (1 / h) / arb)
                                stake_a = round(STAKE * (1 / a) / arb)
                                best = {
                                    "match": match_name,
                                    "sport": sport,
                                    "type": "2-way",
                                    "profit_percent": profit,
                                    "profit_ugx": round(STAKE * (1 - arb)),
                                    "total_stake": STAKE,
                                    "arb_sum": round(arb, 4),
                                    "bets": [
                                        {
                                            "bookmaker": bk_h,
                                            "outcome": "Home",
                                            "selection": "Home",
                                            "odd": h,
                                            "stake": stake_h,
                                            "win": round(stake_h * h),
                                        },
                                        {
                                            "bookmaker": bk_a,
                                            "outcome": "Away",
                                            "selection": "Away",
                                            "odd": a,
                                            "stake": stake_a,
                                            "win": round(stake_a * a),
                                        },
                                    ],
                                }
                if best:
                    opportunities.append(best)

    return opportunities


def load_arbitrage_history_list():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return list(data.values())
        except Exception:
            pass
    return []


def save_arbitrage_history_list(history_list):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_list, f, indent=2)


def write_dashboard_feed(opportunities, all_odds):
    history = load_arbitrage_history_list()
    now_iso = datetime.now(timezone.utc).isoformat()

    for opp in opportunities:
        entry = dict(opp)
        entry["_seen_at"] = now_iso
        history.append(entry)

    MAX_HISTORY = 5000
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    save_arbitrage_history_list(history)

    bookmakers = sorted({o.get("bookmaker") for o in all_odds if o.get("bookmaker")})

    data = {
        "last_updated": now_iso,
        "stake": STAKE,
        "opportunities": opportunities,
        "raw_odds": all_odds,
        "history": history,
        "total_matches": len(all_odds),
        "bookmakers": bookmakers,
    }

    with open("odds.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def main():
    scrapers = [
        # Existing direct/API scrapers
        scrape_championbet,
        scrape_betika,
        scrape_ababet,
        scrape_betpawa,
        scrape_fortebet,
        scrape_sportybet,
        scrape_1xbet,
        scrape_22bet,
        scrape_melbet,

        # AI-assisted scrapers (Gemini)
        scrape_ababet_gemini,
        scrape_betpawa_gemini,
        scrape_fortebet_gemini,
        scrape_gsb_gemini,
        scrape_betway_gemini,
        scrape_betwinner_gemini,
        scrape_premierbet_gemini,
        scrape_bungabet_gemini,
        scrape_bongobongo_gemini,
        scrape_mozzartbet_gemini,
        scrape_betin_gemini,
        scrape_kagwirawo_gemini,
        scrape_sportpesa_gemini,
        scrape_jackpotbet_gemini,
        scrape_betlion_gemini,
        scrape_mbet_gemini,
        scrape_paripesa_gemini,
        scrape_linebet_gemini,
        scrape_betsofa_gemini,
        scrape_betwinner360_gemini,
        scrape_odibets_gemini,
        scrape_thunderbet_gemini,
        scrape_topbet_gemini,
    ]

    all_odds = []
    for scraper in scrapers:
        try:
            print("Running", scraper.__name__)
            result = scraper()
            print(scraper.__name__, "found", len(result), "matches")
            all_odds.extend(result)
        except Exception as e:
            print(scraper.__name__, "failed:", e)

    all_odds = filter_stale_odds(all_odds)
    opportunities = find_arbitrage(all_odds)
    opportunities = filter_opportunities_with_football_data(opportunities)

    print("Total fresh odds:", len(all_odds), "current arbs:", len(opportunities))

    write_dashboard_feed(opportunities, all_odds)


if __name__ == "__main__":
    main()
