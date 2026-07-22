# ============================================================================
#                           ARBITRAGE SCANNER
# ============================================================================

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
import os
from copy import deepcopy
from functools import wraps

import requests
from bs4 import BeautifulSoup

SPORTYBET_API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"
CHAMPIONBET_API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
CHAMPIONBET_MATCH_API = "https://www.championbet.ug/restapi/offer/en/match/{match_id}?annex=13&mobileVersion=2.47.4.3&locale=en"

STAKE = 100000
HISTORY_FILE = "arb_history.json"

SHARED_BOOKMAKERS_1X = {
    "1xBet": {"base_url": "https://1xbet.ug", "partner": "135"},
    "22Bet": {"base_url": "https://22bet.ug", "partner": "151"},
    "Melbet": {"base_url": "https://melbet.ug", "partner": "8"},
}


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


def scrape_1xbet():
    odds = []
    try:
        print("Fetching 1xBet Uganda...")
        url = "https://1xbet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=135&getEmpty=true&virtualSports=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except:
                data = json.loads(raw.decode("utf-8-sig"))
        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team: continue
                if home_team.strip() == "Home" and away_team.strip() == "Away":
                    continue
                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None: continue
                    if t == "1": home_odd = c
                    elif t == "2": away_odd = c
                    elif t == "3": draw_odd = c
                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(build_match_record(home_team, away_team, "1xBet", home_odd, draw_odd, away_odd, market_type="1x2"))
            except: continue
        print(f"1xBet: {count} matches extracted")
    except Exception as e:
        print(f"1xBet error: {e}")
    return odds


def scrape_22bet():
    odds = []
    try:
        print("Fetching 22Bet Uganda...")
        url = "https://22bet.ug/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner=151&getEmpty=true&virtualSports=true"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try: data = json.loads(raw.decode("utf-8"))
            except: data = json.loads(raw.decode("utf-8-sig"))
        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team: continue
                if home_team.strip() == "Home" and away_team.strip() == "Away":
                    continue
                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None: continue
                    if t == "1": home_odd = c
                    elif t == "2": away_odd = c
                    elif t == "3": draw_odd = c
                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(build_match_record(home_team, away_team, "22Bet", home_odd, draw_odd, away_odd, market_type="1x2"))
            except: continue
        print(f"22Bet: {count} matches extracted")
    except Exception as e:
        print(f"22Bet error: {e}")
    return odds


def scrape_melbet():
    odds = []
    try:
        print("Fetching Melbet...")
        url = "https://melbet-046935.top/service-api/LineFeed/Get1x2_VZip?count=1000&lng=en&mode=4&country=191&partner=8&getEmpty=true"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-mobile-project-id": "0",
            "x-requested-with": "XMLHttpRequest",
            "is-srv": "false",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/149.0.7827.91 Mobile Safari/537.36",
            "Referer": "https://1xbet.ug/en/line/football",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try: data = json.loads(raw.decode("utf-8"))
            except: data = json.loads(raw.decode("utf-8-sig"))
        values = data.get("Value", []) if isinstance(data, dict) else []
        count = 0
        for match in values:
            try:
                home_team = match.get("O1")
                away_team = match.get("O2")
                if not home_team or not away_team: continue
                if home_team.strip() == "Home" and away_team.strip() == "Away":
                    continue
                home_odd = draw_odd = away_odd = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if c is None: continue
                    if t == "1": home_odd = c
                    elif t == "2": away_odd = c
                    elif t == "3": draw_odd = c
                if home_odd is not None and away_odd is not None:
                    count += 1
                    odds.append(build_match_record(home_team, away_team, "Melbet", home_odd, draw_odd, away_odd, market_type="1x2"))
            except: continue
        print(f"Melbet: {count} matches extracted")
    except Exception as e:
        print(f"Melbet error: {e}")
    return odds


def scrape_1x_over_under(bookmaker_name, base_url, partner_id):
    odds = []
    try:
        print(f"Fetching {bookmaker_name} Over/Under...")
        url = f"{base_url}/service-api/LineFeed/GetEvents_VZip?count=1000&lng=en&mode=4&country=191&partner={partner_id}&market=5,6&getEmpty=true&virtualSports=true&eventType=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        for match in data.get("Value", []):
            home, away = match.get("O1"), match.get("O2")
            if not home or not away: continue
            over = under = None
            for e in match.get("E", []):
                t = str(e.get("T", "")).strip()
                c = clean_odd(e.get("C"))
                if not c: continue
                if t == "5": over = c
                elif t == "6": under = c
            if over and under:
                record = build_match_record(home, away, bookmaker_name, over, under, None, market_type="Over/Under 2.5")
                odds.append(record)
    except Exception as e:
        print(f"{bookmaker_name} Over/Under error: {e}")
    return odds


