# ============================================================================
#                           ARBITRAGE SCANNER (Standalone)
# ============================================================================

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
import os
from copy import deepcopy
from itertools import combinations, permutations

import requests
from bs4 import BeautifulSoup

# ---------- Constants ----------
SPORTYBET_API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"
CHAMPIONBET_API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
CHAMPIONBET_MATCH_API = "https://www.championbet.ug/restapi/offer/en/match/{match_id}?annex=13&mobileVersion=2.47.4.3&locale=en"

# Uganda Government Withholding Tax: 15% on net profit
WINNINGS_TAX_RATE = 0.15

STAKE = 100000
HISTORY_FILE = "arb_history.json"
OPPORTUNITIES_FILE = "current_opportunities.json"

# ---------- Helper Functions ----------
def normalize(name):
    name = (name or "").lower().strip()
    name = re.sub(r"\b(rovers|rvs)\b", "rvs", name)
    name = re.sub(r"\b(united|utd)\b", "utd", name)
    name = re.sub(r"\b(fc|sc|cf|ac|city|sports|club|football|soccer|women|men|u21|u23)\b", "", name)
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def teams_match(name1, name2):
    n1 = normalize(name1)
    n2 = normalize(name2)
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
    if "|" in key1 or "|" in key2:
        return key1 == key2
    parts1 = key1.split(" vs ")
    parts2 = key2.split(" vs ")
    if len(parts1) != 2 or len(parts2) != 2:
        return False
    return teams_match(parts1[0], parts2[0]) and teams_match(parts1[1], parts2[1])

def clean_odd(v, min_odd=1.01, max_odd=50.0):
    try:
        if v is None:
            return None
        v = float(v)
        if min_odd <= v <= max_odd:
            return v
    except:
        pass
    return None

def build_match_record(home_team, away_team, bookmaker, home, draw, away, sport="Football", competition="", market_type="1x2", market_specifier=""):
    base_key = f"{normalize(home_team)} vs {normalize(away_team)}"
    if market_type == "Over/Under 2.5":
        match_key = f"{base_key} | O/U 2.5"
    elif market_type == "Asian Handicap":
        match_key = f"{base_key} | AH {market_specifier}"
    elif market_type == "Double Chance":
        match_key = f"{base_key} | DC {market_specifier}"
    elif market_type == "BTTS":
        match_key = f"{base_key} | BTTS"
    else:
        match_key = base_key

    return {
        "match": f"{home_team} vs {away_team}",
        "home_team": home_team,
        "away_team": away_team,
        "match_key": match_key,
        "bookmaker": bookmaker,
        "competition": competition,
        "home": home,
        "draw": draw,
        "away": away,
        "sport": sport,
        "market_type": market_type,
        "market_specifier": market_specifier
    }

def normalize_sport_name(record):
    raw = (record.get("sport") or "").strip().lower()
    if not raw:
        record["sport"] = "Football"
        return record
    if "foot" in raw or "soccer" in raw:
        record["sport"] = "Football"
    elif "basket" in raw:
        record["sport"] = "Basketball"
    elif "netball" in raw:
        record["sport"] = "Netball"
    elif "tennis" in raw:
        record["sport"] = "Tennis"
    elif "rugby" in raw:
        record["sport"] = "Rugby"
    elif "futsal" in raw:
        record["sport"] = "Futsal"
    else:
        record["sport"] = raw.title()
    return record

