# =============================================================================
# scraper.py – Arbitrage Scanner Module
# =============================================================================
import os
import json
import re
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ------------------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("arbitrage_scanner")

# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------
STAKE = 100000
HISTORY_FILE = "arb_history.json"
SPORTYBET_API = "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"
CHAMPIONBET_API = "https://www.championbet.ug/restapi/offer/en/top/mob?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
CHAMPIONBET_MATCH_API = "https://www.championbet.ug/restapi/offer/en/match/{match_id}?annex=13&mobileVersion=2.47.4.3&locale=en"

SHARED_BOOKMAKERS = {
    "1xBet": {"base_url": "https://1xbet.ug", "partner": "135"},
    "22Bet": {"base_url": "https://22bet.ug", "partner": "151"},
    "Melbet": {"base_url": "https://melbet.ug", "partner": "8"},
}

# ------------------------------------------------------------------------------
# HTTP Client with retry
# ------------------------------------------------------------------------------
class HTTPClient:
    def __init__(self, timeout=30, retries=3):
        self.session = requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; TECNO BG6m) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.RequestException, ConnectionError, TimeoutError))
    )
    def get_json(self, url, headers=None, params=None) -> Any:
        req_headers = self.session.headers.copy()
        if headers:
            req_headers.update(headers)
        resp = self.session.get(url, headers=req_headers, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.RequestException, ConnectionError))
    )
    def get_text(self, url, headers=None) -> str:
        req_headers = self.session.headers.copy()
        if headers:
            req_headers.update(headers)
        resp = self.session.get(url, headers=req_headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

http = HTTPClient()

# ------------------------------------------------------------------------------
# Team name normalization & matching
# ------------------------------------------------------------------------------
def normalize_team(name: str) -> str:
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r"\b(rovers|rvs)\b", "rvs", name)
    name = re.sub(r"\b(united|utd)\b", "utd", name)
    name = re.sub(r"\b(fc|sc|cf|ac|city|sports|club|football|soccer|women|men|u21|u23)\b", "", name)
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def teams_match(name1: str, name2: str) -> bool:
    n1 = normalize_team(name1)
    n2 = normalize_team(name2)
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

def match_key_similarity(key1: str, key2: str) -> bool:
    if "|" in key1 or "|" in key2:
        return key1 == key2
    parts1 = key1.split(" vs ")
    parts2 = key2.split(" vs ")
    if len(parts1) != 2 or len(parts2) != 2:
        return False
    return teams_match(parts1[0], parts2[0]) and teams_match(parts1[1], parts2[1])

# ------------------------------------------------------------------------------
# Record building
# ------------------------------------------------------------------------------
def clean_odd(v, min_odd=1.01, max_odd=50.0) -> Optional[float]:
    try:
        if v is None:
            return None
        v = float(v)
        if min_odd <= v <= max_odd:
            return v
    except (ValueError, TypeError):
        pass
    return None

def build_match_record(home_team: str, away_team: str, bookmaker: str,
                       home: Optional[float], draw: Optional[float], away: Optional[float],
                       sport: str = "Football", competition: str = "",
                       market_type: str = "1x2", market_specifier: str = "") -> Dict:
    base_key = f"{normalize_team(home_team)} vs {normalize_team(away_team)}"
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

# ------------------------------------------------------------------------------
# Arbitrage History helpers
# ------------------------------------------------------------------------------
def load_arbitrage_history() -> Dict:
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load history: {e}")
        return {}

def save_arbitrage_history(history: Dict) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

def opportunity_key(opp: Dict) -> str:
    mtype = opp.get('market_type', '1x2')
    spec = opp.get('market_specifier', '')
    return f"{opp['sport']}::{mtype}::{opp['match']}::{spec}"

def update_arbitrage_history(current: List[Dict], history: Dict, timestamp: str) -> None:
    for entry in history.values():
        entry["updated_this_cycle"] = False
    for opp in current:
        key = opportunity_key(opp)
        if key not in history:
            history[key] = {
                "match": opp["match"],
                "sport": opp["sport"],
                "market_type": opp.get("market_type", "1x2"),
                "market_specifier": opp.get("market_specifier", ""),
                "first_seen": timestamp,
                "last_seen": timestamp,
                "valid": True,
                "cycles_missed": 0,
                "versions": [],
            }
        entry = history[key]
        entry["last_seen"] = timestamp
        entry["valid"] = True
        entry["cycles_missed"] = 0
        entry["updated_this_cycle"] = True
        entry["versions"].append({
            "timestamp": timestamp,
            "profit_percent": opp["profit_percent"],
            "profit_ugx": opp["profit_ugx"],
            "arb_sum": opp["arb_sum"],
            "bets": opp["bets"],
        })
    for key, entry in list(history.items()):
        if not entry.get("updated_this_cycle"):
            entry["cycles_missed"] = entry.get("cycles_missed", 0) + 1
            if entry["cycles_missed"] >= 2:
                entry["valid"] = False
        entry.pop("updated_this_cycle", None)