def scrape_1x_ah_dc_btts(bookmaker_name, base_url, partner_id):
    odds = []
    try:
        print(f"Fetching {bookmaker_name} AH, DC, BTTS...")
        url = f"{base_url}/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner={partner_id}&getEmpty=true"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        for match in data.get("Value", []):
            home = match.get("O1")
            away = match.get("O2")
            if not home or not away: continue
            if home.strip() == "Home" and away.strip() == "Away":
                continue

            ah_home = ah_away = None
            dc_home = dc_away = None
            btts_yes = btts_no = None

            for e in match.get("E", []):
                t = str(e.get("T", "")).strip()
                c = clean_odd(e.get("C"))
                if not c: continue
                p = e.get("P")

                if t == "7" and p is not None: ah_home = c
                elif t == "8" and p is not None: ah_away = c
                elif t == "4" or t == "180": dc_home = c
                elif t == "181": dc_away = c
                elif t == "19": btts_yes = c
                elif t == "20": btts_no = c

            if ah_home and ah_away:
                odds.append(build_match_record(home, away, bookmaker_name, ah_home, None, ah_away, market_type="Asian Handicap", market_specifier="-0.5"))
            if dc_home and dc_away:
                odds.append(build_match_record(home, away, bookmaker_name, dc_home, None, dc_away, market_type="Double Chance", market_specifier="1X"))
            if btts_yes and btts_no:
                odds.append(build_match_record(home, away, bookmaker_name, btts_yes, None, btts_no, market_type="BTTS"))

        print(f"{bookmaker_name} extra markets: {len(odds)} records")
    except Exception as e:
        print(f"{bookmaker_name} extra markets error: {e}")
    return odds