# ---------- History Management ----------
def load_arbitrage_history():
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_arbitrage_history(arb_history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(arb_history, f, indent=2)

def opportunity_key(opp):
    mtype = opp.get('market_type', '1x2')
    spec = opp.get('market_specifier', '')
    return f"{opp['sport']}::{mtype}::{opp['match']}::{spec}"

def update_arbitrage_history(current_opportunities, arb_history, timestamp_str):
    for history in arb_history.values():
        history["updated_this_cycle"] = False

    for opp in current_opportunities:
        if 'market_type' not in opp:
            opp['market_type'] = '1x2'
        if 'market_specifier' not in opp:
            opp['market_specifier'] = ''

        key = opportunity_key(opp)
        if key not in arb_history:
            entry = {
                "match": opp["match"],
                "sport": opp["sport"],
                "market_type": opp["market_type"],
                "market_specifier": opp.get("market_specifier", ""),
                "first_seen": timestamp_str,
                "last_seen": timestamp_str,
                "valid": True,
                "cycles_missed": 0,
                "versions": [],
            }
            arb_history[key] = entry

        entry = arb_history[key]
        entry["last_seen"] = timestamp_str
        entry["valid"] = True
        entry["cycles_missed"] = 0
        entry["updated_this_cycle"] = True

        version = {
            "timestamp": timestamp_str,
            "profit_percent": opp["profit_percent"],
            "profit_ugx": opp["profit_ugx"],
            "arb_sum": opp["arb_sum"],
            "bets": deepcopy(opp["bets"]),
        }
        entry["versions"].append(version)

    for key, entry in arb_history.items():
        if not entry.get("updated_this_cycle"):
            entry["cycles_missed"] = entry.get("cycles_missed", 0) + 1
            if entry["cycles_missed"] >= 2:
                entry["valid"] = False

    for entry in arb_history.values():
        if "updated_this_cycle" in entry:
            del entry["updated_this_cycle"]

# ---------- Scraping Functions (Active Bookmakers) ----------
def championbet_extract_1x2_from_betmap(bet_map):
    bet_map = bet_map or {}
    def pick_odd(market_keys):
        for k in market_keys:
            market = bet_map.get(str(k), {}) or {}
            if not isinstance(market, dict): continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None: return odd
        return None
    return pick_odd([1, 4, 7]), pick_odd([2, 5, 8]), pick_odd([3, 6, 9])

def championbet_extract_ou_from_betmap(bet_map):
    bet_map = bet_map or {}
    def pick_odd(market_keys):
        for k in market_keys:
            market = bet_map.get(str(k), {}) or {}
            if not isinstance(market, dict): continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None: return odd
        return None
    return pick_odd([51, 21]), pick_odd([52, 22])

def championbet_extract_ah_dc_btts_from_betmap(bet_map):
    bet_map = bet_map or {}
    ah_odds, dc_odds, btts_odds = {}, {}, {}
    def get_odds(market_keys):
        odds_dict = {}
        for k in market_keys:
            market = bet_map.get(str(k), {}) or {}
            if not isinstance(market, dict): continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None:
                        odds_dict[k] = odd
        return odds_dict

    ah_odds = get_odds([5, 6, 7, 8])
    dc_odds = get_odds([20, 21, 22])
    btts_odds = get_odds([19, 20])
    return ah_odds, dc_odds, btts_odds

def scrape_championbet():
    odds = []
    try:
        print("Fetching ChampionBet...")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Referer": "https://www.championbet.ug/mob/",
        }
        req = urllib.request.Request(CHAMPIONBET_API, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            top_data = json.loads(resp.read().decode())

        matches = top_data.get("esMatches", []) if isinstance(top_data, dict) else []
        print(f"ChampionBet: {len(matches)} matches in top list")
        count = 0
        for m in matches:
            try:
                sport_token = str(m.get("sportToken", ""))
                if "Soccer" not in sport_token: continue
                match_id = m.get("id")
                if not match_id: continue
                home_team = m.get("home") or ""
                away_team = m.get("away") or ""
                if not home_team or not away_team: continue

                match_url = CHAMPIONBET_MATCH_API.format(match_id=match_id)
                match_req = urllib.request.Request(match_url, headers=headers)
                with urllib.request.urlopen(match_req, timeout=30) as r2:
                    match_data = json.loads(r2.read().decode())
                bet_map = match_data.get("betMap", {}) if isinstance(match_data, dict) else {}

                h, d, a = championbet_extract_1x2_from_betmap(bet_map)
                if h and a:
                    count += 1
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", h, d, a, competition=m.get("leagueName", ""), market_type="1x2"))

                over, under = championbet_extract_ou_from_betmap(bet_map)
                if over and under:
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", over, under, None, market_type="Over/Under 2.5"))

                ah_odds, dc_odds, btts_odds = championbet_extract_ah_dc_btts_from_betmap(bet_map)
                if ah_odds.get(5) and ah_odds.get(6):
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", ah_odds[5], None, ah_odds[6], market_type="Asian Handicap", market_specifier="-1.5"))
                if ah_odds.get(7) and ah_odds.get(8):
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", ah_odds[7], None, ah_odds[8], market_type="Asian Handicap", market_specifier="-0.5"))
                if dc_odds.get(20): odds.append(build_match_record(home_team, away_team, "ChampionBet", dc_odds[20], None, None, market_type="Double Chance", market_specifier="1X"))
                if dc_odds.get(21): odds.append(build_match_record(home_team, away_team, "ChampionBet", None, None, dc_odds[21], market_type="Double Chance", market_specifier="X2"))
                if dc_odds.get(22): odds.append(build_match_record(home_team, away_team, "ChampionBet", dc_odds[22], None, None, market_type="Double Chance", market_specifier="12"))
                if btts_odds.get(19) and btts_odds.get(20):
                    odds.append(build_match_record(home_team, away_team, "ChampionBet", btts_odds[19], None, btts_odds[20], market_type="BTTS"))

                time.sleep(0.2)
            except:
                continue
        print(f"ChampionBet: {count} matches extracted")
    except Exception as e:
        print(f"ChampionBet error: {e}")
    return odds

def scrape_ababet():
    odds = []
    try:
        print("Fetching AbaBet...")
        url = "https://www.ababet.ug/soccer/match_result?mobile=1"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            print("AbaBet: no tables found")
            return odds

        for table in tables:
            first_row = table.find("tr")
            if not first_row: continue
            headers = [c.get_text(" ", strip=True) for c in first_row.find_all(["th", "td"])]
            if "Home" not in headers or "Away" not in headers: continue
            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 5: continue
                row = dict(zip(headers, cells[:len(headers)]))
                home, away = row.get("Home"), row.get("Away")
                if not home or not away or home == "-" or away == "-": continue

                h = row.get("1"); d = row.get("X"); a = row.get("2")
                if h and a:
                    odds.append(build_match_record(home, away, "AbaBet", h, d, a, competition=row.get("League", ""), market_type="1x2"))

                over = row.get("Over"); under = row.get("Under")
                if over and under:
                    odds.append(build_match_record(home, away, "AbaBet", over, under, None, market_type="Over/Under 2.5"))

        print(f"AbaBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"AbaBet error: {e}")
    return odds

def scrape_fortebet():
    odds = []
    try:
        print("Fetching Fortebet API...")
        url = "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://desktop.fortebet.ug/prematch/landing"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        inner = data.get("data", {})
        events = inner.get("event", {})
        markets = inner.get("markets", {})
        competitors = inner.get("competitors", {})
        event_markets = {}
        for _, market in markets.items():
            event_markets.setdefault(str(market.get("eventId", "")), []).append(market)

        count = 0
        for eid, event in events.items():
            try:
                comps = event.get("competitors", [])
                if len(comps) < 2: continue
                home = competitors.get(str(comps[0]), {}).get("name", "")
                away = competitors.get(str(comps[1]), {}).get("name", "")
                if not home or not away: continue
                h = d = a = over = under = None
                ah_home = ah_away = None
                dc_home = dc_away = None
                btts_yes = btts_no = None

                for market in event_markets.get(eid, []):
                    mid = market.get("marketId")
                    if mid == 1:
                        odd_list = []
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                odd_list.append((v.get("outcomeId", 0), clean_odd(v["odds"])))
                        odd_list = [(i, o) for i, o in odd_list if o is not None]
                        odd_list.sort(key=lambda x: x[0])
                        if len(odd_list) >= 3:
                            h, d, a = odd_list[0][1], odd_list[1][1], odd_list[2][1]
                        elif len(odd_list) == 2:
                            h, a = odd_list[0][1], odd_list[1][1]
                    elif mid == 5:
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1: over = clean_odd(v["odds"])
                                elif oid == 2: under = clean_odd(v["odds"])
                    elif mid == 2:
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1: ah_home = clean_odd(v["odds"])
                                elif oid == 2: ah_away = clean_odd(v["odds"])
                    elif mid == 8:
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1: dc_home = clean_odd(v["odds"])
                                elif oid == 3: dc_away = clean_odd(v["odds"])
                    elif mid == 12:
                        mkt_odds = market.get("odds", {})
                        for _, v in mkt_odds.items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1: btts_yes = clean_odd(v["odds"])
                                elif oid == 2: btts_no = clean_odd(v["odds"])

                if h and a:
                    sport_name = "Netball" if d is None else "Football"
                    ev_sport = (event.get("sportName") or event.get("sport") or "").lower()
                    if "basketball" in ev_sport: sport_name = "Basketball"
                    elif "tennis" in ev_sport: sport_name = "Tennis"
                    count += 1
                    odds.append(build_match_record(home, away, "Fortebet", h, d, a, sport=sport_name, market_type="1x2"))

                if over and under:
                    odds.append(build_match_record(home, away, "Fortebet", over, under, None, sport="Football", market_type="Over/Under 2.5"))

                if ah_home and ah_away:
                    odds.append(build_match_record(home, away, "Fortebet", ah_home, None, ah_away, sport="Football", market_type="Asian Handicap", market_specifier="-0.5"))

                if dc_home: odds.append(build_match_record(home, away, "Fortebet", dc_home, None, None, sport="Football", market_type="Double Chance", market_specifier="1X"))
                if dc_away: odds.append(build_match_record(home, away, "Fortebet", None, None, dc_away, sport="Football", market_type="Double Chance", market_specifier="12"))

                if btts_yes and btts_no:
                    odds.append(build_match_record(home, away, "Fortebet", btts_yes, None, btts_no, sport="Football", market_type="BTTS"))

            except: continue
        print(f"Fortebet: {count} matches extracted")
    except Exception as e:
        print(f"Fortebet error: {e}")
    return odds

def scrape_sportybet():
    odds = []
    try:
        print("Fetching SportyBet...")
        req = urllib.request.Request(SPORTYBET_API, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, list):
            for event in data:
                try:
                    home, away = event.get("home_team", ""), event.get("away_team", "")
                    if not home or not away: continue
                    sport = (event.get("sport") or "Football").strip()
                    h = clean_odd(event.get("home"))
                    d = clean_odd(event.get("draw"))
                    a = clean_odd(event.get("away"))
                    if h and a:
                        odds.append(build_match_record(home, away, "SportyBet", h, d, a, sport=sport, market_type="1x2"))

                    over = clean_odd(event.get("over_odd"))
                    under = clean_odd(event.get("under_odd"))
                    if over and under:
                        odds.append(build_match_record(home, away, "SportyBet", over, under, None, sport=sport, market_type="Over/Under 2.5"))
                except: continue
        print(f"SportyBet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"SportyBet error: {e}")
    return odds

def scrape_melbet():
    odds = []
    try:
        print("Fetching Melbet (GetTopGamesStatZip)...")
        base_url = "https://melbet-424658.top"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-mobile-project-id": "0",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": f"{base_url}/en/line/football",
        }

        top_url = f"{base_url}/service-api/LineFeed/GetTopGamesStatZip"
        top_params = {
            "lng": "en",
            "antisports": "66",
            "partner": "8"
        }
        top_req = requests.get(top_url, headers=headers, params=top_params, timeout=30)
        top_data = top_req.json()
        events = top_data.get("Value", [])
        print(f"Melbet: {len(events)} top games found")

        for event in events:
            try:
                home = event.get("O1", "")
                away = event.get("O2", "")
                if not home or not away:
                    continue
                if event.get("SI") != 1:
                    continue

                odds_map = {}
                for item in event.get("E", []):
                    t = item.get("T")
                    c = clean_odd(item.get("C"))
                    if c is None:
                        continue
                    p = item.get("P")
                    key = (t, p)
                    odds_map[key] = c

                home_odd = odds_map.get((1, None))
                draw_odd = odds_map.get((2, None))
                away_odd = odds_map.get((3, None))
                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "Melbet", home_odd, draw_odd, away_odd,
                                                   sport="Football", competition=event.get("L", ""), market_type="1x2"))

                over_25 = odds_map.get((11, None)) or odds_map.get((11, 2.5))
                under_25 = odds_map.get((12, None)) or odds_map.get((12, 2.5))
                if over_25 and under_25:
                    odds.append(build_match_record(home, away, "Melbet", over_25, under_25, None,
                                                   sport="Football", competition=event.get("L", ""), market_type="Over/Under 2.5"))

                ah_home_odds = {}
                ah_away_odds = {}
                for (t, p), c in odds_map.items():
                    if t in (7, 13, 3829):
                        if p is not None:
                            ah_home_odds[p] = c
                    elif t in (8, 14, 3830):
                        if p is not None:
                            ah_away_odds[p] = c
                for line in set(ah_home_odds.keys()) & set(ah_away_odds.keys()):
                    if line is None:
                        continue
                    odds.append(build_match_record(home, away, "Melbet", ah_home_odds[line], None, ah_away_odds[line],
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="Asian Handicap", market_specifier=str(line)))

                dc_1x = odds_map.get((180, None))
                dc_x2 = odds_map.get((182, None))
                dc_12 = odds_map.get((181, None))
                if dc_1x:
                    odds.append(build_match_record(home, away, "Melbet", dc_1x, None, None,
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="Double Chance", market_specifier="1X"))
                if dc_x2:
                    odds.append(build_match_record(home, away, "Melbet", None, None, dc_x2,
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="Double Chance", market_specifier="X2"))
                if dc_12:
                    odds.append(build_match_record(home, away, "Melbet", dc_12, None, None,
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="Double Chance", market_specifier="12"))

                btts_yes = odds_map.get((19, None))
                btts_no = odds_map.get((20, None))
                if btts_yes and btts_no:
                    odds.append(build_match_record(home, away, "Melbet", btts_yes, None, btts_no,
                                                   sport="Football", competition=event.get("L", ""),
                                                   market_type="BTTS"))

            except Exception:
                continue

        print(f"Melbet: extracted {len(odds)} records")
    except Exception as e:
        print(f"Melbet error: {e}")
    return odds

