# =============================================================================
# CRITICAL: gevent monkey-patch MUST be the very first thing
# =============================================================================
import eventlet
eventlet.monkey_patch()

# =============================================================================
# Standard imports
# =============================================================================
import os
import json
import re
import time
import logging
from datetime import datetime, timedelta
from functools import wraps
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

# Subscription tiers
TIERS = {
    'free': {
        'label': 'Free Trial',
        'price': 0,
        'duration_days': None,
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
        'max_profit_percent': 50.0,
        'bookmakers': ['SportyBet', 'ChampionBet', 'AbaBet', 'Fortebet', '1xBet', '22Bet', 'Melbet'],
        'market_types': ['1x2', 'Over/Under 2.5', 'Asian Handicap', 'Double Chance', 'BTTS'],
        'daily_matches': None,
        'telegram_alerts': True,
        'historical_data': True,
        'value_rating': 'High Saver',
    }
}
PLANS_BY_AMOUNT = {2500: 'day', 15000: 'monthly', 40000: 'quarterly'}

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
        logger.debug(f"ChampionBet top matches: {len(matches)}")
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
    logger.info("Starting scan...")
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

    logger.info("Scan complete.")

# ------------------------------------------------------------------------------
# Flask App (only runs if not in GitHub Actions)
# ------------------------------------------------------------------------------
if os.getenv('GITHUB_ACTIONS') != 'true':
    from flask import Flask, request, jsonify, g
    from flask_cors import CORS
    from flask_sqlalchemy import SQLAlchemy
    from dotenv import load_dotenv
    import bcrypt
    import jwt
    import uuid

    load_dotenv()

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///users.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')
    app.config['JWT_SECRET'] = os.getenv('JWT_SECRET', 'dev-jwt-secret-change-me')

    if 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI']:
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'pool_pre_ping': True,
            'pool_recycle': 300,
            'pool_size': 5,
            'max_overflow': 10
        }

    CORS(app, origins=["*"], supports_credentials=True)

    db = SQLAlchemy(app)

    # --- Database Models ---
    class User(db.Model):
        id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
        email = db.Column(db.String(120), unique=True, nullable=False)
        phone = db.Column(db.String(20), nullable=False)
        password_hash = db.Column(db.String(128), nullable=False)
        tier = db.Column(db.String(20), default='free')
        is_subscribed = db.Column(db.Boolean, default=False)
        subscription_expires = db.Column(db.DateTime, nullable=True)
        free_opportunities_remaining = db.Column(db.Integer, default=0)
        last_free_unlock_at = db.Column(db.DateTime, nullable=True)
        last_arbitrage_date = db.Column(db.DateTime, nullable=True)
        arbitrage_today_count = db.Column(db.Integer, default=0)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

        def set_password(self, password):
            if isinstance(password, str):
                password_bytes = password.encode('utf-8')
            else:
                password_bytes = password
            if len(password_bytes) > 72:
                password_bytes = password_bytes[:72]
            salt = bcrypt.gensalt()
            self.password_hash = bcrypt.hashpw(password_bytes, salt).decode('utf-8')

        def check_password(self, password):
            if isinstance(password, str):
                password_bytes = password.encode('utf-8')
            else:
                password_bytes = password
            if len(password_bytes) > 72:
                password_bytes = password_bytes[:72]
            return bcrypt.checkpw(password_bytes, self.password_hash.encode('utf-8'))

    class Transaction(db.Model):
        id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
        user_id = db.Column(db.String(36), db.ForeignKey('user.id'), nullable=False)
        tx_ref = db.Column(db.String(100), unique=True, nullable=False)
        amount = db.Column(db.Float, nullable=False)
        currency = db.Column(db.String(10), default='UGX')
        status = db.Column(db.String(20), default='pending')
        plan = db.Column(db.String(20))
        manual_transaction_id = db.Column(db.String(100), nullable=True)
        amount_received = db.Column(db.Float, nullable=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- Create tables and admin user on startup ---
    with app.app_context():
        db.create_all()
        logger.info("Database tables created/verified.")

        admin_email = os.getenv('ADMIN_EMAIL')
        if admin_email:
            admin = User.query.filter_by(email=admin_email).first()
            if not admin:
                default_password = "admin123"
                logger.info(f"Creating admin user: {admin_email} with password: {default_password}")
                admin = User(email=admin_email, phone="0000000000", tier='free')
                admin.set_password(default_password)
                db.session.add(admin)
                db.session.commit()
                logger.info("Admin user created.")
            else:
                logger.info(f"Admin user already exists: {admin_email}")
        else:
            logger.warning("ADMIN_EMAIL not set – skipping admin creation.")

    # --- JWT Helpers ---
    ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '')

    def is_admin(user):
        return user.email == ADMIN_EMAIL

    def token_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = request.headers.get('Authorization')
            if not token or not token.startswith('Bearer '):
                return jsonify({'error': 'Token missing'}), 401
            token = token.split(' ')[1]
            try:
                decoded = jwt.decode(token, app.config['JWT_SECRET'], algorithms=['HS256'])
                user = User.query.get(decoded['user_id'])
                if not user:
                    return jsonify({'error': 'User not found'}), 401
            except jwt.ExpiredSignatureError:
                return jsonify({'error': 'Token expired'}), 401
            except jwt.InvalidTokenError:
                return jsonify({'error': 'Invalid token'}), 401
            g.user_id = user.id
            g.user = user
            return f(*args, **kwargs)
        return decorated

    def generate_token(user_id):
        payload = {'user_id': user_id, 'exp': datetime.utcnow() + timedelta(days=30)}
        return jwt.encode(payload, app.config['JWT_SECRET'], algorithm='HS256')

    # ============================================================
    # ROUTES
    # ============================================================
    @app.route('/health', methods=['GET'])
    def health_check():
        return jsonify({'status': 'ok', 'service': 'arbitrage-api', 'timestamp': datetime.utcnow().isoformat()})

    @app.route('/', methods=['GET'])
    def home():
        return jsonify({'status': 'ok', 'service': 'Arbitrage API'})

    @app.route('/api/signup', methods=['POST'])
    def signup():
        try:
            data = request.get_json()
            email = data.get('email')
            phone = data.get('phone', '0000000000')
            password = data.get('password')
            if not email or not password:
                return jsonify({'error': 'Missing required fields (email and password)'}), 400
            if User.query.filter_by(email=email).first():
                return jsonify({'error': 'Email already exists'}), 400
            user = User(email=email, phone=phone, tier='free')
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            token = generate_token(user.id)
            return jsonify({'token': token, 'user_id': user.id}), 201
        except Exception as e:
            logger.error(f"Signup error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @app.route('/api/login', methods=['POST'])
    def login():
        try:
            data = request.get_json()
            email = data.get('email')
            password = data.get('password')
            if not email or not password:
                return jsonify({'error': 'Missing email or password'}), 400
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
        except Exception as e:
            logger.error(f"Login error: {e}")
            return jsonify({'error': 'Internal server error'}), 500

    @app.route('/api/profile', methods=['GET'])
    @token_required
    def get_profile():
        user = g.user
        return jsonify({
            'user': {
                'id': user.id,
                'email': user.email,
                'phone': user.phone,
                'tier': user.tier,
                'is_subscribed': user.is_subscribed,
                'subscription_expires': user.subscription_expires.isoformat() if user.subscription_expires else None
            }
        })

    @app.route('/api/subscription-status', methods=['GET'])
    @token_required
    def subscription_status():
        user = g.user
        if user.subscription_expires and user.subscription_expires < datetime.utcnow():
            user.is_subscribed = False
            user.tier = 'free'
            db.session.commit()
            return jsonify({
                'subscribed': False,
                'tier': 'free',
                'expired': True,
                'days_left': 0,
                'message': 'Your subscription has expired. Please renew.'
            })
        elif user.subscription_expires and user.is_subscribed:
            days_left = (user.subscription_expires - datetime.utcnow()).days
            return jsonify({
                'subscribed': True,
                'tier': user.tier,
                'expired': False,
                'days_left': days_left,
                'message': f'Subscription active for {days_left} more days.'
            })
        else:
            return jsonify({
                'subscribed': False,
                'tier': 'free',
                'expired': False,
                'days_left': 0,
                'message': 'Free Trial active.'
            })

    @app.route('/api/active-plans', methods=['GET'])
    def active_plans():
        plans = []
        labels = {'day': 'Day Pass', 'monthly': 'Monthly VIP', 'quarterly': 'Quarterly Pro'}
        for slug, config in TIERS.items():
            if slug == 'free':
                continue
            plans.append({
                'slug': slug,
                'label': labels.get(slug, slug),
                'price_ugx': config['price'],
                'days': config['duration_days'],
                'formatted': f"UGX {config['price']:,}"
            })
        return jsonify({'plans': plans})

    @app.route('/api/transactions', methods=['GET'])
    @token_required
    def get_transactions():
        user = g.user
        transactions = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.created_at.desc()).all()
        result = []
        for tx in transactions:
            result.append({
                'id': tx.id,
                'tx_ref': tx.tx_ref,
                'amount': tx.amount,
                'currency': tx.currency,
                'status': tx.status,
                'plan': tx.plan,
                'manual_transaction_id': tx.manual_transaction_id,
                'amount_received': tx.amount_received,
                'created_at': tx.created_at.isoformat(),
                'plan_label': TIERS[tx.plan]['label'] if tx.plan in TIERS else tx.plan
            })
        return jsonify({'transactions': result, 'count': len(result)}), 200

    @app.route('/api/transactions', methods=['POST'])
    @token_required
    def create_transaction():
        data = request.get_json()
        user_id = data.get('user_id')
        amount = data.get('amount')
        plan = data.get('plan')
        manual_transaction_id = data.get('manual_transaction_id')
        if not user_id or not amount or not plan or not manual_transaction_id:
            return jsonify({'error': 'Missing fields'}), 400
        if plan not in ['day', 'monthly', 'quarterly']:
            return jsonify({'error': 'Invalid plan'}), 400
        user = User.query.get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        existing = Transaction.query.filter_by(manual_transaction_id=manual_transaction_id, status='success').first()
        if existing:
            return jsonify({'error': 'Transaction ID already used'}), 400
        tx_ref = f"TX-{uuid.uuid4().hex[:10].upper()}"
        transaction = Transaction(
            user_id=user_id,
            tx_ref=tx_ref,
            amount=amount,
            currency='UGX',
            status='pending',
            plan=plan,
            manual_transaction_id=manual_transaction_id
        )
        db.session.add(transaction)
        db.session.commit()
        return jsonify({'id': transaction.id, 'tx_ref': tx_ref, 'status': 'pending'}), 201

    @app.route('/api/initiate-payment', methods=['POST'])
    @token_required
    def initiate_payment():
        user = g.user
        data = request.get_json()
        plan = data.get('plan')
        if not plan or plan not in ['day', 'monthly', 'quarterly']:
            return jsonify({'error': 'Invalid plan'}), 400
        amount = TIERS[plan]['price']
        tx_ref = f"TX-{uuid.uuid4().hex[:10].upper()}"
        existing = Transaction.query.filter_by(user_id=user.id, status='pending').first()
        if existing:
            return jsonify({'error': 'You already have a pending transaction', 'tx_ref': existing.tx_ref}), 409
        transaction = Transaction(
            user_id=user.id,
            tx_ref=tx_ref,
            amount=amount,
            currency='UGX',
            status='pending',
            plan=plan
        )
        db.session.add(transaction)
        db.session.commit()
        return jsonify({
            'tx_ref': tx_ref,
            'amount': amount,
            'plan': plan,
            'plan_label': TIERS[plan]['label'],
            'merchant_phone': os.getenv('MERCHANT_PHONE', '0756408723'),
            'merchant_name': os.getenv('MERCHANT_NAME', 'Nakyanzi Daisy'),
            'instructions': f"Send exactly UGX {amount:,} to {os.getenv('MERCHANT_PHONE', '0756408723')} via Mobile Money."
        }), 201

    @app.route('/api/manual-payment', methods=['POST'])
    @token_required
    def manual_payment():
        user = g.user
        data = request.get_json()
        plan = data.get('plan')
        transaction_id = data.get('transaction_id')
        if not plan or not transaction_id:
            return jsonify({'error': 'Missing fields'}), 400
        if plan not in ['day', 'monthly', 'quarterly']:
            return jsonify({'error': 'Invalid plan'}), 400
        existing = Transaction.query.filter_by(manual_transaction_id=transaction_id, status='success').first()
        if existing:
            return jsonify({'error': 'This transaction ID has already been used.', 'status': 'rejected'}), 400
        amount = TIERS[plan]['price']
        tx_ref = f"KEY-{uuid.uuid4().hex[:10].upper()}"
        transaction = Transaction(
            user_id=user.id,
            tx_ref=tx_ref,
            amount=amount,
            currency='UGX',
            status='pending',
            plan=plan,
            manual_transaction_id=transaction_id
        )
        db.session.add(transaction)
        db.session.commit()
        return jsonify({'status': 'pending', 'message': 'Payment submitted. We will verify when the SMS arrives.', 'transaction_id': tx_ref}), 200

    @app.route('/webhook', methods=['POST'])
    def sms_webhook():
        raw_data = request.get_data(as_text=True)
        logger.info(f"Raw webhook data: {raw_data[:200]}")
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form.to_dict()
        if not data:
            try:
                data = json.loads(raw_data)
            except:
                data = {}
        sms_text = data.get('text') or data.get('body') or data.get('message') or data.get('Body') or data.get('Message')
        sender = data.get('from') or data.get('sender') or data.get('phone_number') or data.get('From') or data.get('Sender')
        if not sms_text or not sender:
            logger.warning(f"Missing SMS data: {data}")
            return 'Missing SMS data', 400

        def extract_transaction_id(text):
            patterns = [
                r'Ref[:\s]+([A-Z0-9\-]+)',
                r'TXN[:\s]+([A-Z0-9\-]+)',
                r'Transaction[:\s]+([A-Z0-9\-]+)',
                r'Reference[:\s]+([A-Z0-9\-]+)'
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    return match.group(1)
            return None

        def extract_amount(text):
            match = re.search(r'UGX\s*([\d,]+\.?\d*)', text, re.IGNORECASE)
            if match:
                return float(match.group(1).replace(',', ''))
            match = re.search(r'([\d,]+\.?\d*)\s*UGX', text, re.IGNORECASE)
            if match:
                return float(match.group(1).replace(',', ''))
            return None

        transaction_id = extract_transaction_id(sms_text)
        amount_received = extract_amount(sms_text)
        if not transaction_id or not amount_received:
            logger.warning(f"Could not parse SMS: {sms_text[:200]}")
            return 'Could not parse SMS', 400

        transaction = Transaction.query.filter_by(manual_transaction_id=transaction_id, status='pending').first()
        if not transaction:
            logger.warning(f"No pending transaction found for ID: {transaction_id}")
            return 'No pending transaction found', 404

        if Transaction.query.filter_by(manual_transaction_id=transaction_id, status='success').first():
            transaction.status = 'failed'
            db.session.commit()
            logger.warning(f"Key {transaction_id} already used.")
            return 'Key already used', 400

        if amount_received not in PLANS_BY_AMOUNT:
            transaction.status = 'failed'
            db.session.commit()
            logger.warning(f"Invalid amount: {amount_received}. Must be 2500, 15000, or 40000.")
            return 'Invalid amount', 400

        plan = PLANS_BY_AMOUNT[amount_received]
        user = User.query.get(transaction.user_id)
        if not user:
            logger.error(f"User not found for transaction {transaction.id}")
            return 'User not found', 404

        duration_days = TIERS[plan]['duration_days']
        now = datetime.utcnow()
        if user.subscription_expires and user.subscription_expires > now:
            new_expiry = user.subscription_expires + timedelta(days=duration_days)
        else:
            new_expiry = now + timedelta(days=duration_days)

        user.tier = plan
        user.is_subscribed = True
        user.subscription_expires = new_expiry
        transaction.status = 'success'
        transaction.amount_received = amount_received
        transaction.plan = plan
        db.session.commit()

        logger.info(f"Subscription activated for {user.email} | Plan: {plan} | Expires: {new_expiry}")
        send_admin_notification(f"✅ Payment confirmed\nUser: {user.email}\nPlan: {plan}\nAmount: {amount_received} UGX\nExpires: {new_expiry}")
        return 'Subscription activated', 200

    def send_admin_notification(message):
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        if not token or not chat_id:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={'chat_id': chat_id, 'text': message, 'parse_mode': 'Markdown'}, timeout=10)
        except Exception as e:
            logger.error(f"Telegram error: {e}")

    @app.route('/api/arbitrage', methods=['GET'])
    @token_required
    def get_arbitrage():
        user = g.user
        tier_config = TIERS[user.tier]
        if user.tier != 'free' and user.subscription_expires and user.subscription_expires < datetime.utcnow():
            user.is_subscribed = False
            user.tier = 'free'
            db.session.commit()
            return jsonify({'error': 'Subscription expired'}), 403

        cache_file = 'current_opportunities.json'
        if not os.path.exists(cache_file):
            return jsonify({'opportunities': [], 'tier': user.tier, 'message': 'No arbitrage data available. Scanner is running.'}), 200

        all_opportunities = []
        for attempt in range(3):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    all_opportunities = json.load(f)
                break
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"File read error (attempt {attempt+1}): {e}")
                time.sleep(0.5)
                continue
        if not all_opportunities:
            all_opportunities = []

        allowed_bookmakers = set(tier_config['bookmakers'])
        allowed_markets = set(tier_config['market_types'])
        max_profit = tier_config['max_profit_percent']
        daily_limit = tier_config['daily_matches']

        filtered = []
        for opp in all_opportunities:
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

        return jsonify({
            'opportunities': filtered,
            'count': len(filtered),
            'tier': user.tier,
            'tier_label': tier_config['label'],
            'value_rating': tier_config.get('value_rating', 'Standard'),
            'scan_time': datetime.utcnow().isoformat()
        })

    @app.route('/api/admin/create-user', methods=['POST'])
    @token_required
    def admin_create_user():
        user = g.user
        if not is_admin(user):
            return jsonify({'error': 'Access denied'}), 403
        data = request.get_json()
        email = data.get('email')
        phone = data.get('phone', '0000000000')
        password = data.get('password')
        tier = data.get('tier', 'free')
        if not email or not password:
            return jsonify({'error': 'Email and password required'}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'User already exists'}), 400
        new_user = User(email=email, phone=phone, tier=tier)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        token = generate_token(new_user.id)
        return jsonify({'message': 'User created', 'token': token, 'user_id': new_user.id, 'tier': new_user.tier}), 201

    @app.route('/api/scan', methods=['POST'])
    @token_required
    def trigger_scan():
        user = g.user
        if not is_admin(user):
            return jsonify({'error': 'Admin only'}), 403
        try:
            run_scan()
            return jsonify({'status': 'Scan completed successfully'}), 200
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/admin/pending-transactions', methods=['GET'])
    @token_required
    def admin_pending_transactions():
        user = g.user
        if not is_admin(user):
            return jsonify({'error': 'Admin access required'}), 403
        pending = Transaction.query.filter_by(status='pending').all()
        result = []
        for tx in pending:
            u = User.query.get(tx.user_id)
            result.append({
                'id': tx.id,
                'user_id': tx.user_id,
                'email': u.email if u else 'Unknown',
                'plan': tx.plan,
                'amount': tx.amount,
                'manual_transaction_id': tx.manual_transaction_id,
                'created_at': tx.created_at.isoformat()
            })
        return jsonify({'transactions': result})

    @app.route('/api/admin/activate', methods=['POST'])
    @token_required
    def admin_activate():
        user = g.user
        if not is_admin(user):
            return jsonify({'error': 'Admin access required'}), 403
        data = request.get_json()
        transaction_id = data.get('transaction_id')
        if not transaction_id:
            return jsonify({'error': 'Missing transaction_id'}), 400
        transaction = Transaction.query.get(transaction_id)
        if not transaction:
            return jsonify({'error': 'Transaction not found'}), 404
        if transaction.status == 'success':
            return jsonify({'error': 'Already activated'}), 400
        plan = transaction.plan
        u = User.query.get(transaction.user_id)
        if not u:
            return jsonify({'error': 'User not found'}), 404
        duration_days = TIERS[plan]['duration_days']
        now = datetime.utcnow()
        if u.subscription_expires and u.subscription_expires > now:
            new_expiry = u.subscription_expires + timedelta(days=duration_days)
        else:
            new_expiry = now + timedelta(days=duration_days)
        u.tier = plan
        u.is_subscribed = True
        u.subscription_expires = new_expiry
        transaction.status = 'success'
        db.session.commit()
        return jsonify({'message': 'Subscription activated successfully'})

    @app.route('/api/admin/activate-by-email', methods=['POST'])
    @token_required
    def admin_activate_by_email():
        user = g.user
        if not is_admin(user):
            return jsonify({'error': 'Admin access required'}), 403
        data = request.get_json()
        email = data.get('email')
        plan = data.get('plan', 'monthly')
        if not email:
            return jsonify({'error': 'Missing email'}), 400
        if plan not in ['day', 'monthly', 'quarterly']:
            return jsonify({'error': 'Invalid plan'}), 400
        u = User.query.filter_by(email=email).first()
        if not u:
            return jsonify({'error': 'User not found'}), 404
        duration_days = TIERS[plan]['duration_days']
        now = datetime.utcnow()
        if u.subscription_expires and u.subscription_expires > now:
            new_expiry = u.subscription_expires + timedelta(days=duration_days)
        else:
            new_expiry = now + timedelta(days=duration_days)
        u.tier = plan
        u.is_subscribed = True
        u.subscription_expires = new_expiry
        tx_ref = f"ADMIN-{uuid.uuid4().hex[:8].upper()}"
        transaction = Transaction(
            user_id=u.id,
            tx_ref=tx_ref,
            amount=TIERS[plan]['price'],
            currency='UGX',
            status='success',
            plan=plan,
            manual_transaction_id=f"ADMIN-{email}",
            amount_received=TIERS[plan]['price']
        )
        db.session.add(transaction)
        db.session.commit()
        return jsonify({'message': f'{email} activated with {plan} plan'})

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'error': 'Endpoint not found'}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({'error': 'Internal server error'}), 500

    # --------------------------------------------------------------------------
    # Run Flask – with background scan on startup (NO SCHEDULER INSIDE)
    # --------------------------------------------------------------------------
    if __name__ == "__main__":
        # ✅ Check for critical environment variables
        if not os.getenv('DATABASE_URL'):
            logger.warning("⚠️ DATABASE_URL is not set – using SQLite (not recommended for production)")
        if not os.getenv('SECRET_KEY'):
            logger.warning("⚠️ SECRET_KEY not set – using default (insecure)")
        if not os.getenv('JWT_SECRET'):
            logger.warning("⚠️ JWT_SECRET not set – using default (insecure)")

        # ✅ Start scanner in background to avoid blocking server startup
        import threading

        def initial_scan():
            try:
                logger.info("🔄 Running initial arbitrage scan in background...")
                run_scan()
                logger.info("✅ Initial scan completed.")
            except Exception as e:
                logger.error(f"❌ Initial scan failed: {e}")

        scan_thread = threading.Thread(target=initial_scan, daemon=True)
        scan_thread.start()

        # ✅ Start the Flask app
        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port)

else:
    # GitHub Actions – only run the scraper (no Flask)
    if __name__ == "__main__":
        run_scan()