def find_arbitrage(all_odds):
    opportunities = []
    sports_odds = {}
    for odd in all_odds:
        sport = odd.get("sport", "Football")
        sports_odds.setdefault(sport, []).append(odd)

    for sport, sport_odds in sports_odds.items():
        exact_groups = {}
        for odd in sport_odds:
            exact_groups.setdefault(odd.get("match_key", ""), []).append(odd)

        merged_groups, processed_keys = {}, set()
        all_keys = list(exact_groups.keys())
        for i, key1 in enumerate(all_keys):
            if key1 in processed_keys: continue
            group = list(exact_groups[key1])
            processed_keys.add(key1)
            for key2 in all_keys[i+1:]:
                if key2 in processed_keys: continue
                if match_key_similarity(key1, key2):
                    group.extend(exact_groups[key2])
                    processed_keys.add(key2)
            merged_groups[key1] = group

        for match_name, bookmakers in merged_groups.items():
            if not bookmakers: continue
            first = bookmakers[0]
            mtype = first.get("market_type", "1x2")
            spec = first.get("market_specifier", "")

            if len(set(b["bookmaker"] for b in bookmakers)) < 2: continue

            bk_odds = {}
            for b in bookmakers:
                bk = b["bookmaker"]
                bk_odds.setdefault(bk, {"home": 0.0, "draw": 0.0, "away": 0.0})
                home = clean_odd(b.get("home"))
                draw = clean_odd(b.get("draw"))
                away = clean_odd(b.get("away"))
                if home is not None and home > bk_odds[bk]["home"]: bk_odds[bk]["home"] = home
                if draw is not None and draw > bk_odds[bk]["draw"]: bk_odds[bk]["draw"] = draw
                if away is not None and away > bk_odds[bk]["away"]: bk_odds[bk]["away"] = away

            bk_list = list(bk_odds.keys())

            # 2-way markets: O/U, AH, DC, BTTS
            if mtype in ["Over/Under 2.5", "Asian Handicap", "Double Chance", "BTTS"]:
                best = None
                for bk1 in bk_list:
                    for bk2 in bk_list:
                        if bk1 == bk2: continue
                        h1 = bk_odds[bk1]["home"]
                        a1 = bk_odds[bk1]["away"]
                        h2 = bk_odds[bk2]["home"]
                        a2 = bk_odds[bk2]["away"]

                        candidates = []
                        if h1 and a2: candidates.append((h1, a2, bk1, bk2))
                        if h2 and a1: candidates.append((h2, a1, bk2, bk1))
                        if not candidates: continue

                        best_candidate = None
                        best_arb = 2.0
                        for cand in candidates:
                            o, u, bk_o, bk_u = cand
                            arb = (1/o) + (1/u)
                            if arb < best_arb:
                                best_arb = arb
                                best_candidate = (o, u, bk_o, bk_u)

                        if not best_candidate: continue
                        over, under, bk_over, bk_under = best_candidate

                        arb = (1/over) + (1/under)
                        if arb < 1:
                            profit = round((1 - arb) * 100, 2)
                            if 0.5 <= profit <= 50.0:
                                stake_over = round(STAKE * (1/over) / arb)
                                stake_under = round(STAKE * (1/under) / arb)
                                display_match = match_name.split(" | ")[0] if " | " in match_name else match_name
                                best = {
                                    "match": display_match,
                                    "sport": sport,
                                    "type": mtype + (f" {spec}" if spec else ""),
                                    "profit_percent": profit,
                                    "profit_ugx": round(STAKE * (1 - arb)),
                                    "total_stake": STAKE,
                                    "arb_sum": round(arb, 4),
                                    "bets": [
                                        {"bookmaker": bk_over, "outcome": "Outcome 1", "odd": over, "stake": stake_over, "win": round(stake_over * over)},
                                        {"bookmaker": bk_under, "outcome": "Outcome 2", "odd": under, "stake": stake_under, "win": round(stake_under * under)}
                                    ]
                                }
                                if mtype == "Over/Under 2.5":
                                    best["bets"][0]["outcome"] = "Over 2.5"
                                    best["bets"][1]["outcome"] = "Under 2.5"
                                elif mtype == "Asian Handicap":
                                    best["bets"][0]["outcome"] = f"AH {spec} (Home)"
                                    best["bets"][1]["outcome"] = f"AH {spec} (Away)"
                                elif mtype == "Double Chance":
                                    if spec == "1X":
                                        best["bets"][0]["outcome"] = "1X"
                                        best["bets"][1]["outcome"] = "X2"
                                    elif spec == "12":
                                        best["bets"][0]["outcome"] = "12"
                                        best["bets"][1]["outcome"] = "12 (other)"
                                elif mtype == "BTTS":
                                    best["bets"][0]["outcome"] = "BTTS Yes"
                                    best["bets"][1]["outcome"] = "BTTS No"
                                opportunities.append(best)

            # 3-way: Football/Rugby/Futsal
            elif mtype == "1x2" and sport in ["Football", "Rugby", "Futsal"]:
                best = None
                for bk_h in bk_list:
                    for bk_d in bk_list:
                        for bk_a in bk_list:
                            if len({bk_h, bk_d, bk_a}) < 3: continue
                            h, d, a = bk_odds[bk_h]["home"], bk_odds[bk_d]["draw"], bk_odds[bk_a]["away"]
                            if not h or not d or not a: continue
                            arb = (1/h) + (1/d) + (1/a)
                            if arb < 1:
                                profit = round((1 - arb) * 100, 2)
                                if 0.5 <= profit <= 50.0:
                                    stake_h = round(STAKE * (1/h) / arb)
                                    stake_d = round(STAKE * (1/d) / arb)
                                    stake_a = round(STAKE * (1/a) / arb)
                                    best = {
                                        "match": match_name,
                                        "sport": sport,
                                        "type": "3-way",
                                        "profit_percent": profit,
                                        "profit_ugx": round(STAKE * (1 - arb)),
                                        "total_stake": STAKE,
                                        "arb_sum": round(arb, 4),
                                        "bets": [
                                            {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stake_h, "win": round(stake_h * h)},
                                            {"bookmaker": bk_d, "outcome": "Draw", "odd": d, "stake": stake_d, "win": round(stake_d * d)},
                                            {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stake_a, "win": round(stake_a * a)}
                                        ]
                                    }
                if best: opportunities.append(best)

            # 2-way sports
            elif mtype == "1x2" and sport not in ["Football", "Rugby", "Futsal"]:
                best = None
                for bk_h in bk_list:
                    for bk_a in bk_list:
                        if bk_h == bk_a: continue
                        h, a = bk_odds[bk_h]["home"], bk_odds[bk_a]["away"]
                        if not h or not a: continue
                        arb = (1/h) + (1/a)
                        if arb < 1:
                            profit = round((1 - arb) * 100, 2)
                            if 0.5 <= profit <= 50.0:
                                stake_h = round(STAKE * (1/h) / arb)
                                stake_a = round(STAKE * (1/a) / arb)
                                best = {
                                    "match": match_name,
                                    "sport": sport,
                                    "type": "2-way",
                                    "profit_percent": profit,
                                    "profit_ugx": round(STAKE * (1 - arb)),
                                    "total_stake": STAKE,
                                    "arb_sum": round(arb, 4),
                                    "bets": [
                                        {"bookmaker": bk_h, "outcome": "Home", "odd": h, "stake": stake_h, "win": round(stake_h * h)},
                                        {"bookmaker": bk_a, "outcome": "Away", "odd": a, "stake": stake_a, "win": round(stake_a * a)}
                                    ]
                                }
                if best: opportunities.append(best)

    return opportunities