# ---------- New Bookmaker Placeholders ----------
def scrape_bongobongo():
    """BongoBongo – modern GR8 Tech API. TODO: fill in real endpoint."""
    print("BongoBongo: placeholder – no odds extracted (needs API endpoint)")
    return []

def scrape_betano():
    """Betano – JSON API. TODO: fill in real endpoint."""
    print("Betano: placeholder – no odds extracted (needs API endpoint)")
    return []

def scrape_bet9ja():
    """Bet9ja – JSON API. TODO: fill in real endpoint."""
    print("Bet9ja: placeholder – no odds extracted (needs API endpoint)")
    return []

def scrape_nilebet():
    """NileBet – JSON API. TODO: fill in real endpoint."""
    print("NileBet: placeholder – no odds extracted (needs API endpoint)")
    return []

def scrape_betway_uganda():
    """Betway Uganda – JSON API. TODO: fill in real endpoint."""
    print("Betway Uganda: placeholder – no odds extracted (needs API endpoint)")
    return []

# ---------- Arbitrage Detection (with 15% tax on profit) ----------
def find_arbitrage(all_odds):
    """
    Finds valid arbitrage opportunities.
    Rules:
      1. Pairings cover all outcomes with no gap.
      2. No shared feed blocking.
      3. Accept only arbs with arb_sum between 0.625 and 0.99 (raw profit 1%-60%).
      4. After 15% tax on profit, net profit must be > 0.
    """
    opportunities = []
    SHARED = set()

    def market_type_standard(mt):
        mt = (mt or "").lower().strip()
        if mt in ["1x2", "match winner", "winning"]:
            return "1X2"
        if "over/under" in mt or "ou" in mt:
            return "OU"
        if "asian handicap" in mt or "ah" in mt:
            return "AH"
        if "btts" in mt or "both teams to score" in mt:
            return "BTTS"
        return mt.upper()

    sides = []
    for rec in all_odds:
        mk = rec.get("match_key", "")
        mt = market_type_standard(rec.get("market_type", ""))
        spec = rec.get("market_specifier", "")
        bk = rec.get("bookmaker", "")
        sport = rec.get("sport", "Football")
        home_odd = clean_odd(rec.get("home"))
        draw_odd = clean_odd(rec.get("draw"))
        away_odd = clean_odd(rec.get("away"))

        if mt == "1X2":
            if home_odd is not None:
                sides.append((mk, mt, "home", "", bk, home_odd, sport))
            if draw_odd is not None:
                sides.append((mk, mt, "draw", "", bk, draw_odd, sport))
            if away_odd is not None:
                sides.append((mk, mt, "away", "", bk, away_odd, sport))
        elif mt == "OU":
            line = spec if spec else "2.5"
            if home_odd is not None:
                sides.append((mk, mt, "over", line, bk, home_odd, sport))
            if away_odd is not None:
                sides.append((mk, mt, "under", line, bk, away_odd, sport))
        elif mt == "AH":
            line = spec
            if home_odd is not None:
                sides.append((mk, mt, "home", line, bk, home_odd, sport))
            if away_odd is not None:
                sides.append((mk, mt, "away", line, bk, away_odd, sport))
        elif mt == "BTTS":
            if home_odd is not None:
                sides.append((mk, mt, "yes", "", bk, home_odd, sport))
            if away_odd is not None:
                sides.append((mk, mt, "no", "", bk, away_odd, sport))

    groups = {}
    for side in sides:
        groups.setdefault(side[0], []).append(side)

    for mk, group in groups.items():
        mt = group[0][1]

        if mt == "1X2":
            best = {}
            for side in group:
                _, _, s, _, bk, odd, sport = side
                best.setdefault(bk, {})[s] = max(best.get(bk, {}).get(s, 0), odd)

            bookies = list(best.keys())
            for bk_h, bk_d, bk_a in permutations(bookies, 3):
                if bk_h == bk_d or bk_h == bk_a or bk_d == bk_a:
                    continue
                if bk_h in SHARED and (bk_d in SHARED or bk_a in SHARED):
                    continue
                if bk_d in SHARED and bk_a in SHARED:
                    continue

                h = best[bk_h].get("home")
                d = best[bk_d].get("draw")
                a = best[bk_a].get("away")
                if not h or not d or not a:
                    continue

                arb_sum = 1/h + 1/d + 1/a
                if arb_sum >= 0.99 or arb_sum < 0.625:
                    continue

                odds = [h, d, a]
                stakes = [STAKE * (1/odd) / arb_sum for odd in odds]

                raw_profits = [stake_i * odd_i - STAKE for stake_i, odd_i in zip(stakes, odds)]
                net_profits = [raw * 0.85 for raw in raw_profits]  # 15% tax on profit

                if any(np <= 0 for np in net_profits):
                    continue

                net_profit = min(net_profits)
                net_profit_percent = (net_profit / STAKE) * 100

                bets = [
                    {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stakes[0], "win": round(stakes[0] * h)},
                    {"bookmaker": bk_d, "outcome": "Draw", "odd": d, "stake": stakes[1], "win": round(stakes[1] * d)},
                    {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stakes[2], "win": round(stakes[2] * a)}
                ]
                opportunities.append({
                    "match": mk.split(" | ")[0] if " | " in mk else mk,
                    "sport": group[0][6],
                    "type": "3-way",
                    "profit_percent": round(net_profit_percent, 2),
                    "profit_ugx": round(net_profit),
                    "total_stake": STAKE,
                    "arb_sum": round(arb_sum, 4),
                    "tax_applied": f"{WINNINGS_TAX_RATE*100:.0f}%",
                    "bets": bets
                })

        else:
            best = {}
            for side in group:
                _, _, s, line, bk, odd, sport = side
                best.setdefault(bk, {})[s] = max(best.get(bk, {}).get(s, 0), odd)

            bookies = list(best.keys())
            for bk1, bk2 in combinations(bookies, 2):
                if bk1 == bk2:
                    continue
                if bk1 in SHARED and bk2 in SHARED:
                    continue

                if mt == "AH":
                    side1, side2 = "home", "away"
                elif mt == "OU":
                    side1, side2 = "over", "under"
                elif mt == "BTTS":
                    side1, side2 = "yes", "no"
                else:
                    continue

                o1 = best[bk1].get(side1)
                o2 = best[bk2].get(side2)
                o1_alt = best[bk1].get(side2)
                o2_alt = best[bk2].get(side1)

                candidates = []
                if o1 and o2:
                    candidates.append((o1, o2, bk1, bk2, side1, side2))
                if o1_alt and o2_alt:
                    candidates.append((o1_alt, o2_alt, bk1, bk2, side2, side1))

                if not candidates:
                    continue

                best_candidate = min(candidates, key=lambda x: 1/x[0] + 1/x[1])
                odd1, odd2, bk_a, bk_b, side_a, side_b = best_candidate
                arb_sum = 1/odd1 + 1/odd2
                if arb_sum >= 0.99 or arb_sum < 0.625:
                    continue

                odds = [odd1, odd2]
                stakes = [STAKE * (1/odd) / arb_sum for odd in odds]

                raw_profits = [stake_i * odd_i - STAKE for stake_i, odd_i in zip(stakes, odds)]
                net_profits = [raw * 0.85 for raw in raw_profits]

                if any(np <= 0 for np in net_profits):
                    continue

                net_profit = min(net_profits)
                net_profit_percent = (net_profit / STAKE) * 100

                if mt == "AH":
                    label1 = f"AH {group[0][3]} {side_a}"
                    label2 = f"AH {group[0][3]} {side_b}"
                elif mt == "OU":
                    label1 = "Over" if side_a == "over" else "Under"
                    label2 = "Over" if side_b == "over" else "Under"
                elif mt == "BTTS":
                    label1 = "Yes" if side_a == "yes" else "No"
                    label2 = "Yes" if side_b == "yes" else "No"

                bets = [
                    {"bookmaker": bk_a, "outcome": label1, "odd": odd1, "stake": stakes[0], "win": round(stakes[0] * odd1)},
                    {"bookmaker": bk_b, "outcome": label2, "odd": odd2, "stake": stakes[1], "win": round(stakes[1] * odd2)}
                ]
                opportunities.append({
                    "match": mk.split(" | ")[0] if " | " in mk else mk,
                    "sport": group[0][6],
                    "type": mt,
                    "profit_percent": round(net_profit_percent, 2),
                    "profit_ugx": round(net_profit),
                    "total_stake": STAKE,
                    "arb_sum": round(arb_sum, 4),
                    "tax_applied": f"{WINNINGS_TAX_RATE*100:.0f}%",
                    "bets": bets
                })

    return opportunities