# ------------------------------------------------------------------------------
# SCRAPER FUNCTIONS
# ------------------------------------------------------------------------------
def scrape_sportybet() -> List[Dict]:
    logger.info("Fetching SportyBet...")
    try:
        data = http.get_json(SPORTYBET_API)
        odds = []
        if isinstance(data, list):
            for event in data:
                home = event.get("home_team", "")
                away = event.get("away_team", "")
                if not home or not away:
                    continue
                sport = event.get("sport", "Football")
                h = clean_odd(event.get("home"))
                d = clean_odd(event.get("draw"))
                a = clean_odd(event.get("away"))
                if h and a:
                    odds.append(build_match_record(home, away, "SportyBet", h, d, a, sport=sport))
                over = clean_odd(event.get("over_odd"))
                under = clean_odd(event.get("under_odd"))
                if over and under:
                    odds.append(build_match_record(home, away, "SportyBet", over, under, None, sport=sport, market_type="Over/Under 2.5"))
        logger.info(f"SportyBet: {len(odds)} records")
        return odds
    except Exception as e:
        logger.error(f"SportyBet error: {e}")
        return []

def scrape_championbet() -> List[Dict]:
    logger.info("Fetching ChampionBet...")
    odds = []
    try:
        data = http.get_json(CHAMPIONBET_API)
        matches = data.get("esMatches", []) if isinstance(data, dict) else []
        for m in matches:
            try:
                if "Soccer" not in str(m.get("sportToken", "")):
                    continue
                match_id = m.get("id")
                if not match_id:
                    continue
                home = m.get("home", "")
                away = m.get("away", "")
                if not home or not away:
                    continue
                match_data = http.get_json(CHAMPIONBET_MATCH_API.format(match_id=match_id))
                bet_map = match_data.get("betMap", {}) if isinstance(match_data, dict) else {}
                h, d, a = _extract_championbet_1x2(bet_map)
                if h and a:
                    odds.append(build_match_record(home, away, "ChampionBet", h, d, a, competition=m.get("leagueName", "")))
                over, under = _extract_championbet_ou(bet_map)
                if over and under:
                    odds.append(build_match_record(home, away, "ChampionBet", over, under, None, market_type="Over/Under 2.5"))
                ah_odds, dc_odds, btts_odds = _extract_championbet_extra(bet_map)
                if ah_odds.get(5) and ah_odds.get(6):
                    odds.append(build_match_record(home, away, "ChampionBet", ah_odds[5], None, ah_odds[6], market_type="Asian Handicap", market_specifier="-1.5"))
                if ah_odds.get(7) and ah_odds.get(8):
                    odds.append(build_match_record(home, away, "ChampionBet", ah_odds[7], None, ah_odds[8], market_type="Asian Handicap", market_specifier="-0.5"))
                if dc_odds.get(20):
                    odds.append(build_match_record(home, away, "ChampionBet", dc_odds[20], None, None, market_type="Double Chance", market_specifier="1X"))
                if dc_odds.get(21):
                    odds.append(build_match_record(home, away, "ChampionBet", None, None, dc_odds[21], market_type="Double Chance", market_specifier="X2"))
                if dc_odds.get(22):
                    odds.append(build_match_record(home, away, "ChampionBet", dc_odds[22], None, None, market_type="Double Chance", market_specifier="12"))
                if btts_odds.get(19) and btts_odds.get(20):
                    odds.append(build_match_record(home, away, "ChampionBet", btts_odds[19], None, btts_odds[20], market_type="BTTS"))
                time.sleep(0.1)
            except Exception:
                continue
        logger.info(f"ChampionBet: {len(odds)} records")
    except Exception as e:
        logger.error(f"ChampionBet error: {e}")
    return odds