def send_telegram_alert(opp):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        print("⚠️ Telegram credentials missing – alert not sent.")
        return

    match = opp.get('match', 'Unknown')
    profit = opp.get('profit_percent', 0)
    ugx = opp.get('profit_ugx', 0)
    message = f"⚽ *{match}*\n💰 Profit: *{profit}%* (UGX {ugx:,})\n"
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


def run_scan():
    all_odds = []
    all_odds.extend(scrape_sportybet())
    all_odds.extend(scrape_championbet())
    all_odds.extend(scrape_ababet())
    all_odds.extend(scrape_fortebet())
    all_odds.extend(scrape_1xbet())
    all_odds.extend(scrape_22bet())
    all_odds.extend(scrape_melbet())
    for name, config in SHARED_BOOKMAKERS_1X.items():
        all_odds.extend(scrape_1x_over_under(name, config["base_url"], config["partner"]))
    for name, config in SHARED_BOOKMAKERS_1X.items():
        all_odds.extend(scrape_1x_ah_dc_btts(name, config["base_url"], config["partner"]))

    opportunities = find_arbitrage(all_odds)
    arb_history = load_arbitrage_history()
    timestamp_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    for opp in opportunities:
        key = opportunity_key(opp)
        is_new = (key in arb_history and arb_history[key]['first_seen'] == timestamp_str)
        if is_new and opp.get('profit_percent', 0) >= 5.0:
            send_telegram_alert(opp)

    update_arbitrage_history(opportunities, arb_history, timestamp_str)
    save_arbitrage_history(arb_history)

    if len(opportunities) == 0 and os.path.exists("current_opportunities.json"):
        try:
            with open("current_opportunities.json", "r") as f:
                old_data = json.load(f)
            if old_data:
                print("⚠️ Fallback active: Keeping previous data to prevent app crash.")
                return
        except:
            pass

    with open("current_opportunities.json", "w", encoding="utf-8") as f:
        json.dump(opportunities, f, indent=2)

    print(f"Scan complete: {len(opportunities)} opportunities, history updated.")


# ============================================================================
#                              FLASK BILLING API (PESAPAL)
# ============================================================================

import uuid
import jwt
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from passlib.hash import bcrypt as bcrypt_hash

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///users.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')
app.config['JWT_SECRET'] = os.getenv('JWT_SECRET', 'dev-jwt-secret')
CORS(app)

db = SQLAlchemy(app)