# ---------- Telegram Alert ----------
def send_telegram_alert(opp):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        print("⚠️ Telegram credentials missing – alert not sent.")
        return

    match = opp.get('match', 'Unknown')
    profit = opp.get('profit_percent', 0)
    ugx = opp.get('profit_ugx', 0)
    message = f"⚽ *{match}*\n💰 Net Profit (after {opp.get('tax_applied', '15%')} tax): *{profit}%* (UGX {ugx:,})\n"
    for bet in opp.get('bets', []):
        bookie = bet.get('bookmaker', 'Unknown')
        outcome = bet.get('outcome', 'Unknown')
        odd = bet.get('odd', 0)
        stake = bet.get('stake', 0)
        message += f"▶ {bookie} ({outcome}) @ {odd} – Stake: UGX {stake:,}\n"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={'chat_id': chat, 'text': message, 'parse_mode': 'Markdown'}, timeout=10)
        print(f"✅ Alert sent for {match}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")

# ---------- Main Scanner ----------
def run_scan():
    all_odds = []
    all_odds.extend(scrape_sportybet())
    all_odds.extend(scrape_championbet())
    all_odds.extend(scrape_ababet())
    all_odds.extend(scrape_fortebet())
    all_odds.extend(scrape_melbet())

    # New bookmakers (placeholders – you will need to fill in the real scraping logic)
    all_odds.extend(scrape_bongobongo())
    all_odds.extend(scrape_betano())
    all_odds.extend(scrape_bet9ja())
    all_odds.extend(scrape_nilebet())
    all_odds.extend(scrape_betway_uganda())

    opportunities = find_arbitrage(all_odds)
    arb_history = load_arbitrage_history()
    timestamp_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    for opp in opportunities:
        key = opportunity_key(opp)
        is_new = (key in arb_history and arb_history[key]['first_seen'] == timestamp_str)
        if is_new and opp.get('profit_percent', 0) >= 1.0:
            send_telegram_alert(opp)

    update_arbitrage_history(opportunities, arb_history, timestamp_str)
    save_arbitrage_history(arb_history)

    with open(OPPORTUNITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(opportunities, f, indent=2)

    print(f"Scan complete: {len(opportunities)} opportunities, history updated.")

# ---------- Execution ----------
if __name__ == "__main__":
    run_scan()