def _extract_championbet_1x2(bet_map: Dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    def _pick(market_keys):
        for k in market_keys:
            market = bet_map.get(str(k), {})
            if not isinstance(market, dict):
                continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None:
                        return odd
        return None
    return _pick([1,4,7]), _pick([2,5,8]), _pick([3,6,9])

def _extract_championbet_ou(bet_map: Dict) -> Tuple[Optional[float], Optional[float]]:
    def _pick(market_keys):
        for k in market_keys:
            market = bet_map.get(str(k), {})
            if not isinstance(market, dict):
                continue
            for _, item in market.items():
                if isinstance(item, dict):
                    odd = clean_odd(item.get("ov"))
                    if odd is not None:
                        return odd
        return None
    return _pick([51,21]), _pick([52,22])

def _extract_championbet_extra(bet_map: Dict) -> Tuple[Dict, Dict, Dict]:
    ah, dc, btts = {}, {}, {}
    for k in [5,6,7,8]:
        market = bet_map.get(str(k), {})
        if not isinstance(market, dict):
            continue
        for _, item in market.items():
            if isinstance(item, dict):
                odd = clean_odd(item.get("ov"))
                if odd is not None:
                    ah[k] = odd
    for k in [20,21,22]:
        market = bet_map.get(str(k), {})
        if not isinstance(market, dict):
            continue
        for _, item in market.items():
            if isinstance(item, dict):
                odd = clean_odd(item.get("ov"))
                if odd is not None:
                    dc[k] = odd
    for k in [19,20]:
        market = bet_map.get(str(k), {})
        if not isinstance(market, dict):
            continue
        for _, item in market.items():
            if isinstance(item, dict):
                odd = clean_odd(item.get("ov"))
                if odd is not None:
                    btts[k] = odd
    return ah, dc, btts

def scrape_ababet() -> List[Dict]:
    logger.info("Fetching AbaBet...")
    odds = []
    try:
        html = http.get_text("https://www.ababet.ug/soccer/match_result?mobile=1")
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            logger.warning("AbaBet: no tables found")
            return odds
        for table in tables:
            first_row = table.find("tr")
            if not first_row:
                continue
            headers = [c.get_text(" ", strip=True) for c in first_row.find_all(["th", "td"])]
            if "Home" not in headers or "Away" not in headers:
                continue
            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 5:
                    continue
                row = dict(zip(headers, cells[:len(headers)]))
                home, away = row.get("Home"), row.get("Away")
                if not home or not away or home == "-" or away == "-":
                    continue
                h = clean_odd(row.get("1"))
                d = clean_odd(row.get("X"))
                a = clean_odd(row.get("2"))
                if h and a:
                    odds.append(build_match_record(home, away, "AbaBet", h, d, a, competition=row.get("League", "")))
                over = clean_odd(row.get("Over"))
                under = clean_odd(row.get("Under"))
                if over and under:
                    odds.append(build_match_record(home, away, "AbaBet", over, under, None, market_type="Over/Under 2.5"))
        logger.info(f"AbaBet: {len(odds)} records")
    except Exception as e:
        logger.error(f"AbaBet error: {e}")
    return odds

def scrape_fortebet() -> List[Dict]:
    logger.info("Fetching Fortebet API...")
    odds = []
    try:
        url = "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en"
        data = http.get_json(url, headers={"Referer": "https://desktop.fortebet.ug/prematch/landing"})
        inner = data.get("data", {})
        events = inner.get("event", {})
        markets = inner.get("markets", {})
        competitors = inner.get("competitors", {})
        event_markets = {}
        for _, market in markets.items():
            eid = str(market.get("eventId", ""))
            event_markets.setdefault(eid, []).append(market)

        for eid, event in events.items():
            try:
                comps = event.get("competitors", [])
                if len(comps) < 2:
                    continue
                home = competitors.get(str(comps[0]), {}).get("name", "")
                away = competitors.get(str(comps[1]), {}).get("name", "")
                if not home or not away:
                    continue
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
                        for _, v in market.get("odds", {}).items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1:
                                    over = clean_odd(v["odds"])
                                elif oid == 2:
                                    under = clean_odd(v["odds"])
                    elif mid == 2:
                        for _, v in market.get("odds", {}).items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1:
                                    ah_home = clean_odd(v["odds"])
                                elif oid == 2:
                                    ah_away = clean_odd(v["odds"])
                    elif mid == 8:
                        for _, v in market.get("odds", {}).items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1:
                                    dc_home = clean_odd(v["odds"])
                                elif oid == 3:
                                    dc_away = clean_odd(v["odds"])
                    elif mid == 12:
                        for _, v in market.get("odds", {}).items():
                            if isinstance(v, dict) and "odds" in v:
                                oid = v.get("outcomeId", 0)
                                if oid == 1:
                                    btts_yes = clean_odd(v["odds"])
                                elif oid == 2:
                                    btts_no = clean_odd(v["odds"])
                if h and a:
                    sport_name = "Netball" if d is None else "Football"
                    ev_sport = (event.get("sportName") or event.get("sport") or "").lower()
                    if "basketball" in ev_sport:
                        sport_name = "Basketball"
                    elif "tennis" in ev_sport:
                        sport_name = "Tennis"
                    odds.append(build_match_record(home, away, "Fortebet", h, d, a, sport=sport_name))
                if over and under:
                    odds.append(build_match_record(home, away, "Fortebet", over, under, None, market_type="Over/Under 2.5"))
                if ah_home and ah_away:
                    odds.append(build_match_record(home, away, "Fortebet", ah_home, None, ah_away, market_type="Asian Handicap", market_specifier="-0.5"))
                if dc_home:
                    odds.append(build_match_record(home, away, "Fortebet", dc_home, None, None, market_type="Double Chance", market_specifier="1X"))
                if dc_away:
                    odds.append(build_match_record(home, away, "Fortebet", None, None, dc_away, market_type="Double Chance", market_specifier="12"))
                if btts_yes and btts_no:
                    odds.append(build_match_record(home, away, "Fortebet", btts_yes, None, btts_no, market_type="BTTS"))
            except Exception:
                continue
        logger.info(f"Fortebet: {len(odds)} records")
    except Exception as e:
        logger.error(f"Fortebet error: {e}")
    return odds

def scrape_1xbet() -> List[Dict]:
    return _scrape_shared_1x_like("1xBet", SHARED_BOOKMAKERS["1xBet"]["base_url"], SHARED_BOOKMAKERS["1xBet"]["partner"])

def scrape_22bet() -> List[Dict]:
    return _scrape_shared_1x_like("22Bet", SHARED_BOOKMAKERS["22Bet"]["base_url"], SHARED_BOOKMAKERS["22Bet"]["partner"])

def scrape_melbet() -> List[Dict]:
    return _scrape_shared_1x_like("Melbet", SHARED_BOOKMAKERS["Melbet"]["base_url"], SHARED_BOOKMAKERS["Melbet"]["partner"])

def _scrape_shared_1x_like(name: str, base_url: str, partner: str) -> List[Dict]:
    logger.info(f"Fetching {name}...")
    odds = []
    try:
        url = f"{base_url}/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner={partner}&getEmpty=true&virtualSports=true"
        data = http.get_json(url)
        values = data.get("Value", []) if isinstance(data, dict) else []
        for match in values:
            home = match.get("O1", "")
            away = match.get("O2", "")
            if not home or not away:
                continue
            if home.strip() == "Home" and away.strip() == "Away":
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
                odds.append(build_match_record(home, away, name, h, d, a))
        logger.info(f"{name}: {len(odds)} records")
    except Exception as e:
        logger.error(f"{name} error: {e}")
    return odds

def scrape_shared_extra_markets() -> List[Dict]:
    all_odds = []
    for name, config in SHARED_BOOKMAKERS.items():
        # Over/Under
        try:
            url = f"{config['base_url']}/service-api/LineFeed/GetEvents_VZip?count=1000&lng=en&mode=4&country=191&partner={config['partner']}&market=5,6&getEmpty=true&virtualSports=true&eventType=1"
            data = http.get_json(url)
            for match in data.get("Value", []):
                home = match.get("O1", "")
                away = match.get("O2", "")
                if not home or not away:
                    continue
                over = under = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if not c:
                        continue
                    if t == "5":
                        over = c
                    elif t == "6":
                        under = c
                if over and under:
                    all_odds.append(build_match_record(home, away, name, over, under, None, market_type="Over/Under 2.5"))
        except Exception as e:
            logger.error(f"{name} Over/Under error: {e}")

        # AH, DC, BTTS
        try:
            url = f"{config['base_url']}/service-api/LineFeed/Get1x2_VZip?sports=1&count=1000&lng=en&mode=4&country=191&partner={config['partner']}&getEmpty=true"
            data = http.get_json(url)
            for match in data.get("Value", []):
                home = match.get("O1", "")
                away = match.get("O2", "")
                if not home or not away:
                    continue
                if home.strip() == "Home" and away.strip() == "Away":
                    continue
                ah_home = ah_away = None
                dc_home = dc_away = None
                btts_yes = btts_no = None
                for e in match.get("E", []):
                    t = str(e.get("T", "")).strip()
                    c = clean_odd(e.get("C"))
                    if not c:
                        continue
                    p = e.get("P")
                    if t == "7" and p is not None:
                        ah_home = c
                    elif t == "8" and p is not None:
                        ah_away = c
                    elif t == "4" or t == "180":
                        dc_home = c
                    elif t == "181":
                        dc_away = c
                    elif t == "19":
                        btts_yes = c
                    elif t == "20":
                        btts_no = c
                if ah_home and ah_away:
                    all_odds.append(build_match_record(home, away, name, ah_home, None, ah_away, market_type="Asian Handicap", market_specifier="-0.5"))
                if dc_home and dc_away:
                    all_odds.append(build_match_record(home, away, name, dc_home, None, dc_away, market_type="Double Chance", market_specifier="1X"))
                if btts_yes and btts_no:
                    all_odds.append(build_match_record(home, away, name, btts_yes, None, btts_no, market_type="BTTS"))
        except Exception as e:
            logger.error(f"{name} extra markets error: {e}")
    return all_odds

# ------------------------------------------------------------------------------
# Arbitrage Finder
# ------------------------------------------------------------------------------
def find_arbitrage(all_odds: List[Dict]) -> List[Dict]:
    opportunities = []
    sports = {}
    for odd in all_odds:
        sport = odd.get("sport", "Football")
        sports.setdefault(sport, []).append(odd)

    for sport, sport_odds in sports.items():
        groups = {}
        for odd in sport_odds:
            key = odd.get("match_key", "")
            groups.setdefault(key, []).append(odd)

        merged = {}
        processed = set()
        keys = list(groups.keys())
        for i, k1 in enumerate(keys):
            if k1 in processed:
                continue
            group = list(groups[k1])
            processed.add(k1)
            for k2 in keys[i+1:]:
                if k2 in processed:
                    continue
                if match_key_similarity(k1, k2):
                    group.extend(groups[k2])
                    processed.add(k2)
            merged[k1] = group

        for match_key, bookmakers in merged.items():
            if len(bookmakers) < 2:
                continue
            first = bookmakers[0]
            mtype = first.get("market_type", "1x2")
            spec = first.get("market_specifier", "")

            bookmaker_odds = {}
            for rec in bookmakers:
                bk = rec["bookmaker"]
                if bk not in bookmaker_odds:
                    bookmaker_odds[bk] = {"home": 0.0, "draw": 0.0, "away": 0.0}
                h = clean_odd(rec.get("home"))
                d = clean_odd(rec.get("draw"))
                a = clean_odd(rec.get("away"))
                if h is not None and h > bookmaker_odds[bk]["home"]:
                    bookmaker_odds[bk]["home"] = h
                if d is not None and d > bookmaker_odds[bk]["draw"]:
                    bookmaker_odds[bk]["draw"] = d
                if a is not None and a > bookmaker_odds[bk]["away"]:
                    bookmaker_odds[bk]["away"] = a

            bk_list = list(bookmaker_odds.keys())
            display_match = match_key.split(" | ")[0] if " | " in match_key else match_key

            if mtype in ["Over/Under 2.5", "Asian Handicap", "Double Chance", "BTTS"]:
                for i, bk1 in enumerate(bk_list):
                    for bk2 in bk_list[i+1:]:
                        h1 = bookmaker_odds[bk1]["home"]
                        a1 = bookmaker_odds[bk1]["away"]
                        h2 = bookmaker_odds[bk2]["home"]
                        a2 = bookmaker_odds[bk2]["away"]
                        candidates = []
                        if h1 and a2:
                            candidates.append((h1, a2, bk1, bk2))
                        if h2 and a1:
                            candidates.append((h2, a1, bk2, bk1))
                        if not candidates:
                            continue
                        for over, under, bk_over, bk_under in candidates:
                            arb = (1/over) + (1/under)
                            if arb >= 1:
                                continue
                            profit = round((1 - arb) * 100, 2)
                            if 0.5 <= profit <= 50.0:
                                stake_over = round(STAKE * (1/over) / arb)
                                stake_under = round(STAKE * (1/under) / arb)
                                opp = {
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
                                    opp["bets"][0]["outcome"] = "Over 2.5"
                                    opp["bets"][1]["outcome"] = "Under 2.5"
                                elif mtype == "Asian Handicap":
                                    opp["bets"][0]["outcome"] = f"AH {spec} (Home)"
                                    opp["bets"][1]["outcome"] = f"AH {spec} (Away)"
                                elif mtype == "Double Chance":
                                    if spec == "1X":
                                        opp["bets"][0]["outcome"] = "1X"
                                        opp["bets"][1]["outcome"] = "X2"
                                    elif spec == "12":
                                        opp["bets"][0]["outcome"] = "12"
                                        opp["bets"][1]["outcome"] = "12 (other)"
                                elif mtype == "BTTS":
                                    opp["bets"][0]["outcome"] = "BTTS Yes"
                                    opp["bets"][1]["outcome"] = "BTTS No"
                                opportunities.append(opp)
                                break

            elif mtype == "1x2" and sport in ["Football", "Rugby", "Futsal"]:
                for bk_h in bk_list:
                    for bk_d in bk_list:
                        for bk_a in bk_list:
                            if len({bk_h, bk_d, bk_a}) < 3:
                                continue
                            h = bookmaker_odds[bk_h]["home"]
                            d = bookmaker_odds[bk_d]["draw"]
                            a = bookmaker_odds[bk_a]["away"]
                            if not h or not d or not a:
                                continue
                            arb = (1/h) + (1/d) + (1/a)
                            if arb < 1:
                                profit = round((1 - arb) * 100, 2)
                                if 0.5 <= profit <= 50.0:
                                    stake_h = round(STAKE * (1/h) / arb)
                                    stake_d = round(STAKE * (1/d) / arb)
                                    stake_a = round(STAKE * (1/a) / arb)
                                    opp = {
                                        "match": display_match,
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
                                    opportunities.append(opp)

            elif mtype == "1x2" and sport not in ["Football", "Rugby", "Futsal"]:
                for bk_h in bk_list:
                    for bk_a in bk_list:
                        if bk_h == bk_a:
                            continue
                        h = bookmaker_odds[bk_h]["home"]
                        a = bookmaker_odds[bk_a]["away"]
                        if not h or not a:
                            continue
                        arb = (1/h) + (1/a)
                        if arb < 1:
                            profit = round((1 - arb) * 100, 2)
                            if 0.5 <= profit <= 50.0:
                                stake_h = round(STAKE * (1/h) / arb)
                                stake_a = round(STAKE * (1/a) / arb)
                                opp = {
                                    "match": display_match,
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
                                opportunities.append(opp)

    return opportunities

# ------------------------------------------------------------------------------
# Telegram Alert
# ------------------------------------------------------------------------------
def send_telegram_alert(opp: Dict) -> None:
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
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
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={'chat_id': chat, 'text': message, 'parse_mode': 'Markdown'},
            timeout=10
        )
        logger.info(f"Telegram alert sent for {match}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ------------------------------------------------------------------------------
# Main Scanner
# ------------------------------------------------------------------------------
def run_scan() -> None:
    logger.info("Starting arbitrage scan...")
    all_odds = []
    all_odds.extend(scrape_sportybet())
    all_odds.extend(scrape_championbet())
    all_odds.extend(scrape_ababet())
    all_odds.extend(scrape_fortebet())
    all_odds.extend(scrape_1xbet())
    all_odds.extend(scrape_22bet())
    all_odds.extend(scrape_melbet())
    all_odds.extend(scrape_shared_extra_markets())

    opportunities = find_arbitrage(all_odds)
    logger.info(f"Found {len(opportunities)} arbitrage opportunities")

    history = load_arbitrage_history()
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    for opp in opportunities:
        key = opportunity_key(opp)
        if key not in history or history[key]['first_seen'] == timestamp:
            if opp.get('profit_percent', 0) >= 5.0:
                send_telegram_alert(opp)

    update_arbitrage_history(opportunities, history, timestamp)
    save_arbitrage_history(history)

    with open("current_opportunities.json", "w", encoding="utf-8") as f:
        json.dump(opportunities, f, indent=2)

    logger.info("Scan complete. Output written to current_opportunities.json")

# ------------------------------------------------------------------------------
# If run standalone
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    run_scan()