# ---- Tier Configuration ----
TIERS = {
    'free': {
        'label': 'Free Trial',
        'price': 0,
        'duration_days': None,
        'scanner_speed_seconds': 1800,
        'max_profit_percent': 5.0,
        'bookmakers': ['SportyBet', 'ChampionBet', 'AbaBet', 'Fortebet'],
        'market_types': ['1x2'],
        'daily_matches': 3,
        'telegram_alerts': False,
        'historical_data': False,
        'value_rating': 'Poor Value',
    },
    'day': {
        'label': 'Day Pass',
        'price': 2500,
        'duration_days': 1,
        'scanner_speed_seconds': 120,
        'max_profit_percent': 15.0,
        'bookmakers': ['SportyBet', 'ChampionBet', 'AbaBet', 'Fortebet', '1xBet', '22Bet'],
        'market_types': ['1x2', 'Over/Under 2.5'],
        'daily_matches': None,
        'telegram_alerts': False,
        'historical_data': False,
        'value_rating': 'Best Value',
    },
    'monthly': {
        'label': 'Monthly VIP',
        'price': 15000,
        'duration_days': 30,
        'scanner_speed_seconds': 0,
        'max_profit_percent': 50.0,
        'bookmakers': ['SportyBet', 'ChampionBet', 'AbaBet', 'Fortebet', '1xBet', '22Bet', 'Melbet'],
        'market_types': ['1x2', 'Over/Under 2.5', 'Asian Handicap', 'Double Chance', 'BTTS'],
        'daily_matches': None,
        'telegram_alerts': True,
        'historical_data': True,
        'value_rating': 'High Saver',
    },
    'quarterly': {
        'label': 'Quarterly Pro',
        'price': 40000,
        'duration_days': 90,
        'scanner_speed_seconds': 0,
        'max_profit_percent': 50.0,
        'bookmakers': ['SportyBet', 'ChampionBet', 'AbaBet', 'Fortebet', '1xBet', '22Bet', 'Melbet'],
        'market_types': ['1x2', 'Over/Under 2.5', 'Asian Handicap', 'Double Chance', 'BTTS'],
        'daily_matches': None,
        'telegram_alerts': True,
        'historical_data': True,
        'value_rating': 'High Saver',
    }
}

# ---- Database Models ----
class User(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

    tier = db.Column(db.String(20), default='free')
    is_subscribed = db.Column(db.Boolean, default=False)
    subscription_expires = db.Column(db.DateTime, nullable=True)

    last_arbitrage_date = db.Column(db.DateTime, nullable=True)
    arbitrage_today_count = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = bcrypt_hash.hash(password)

    def check_password(self, password):
        return bcrypt_hash.verify(password, self.password_hash)


class Transaction(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('user.id'), nullable=False)
    tx_ref = db.Column(db.String(100), unique=True, nullable=False)   # merchant_reference
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='UGX')
    status = db.Column(db.String(20), default='pending')
    plan = db.Column(db.String(20))          # day, monthly, quarterly
    pesapal_tracking_id = db.Column(db.String(100), nullable=True)   # instead of flutterwave_transaction_id
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()

# ---- JWT Helpers ----
def generate_token(user_id):
    payload = {
        'user_id': user_id,
        'exp': datetime.utcnow() + timedelta(days=7)
    }
    return jwt.encode(payload, os.getenv('JWT_SECRET', 'dev-jwt-secret'), algorithm='HS256')


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token or not token.startswith('Bearer '):
            return jsonify({'error': 'Token missing'}), 401
        token = token.split(' ')[1]
        try:
            data = jwt.decode(token, os.getenv('JWT_SECRET', 'dev-jwt-secret'), algorithms=['HS256'])
            g.user_id = data['user_id']
        except:
            return jsonify({'error': 'Invalid token'}), 401
        return f(*args, **kwargs)
    return decorated


