# --- dependency bootstrap ---
def _ensure_dependencies():
    import importlib
    missing = []
    for mod in ["reque", "bs4", "playwright", "google-genai"]:
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

import json
import os
import re
import urllib.request
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from google import genai

# -----------------------------
# Config, constants, clients
# -----------------------------

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

BAD_TEAM_NAMES = {"home", "away", "team a", "team b", "tbd", "unknown", "—", "-"}

FINISHED_STATUSES = {"FINISHED", "FULL_TIME", "FT", "ENDED", "CANCELLED", "CANCELED", "POSTPONED", "SUSPENDED", "ABANDONED"}
LIVE_INDICATORS = {"LIVE", "IN_PLAY", "IN-PLAY", "PLAYING", "1ST HALF", "2ND HALF", "HT", "HALF TIME"}


# -----------------------------
# Name / odds helpers
# -----------------------------

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


# -----------------------------
# Status / freshness helpers
# -----------------------------

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


# -----------------------------
# Football-data.org helpers
# -----------------------------

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


# -----------------------------
# Gemini 2.0 Flash scraper
# -----------------------------

def scrape_with_gemini(url, bookmaker_name, sport="Football"):
    """
    Scrape pre-match odds using Gemini 2.0 Flash with stricter rules
    to get clean match-winner markets for arbitrage.
    """

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

SUPPORTED SPORTS AND MARKETS:
- Football (soccer): 1X2 market (home / draw / away).
- Rugby: main match winner market (home / away; 2-way).
- Basketball: main match winner market (home / away; 2-way).
- Other sports: only include if the market is clearly home/draw/away or home/away.

TASK:
From the HTML below, find all PRE-MATCH events and return ONLY valid JSON, no explanations.

Return EXACTLY this format (JSON object):

{{
  "matches": [
    {{
      "home_team": "Team A",
      "away_team": "Team B",
      "home_odd": 2.10,
      "draw_odd": 3.00,
      "away_odd": 2.50,
      "competition": "League or competition name (if available)",
      "sport": "Football",
      "market_type": "3-way"
    }}
  ]
}}

MATCH SELECTION RULES:
- Include only PRE-MATCH odds. Exclude live/in-play or markets clearly marked as "Live", "In-play", etc.
- Exclude virtual sports, simulated games, casino, slots, and non-sports events.
- Exclude boosted odds, specials, bet builders, and exotic markets.
- Use the main match winner market:
  - Football: standard 1X2 (home, draw, away).
  - Rugby/Basketball: two-way match winner (home, away).

TEAM NAME RULES:
- Exclude placeholder/test names: "Team 1", "Team 2", "Home", "Away", "Test Team", etc.
- Include realistic team/club/country names.

ODDS RULES:
- Odds must be positive decimal numbers (e.g., 1.50, 2.10).
- For 3-way markets: home_odd, draw_odd, away_odd present.
- For 2-way markets: home_odd, away_odd present; draw_odd is null.

COMPETITION AND SPORT:
- competition: league/tournament if available, else empty string.
- sport: use visible sport name; if not obvious, default to "{sport}".

OUTPUT STRICTNESS:
- Return ONLY JSON (starting with '{{' and ending with '}}').
- If no valid matches: return {{ "matches": [] }}

HTML STARTS BELOW THIS LINE:
{html}
"""

        # Use Gemini 2.0 Flash via Gen AI SDK.[web:186][web:189]
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
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

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            print(f"Gemini JSON decode error for {bookmaker_name}")
            return odds

        matches = data.get("matches", []) if isinstance(data, dict) else []

        for m in matches:
            try:
                home_team = m.get("home_team")
                away_team = m.get("away_team")
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


def scrape_betpawa_gemini():
    return scrape_with_gemini("https://www.betpawa.ug/", "BetPawa-Gemini", sport="Football")


def scrape_fortebet_gemini():
    return scrape_with_gemini("https://www.fortebet.ug/", "ForteBet-Gemini", sport="Football")


# -----------------------------
# Native scrapers (HTTP / Playwright)
# -----------------------------

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
            "User-Agent": "Mozilla/5.0",
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
            "User-Agent": "Mozilla/5.0",
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
                        clean_odd(row.get("1")),
                        clean_odd(row.get("X")),
                        clean_odd(row.get("2")),
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
                                            clean_odd(odd_values[0]),
                                            clean_odd(odd_values[1] if len(odd_values) >= 3 else None),
                                            clean_odd(odd_values[2] if len(odd_values) >= 3 else odd_values[1]),
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


# (You can add Fortebet, SportyBet, 1xBet, 22Bet, Melbet scrapers here similarly.)

# -----------------------------
# Arbitrage finder
# -----------------------------

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

        for match_key, bookmakers in merged_groups.items():
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
                                        "match": bookmakers[0]["match"],
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

    return opportunities


# -----------------------------
# Main
# -----------------------------

def main():
    all_odds = []

    # Native scrapers
    all_odds.extend(scrape_ababet())
    all_odds.extend(scrape_betika())
    all_odds.extend(scrape_championbet())
    all_odds.extend(scrape_betpawa())

    # Gemini scrapers (HTML → JSON via Gemini 2.0 Flash)
    all_odds.extend(scrape_ababet_gemini())
    all_odds.extend(scrape_betpawa_gemini())
    all_odds.extend(scrape_fortebet_gemini())

    print(f"Total raw odds: {len(all_odds)}")

    fresh = filter_stale_odds(all_odds)
    print(f"Total fresh odds: {len(fresh)}")

    arbs = find_arbitrage(fresh)
    arbs = filter_opportunities_with_football_data(arbs)
    print(f"Current arbs: {len(arbs)}")

    # Save arbitrage opportunities
    try:
        with open(HISTORY_FILE, "a") as f:
            for opp in arbs:
                opp["timestamp"] = datetime.now(timezone.utc).isoformat()
                f.write(json.dumps(opp) + "\n")
    except Exception as e:
        print("Error writing history:", e)

    # Print a sample
    for opp in arbs[:10]:
        print(json.dumps(opp, indent=2))


if __name__ == "__main__":
    main()