# ---- Auth Endpoints ----
@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json()
    email = data.get('email')
    phone = data.get('phone')
    password = data.get('password')

    if not email or not phone or not password:
        return jsonify({'error': 'Missing fields'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already exists'}), 400

    user = User(email=email, phone=phone, tier='free')
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    token = generate_token(user.id)
    return jsonify({'token': token, 'user_id': user.id}), 201


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({'error': 'Invalid credentials'}), 401

    token = generate_token(user.id)
    return jsonify({
        'token': token,
        'user_id': user.id,
        'tier': user.tier,
        'subscribed': user.is_subscribed,
        'expires': user.subscription_expires.isoformat() if user.subscription_expires else None
    })


# ---- Payment Initiation with Pesapal (lazy import) ----
@app.route('/api/pay', methods=['POST'])
@token_required
def initiate_payment():
    from pesapal_client.client import PesapalClientV3

    user = User.query.get(g.user_id)
    data = request.get_json()
    plan = data.get('plan')

    if plan not in ['day', 'monthly', 'quarterly']:
        return jsonify({'error': 'Invalid plan. Choose: day, monthly, quarterly'}), 400

    amount = TIERS[plan]['price']
    phone = user.phone
    if not phone.startswith('256'):
        phone = '256' + phone.lstrip('0')

    merchant_reference = f"ORDER-{uuid.uuid4().hex[:10].upper()}"

    client = PesapalClientV3(
        consumer_key=os.getenv('PESAPAL_CONSUMER_KEY'),
        consumer_secret=os.getenv('PESAPAL_CONSUMER_SECRET'),
        is_sandbox=os.getenv('PESAPAL_ENVIRONMENT', 'sandbox') == 'sandbox',
    )

    payment_data = {
        "currency": "UGX",
        "amount": amount,
        "description": f"{TIERS[plan]['label']} - {amount} UGX",
        "email": user.email,
        "phone_number": phone,
        "callback_url": os.getenv('PESAPAL_CALLBACK_URL'),
        "merchant_reference": merchant_reference,
    }

    try:
        response = client.one_time_payment.initiate_payment_order(payment_data)
        transaction = Transaction(
            user_id=user.id,
            tx_ref=merchant_reference,
            amount=amount,
            currency='UGX',
            status='pending',
            plan=plan
        )
        db.session.add(transaction)
        db.session.commit()

        return jsonify({
            'redirect_url': response.redirect_url,
            'merchant_reference': merchant_reference,
            'message': 'Redirect user to Pesapal checkout page.'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---- Pesapal IPN (Webhook) ----
@app.route('/pesapal/ipn', methods=['GET', 'POST'])
def pesapal_ipn():
    if request.method == 'POST':
        data = request.form.to_dict() or request.get_json() or {}
    else:
        data = request.args.to_dict()

    merchant_reference = data.get('merchant_reference')
    status = data.get('status')
    tracking_id = data.get('pesapal_transaction_tracking_id')

    if not merchant_reference or not status:
        return 'Missing parameters', 400

    transaction = Transaction.query.filter_by(tx_ref=merchant_reference).first()
    if not transaction:
        return 'Transaction not found', 404

    if transaction.status == 'success':
        return 'Already processed', 200

    if status.upper() != 'COMPLETED':
        transaction.status = 'failed'
        transaction.pesapal_tracking_id = tracking_id
        db.session.commit()
        return 'OK', 200

    # Verify with Pesapal before granting access
    try:
        from pesapal_client.client import PesapalClientV3
        client = PesapalClientV3(
            consumer_key=os.getenv('PESAPAL_CONSUMER_KEY'),
            consumer_secret=os.getenv('PESAPAL_CONSUMER_SECRET'),
            is_sandbox=os.getenv('PESAPAL_ENVIRONMENT', 'sandbox') == 'sandbox',
        )
        txn_status = client.get_transaction_status(
            tracking_id=tracking_id,
            merchant_reference=merchant_reference
        )
        if txn_status.get('payment_status_description', '').upper() != 'COMPLETED':
            transaction.status = 'failed'
            transaction.pesapal_tracking_id = tracking_id
            db.session.commit()
            return 'OK', 200
    except Exception as e:
        transaction.status = 'pending'
        transaction.pesapal_tracking_id = tracking_id
        db.session.commit()
        return 'Internal error', 500

    # Grant access
    transaction.status = 'success'
    transaction.pesapal_tracking_id = tracking_id

    user = User.query.get(transaction.user_id)
    if user:
        plan = transaction.plan
        duration_days = TIERS[plan]['duration_days']
        now = datetime.utcnow()
        if user.subscription_expires and user.subscription_expires > now:
            new_expiry = user.subscription_expires + timedelta(days=duration_days)
        else:
            new_expiry = now + timedelta(days=duration_days)

        user.tier = plan
        user.is_subscribed = True
        user.subscription_expires = new_expiry
        user.arbitrage_today_count = 0
        db.session.commit()

    return 'OK', 200


# ---- Admin Credit Endpoint ----
@app.route('/admin/credit', methods=['POST'])
def admin_credit():
    admin_secret = request.headers.get('X-Admin-Secret')
    if admin_secret != os.getenv('ADMIN_SECRET', 'supersecret'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    user_id = data.get('user_id')
    plan = data.get('plan')
    if not user_id or plan not in ['day', 'monthly', 'quarterly']:
        return jsonify({'error': 'Invalid request'}), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    duration_days = TIERS[plan]['duration_days']
    now = datetime.utcnow()
    if user.subscription_expires and user.subscription_expires > now:
        new_expiry = user.subscription_expires + timedelta(days=duration_days)
    else:
        new_expiry = now + timedelta(days=duration_days)

    user.tier = plan
    user.is_subscribed = True
    user.subscription_expires = new_expiry
    user.arbitrage_today_count = 0

    manual_tx = Transaction(
        user_id=user.id,
        tx_ref=f"MANUAL-{uuid.uuid4().hex[:10].upper()}",
        amount=0,
        currency='UGX',
        status='success',
        plan=plan,
        pesapal_tracking_id='manual'
    )
    db.session.add(manual_tx)
    db.session.commit()

    return jsonify({'message': f'User {user.email} upgraded to {plan} until {new_expiry.isoformat()}'})


def filter_opportunities(opportunities, tier_config):
    allowed_bookmakers = set(tier_config['bookmakers'])
    allowed_markets = set(tier_config['market_types'])
    max_profit = tier_config['max_profit_percent']
    daily_limit = tier_config['daily_matches']

    filtered = []
    for opp in opportunities:
        bets = opp.get('bets', [])
        bookmakers_in_opp = set(b.get('bookmaker') for b in bets)
        if not bookmakers_in_opp.issubset(allowed_bookmakers):
            continue

        market_map = {
            '3-way': '1x2',
            '2-way': '1x2',
            'Over/Under 2.5': 'Over/Under 2.5',
            'Asian Handicap': 'Asian Handicap',
            'Double Chance': 'Double Chance',
            'BTTS': 'BTTS'
        }
        opp_market = market_map.get(opp.get('type', ''), opp.get('type', ''))
        if opp_market not in allowed_markets:
            continue

        if opp.get('profit_percent', 0) > max_profit:
            continue

        filtered.append(opp)

    if daily_limit is not None:
        filtered.sort(key=lambda x: x.get('profit_percent', 0), reverse=True)
        filtered = filtered[:daily_limit]

    return filtered


@app.route('/api/arbitrage', methods=['GET'])
@token_required
def get_arbitrage():
    user = User.query.get(g.user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    if user.tier != 'free' and user.subscription_expires and user.subscription_expires < datetime.utcnow():
        user.is_subscribed = False
        user.tier = 'free'
        db.session.commit()
        return jsonify({'error': 'Subscription expired. Please renew.'}), 403

    if user.tier == 'free':
        today = datetime.utcnow().date()
        if user.last_arbitrage_date:
            last_date = user.last_arbitrage_date.date()
            if last_date != today:
                user.arbitrage_today_count = 0
                user.last_arbitrage_date = datetime.utcnow()
        else:
            user.last_arbitrage_date = datetime.utcnow()

        if user.arbitrage_today_count >= 3:
            return jsonify({
                'error': 'Daily limit reached (3 matches). Upgrade to continue.',
                'tier': 'free'
            }), 403

    tier_config = TIERS[user.tier]
    cache_file = 'current_opportunities.json'
    run_scanner = False

    if os.path.exists(cache_file):
        file_age = time.time() - os.path.getmtime(cache_file)
        if tier_config['scanner_speed_seconds'] == 0:
            run_scanner = True
        elif file_age >= tier_config['scanner_speed_seconds']:
            run_scanner = True
    else:
        run_scanner = True

    if run_scanner:
        print("Running fresh scan... (20-30 sec)")
        try:
            run_scan()
        except Exception as e:
            if not os.path.exists(cache_file):
                return jsonify({'error': 'Scanner failed and no cache available.'}), 500

    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            all_opportunities = json.load(f)
    except:
        all_opportunities = []

    filtered_opps = filter_opportunities(all_opportunities, tier_config)

    if user.tier == 'free':
        user.arbitrage_today_count += 1
        user.last_arbitrage_date = datetime.utcnow()
        db.session.commit()

    history = None
    if tier_config['historical_data']:
        history = load_arbitrage_history()

    response = {
        'opportunities': filtered_opps,
        'count': len(filtered_opps),
        'tier': user.tier,
        'tier_label': tier_config['label'],
        'value_rating': tier_config['value_rating'],
        'scan_time': datetime.utcnow().isoformat(),
        'cached': not run_scanner,
        'remaining_daily': tier_config['daily_matches'] - user.arbitrage_today_count if tier_config['daily_matches'] else None
    }
    if history is not None:
        response['history'] = history

    return jsonify(response)


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTION") == "1" or os.environ.get("CI") == "true":
        print("🚀 Running in GitHub Actions - executing scanner...")
        run_scan()
    else:
        print("🚀 Running locally - starting Flask server...")
        app.run(host='0.0.0.0', port=5000, debug=True)
