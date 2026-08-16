# =============================================================================
# scraper.py
# Full Flask Application + Arbitrage Scanner + Payments + History + Sitemap
# =============================================================================

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

import bcrypt
import jwt
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_file, Response
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# =============================================================================
# APScheduler for automatic scans
# =============================================================================
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import threading

# =============================================================================
# Environment and logging
# =============================================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("arbitrage_scanner")

# =============================================================================
# Configuration
# =============================================================================

DEFAULT_STAKE = int(os.getenv("DEFAULT_STAKE", "100000"))
HISTORY_FILE = "arb_history.json"
OPPORTUNITIES_FILE = "current_opportunities.json"

SPORTYBET_API = (
    "https://betting-odds-scraper--hkltfsmjgkfde.replit.app"
    "/api/odds/simple"
)

CHAMPIONBET_API = (
    "https://www.championbet.ug/restapi/offer/en/top/mob"
    "?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
)

CHAMPIONBET_MATCH_API = (
    "https://www.championbet.ug/restapi/offer/en/match/{match_id}"
    "?annex=13&mobileVersion=2.47.4.3&locale=en"
)

SHARED_BOOKMAKERS = {
    "1xBet": {
        "base_url": "https://1xbet.ug",
        "partner": "135",
    },
    "22Bet": {
        "base_url": "https://22bet.ug",
        "partner": "151",
    },
    "Melbet": {
        "base_url": "https://melbet.ug",
        "partner": "8",
    },
}

# =============================================================================
# HTTP client
# =============================================================================

class HTTPClient:
    def __init__(self, timeout: int = 30, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 14) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/149.0.7827.159 "
                    "Mobile Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(
            multiplier=1,
            min=2,
            max=10,
        ),
        retry=retry_if_exception_type(
            (
                requests.RequestException,
                ConnectionError,
                TimeoutError,
            )
        ),
    )
    def get_json(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        request_headers = self.session.headers.copy()
        if headers:
            request_headers.update(headers)
        response = self.session.get(
            url,
            headers=request_headers,
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(
            multiplier=1,
            min=2,
            max=10,
        ),
        retry=retry_if_exception_type(
            (
                requests.RequestException,
                ConnectionError,
            )
        ),
    )
    def get_text(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        request_headers = self.session.headers.copy()
        if headers:
            request_headers.update(headers)
        response = self.session.get(
            url,
            headers=request_headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.text


http = HTTPClient()

# =============================================================================
# General helpers
# =============================================================================

def utc_timestamp() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def normalize_team(name: str) -> str:
    if not name:
        return ""

    name = str(name).lower().strip()

    name = re.sub(r"\b(rovers|rvs)\b", "rvs", name)
    name = re.sub(r"\b(united|utd)\b", "utd", name)

    name = re.sub(
        r"\b(fc|sc|cf|ac|city|sports|club|football|soccer|women|men|u21|u23)\b",
        "",
        name,
    )

    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name)

    return name.strip()


def teams_match(name1: str, name2: str) -> bool:
    normalized_one = normalize_team(name1)
    normalized_two = normalize_team(name2)

    if not normalized_one or not normalized_two:
        return False

    if normalized_one == normalized_two:
        return True

    if len(normalized_one) > 3 and len(normalized_two) > 3:
        if normalized_one in normalized_two:
            return True
        if normalized_two in normalized_one:
            return True

        first_word_one = normalized_one.split()[0]
        first_word_two = normalized_two.split()[0]

        if len(first_word_one) > 4 and first_word_one == first_word_two:
            return True

    return False


def match_key_similarity(key1: str, key2: str) -> bool:
    if "|" in key1 or "|" in key2:
        return key1 == key2

    parts_one = key1.split(" vs ")
    parts_two = key2.split(" vs ")

    if len(parts_one) != 2 or len(parts_two) != 2:
        return False

    return (
        teams_match(parts_one[0], parts_two[0])
        and teams_match(parts_one[1], parts_two[1])
    )


def clean_odd(
    value: Any,
    min_odd: float = 1.01,
    max_odd: float = 50.0,
) -> Optional[float]:
    try:
        if value is None:
            return None
        odd = float(value)
        if min_odd <= odd <= max_odd:
            return odd
    except (TypeError, ValueError):
        pass
    return None


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# =============================================================================
# Match record creation
# =============================================================================

def build_match_record(
    home_team: str,
    away_team: str,
    bookmaker: str,
    home: Optional[float],
    draw: Optional[float],
    away: Optional[float],
    sport: str = "Football",
    competition: str = "",
    market_type: str = "1x2",
    market_specifier: str = "",
) -> Dict[str, Any]:
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
        "market_specifier": market_specifier,
    }


# =============================================================================
# History helpers
# =============================================================================

def load_arbitrage_history() -> Dict[str, Any]:
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to load history: %s", exc)
        return {}


def save_arbitrage_history(history: Dict[str, Any]) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as file:
        json.dump(history, file, indent=2)


def opportunity_key(opp: Dict[str, Any]) -> str:
    market_type = opp.get("market_type") or opp.get("type") or "1x2"
    market_specifier = opp.get("market_specifier", "")
    return (
        f"{opp.get('sport', 'Football')}::{market_type}::"
        f"{opp.get('match', '')}::{market_specifier}"
    )


def update_arbitrage_history(
    current: List[Dict[str, Any]],
    history: Dict[str, Any],
    timestamp: str,
) -> None:
    for entry in history.values():
        entry["updated_this_cycle"] = False

    for opportunity in current:
        key = opportunity_key(opportunity)

        if key not in history:
            history[key] = {
                "match": opportunity.get("match", ""),
                "sport": opportunity.get("sport", "Football"),
                "market_type": opportunity.get(
                    "market_type", opportunity.get("type", "1x2")
                ),
                "market_specifier": opportunity.get("market_specifier", ""),
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

        entry.setdefault("versions", []).append(
            {
                "timestamp": timestamp,
                "profit_percent": opportunity.get("profit_percent", 0),
                "profit_ugx": opportunity.get("profit_ugx", 0),
                "arb_sum": opportunity.get("arb_sum", 0),
                "bets": opportunity.get("bets", []),
            }
        )

    for entry in history.values():
        if not entry.get("updated_this_cycle"):
            entry["cycles_missed"] = entry.get("cycles_missed", 0) + 1
            if entry["cycles_missed"] >= 2:
                entry["valid"] = False
        entry.pop("updated_this_cycle", None)


# =============================================================================
# SportyBet scraper
# =============================================================================

def scrape_sportybet() -> List[Dict[str, Any]]:
    logger.info("Fetching SportyBet...")
    odds = []

    try:
        data = http.get_json(SPORTYBET_API)

        if not isinstance(data, list):
            return odds

        for event in data:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            if not home or not away:
                continue

            sport = event.get("sport", "Football")

            home_odd = clean_odd(event.get("home"))
            draw_odd = clean_odd(event.get("draw"))
            away_odd = clean_odd(event.get("away"))

            if home_odd and away_odd:
                odds.append(
                    build_match_record(
                        home,
                        away,
                        "SportyBet",
                        home_odd,
                        draw_odd,
                        away_odd,
                        sport=sport,
                    )
                )

            over_odd = clean_odd(event.get("over_odd"))
            under_odd = clean_odd(event.get("under_odd"))

            if over_odd and under_odd:
                odds.append(
                    build_match_record(
                        home,
                        away,
                        "SportyBet",
                        over_odd,
                        under_odd,
                        None,
                        sport=sport,
                        market_type="Over/Under 2.5",
                    )
                )

        logger.info("SportyBet: %s records", len(odds))

    except Exception as exc:
        logger.error("SportyBet error: %s", exc)

    return odds


# =============================================================================
# ChampionBet scraper
# =============================================================================

def scrape_championbet() -> List[Dict[str, Any]]:
    logger.info("Fetching ChampionBet...")
    odds = []

    try:
        data = http.get_json(CHAMPIONBET_API)

        matches = data.get("esMatches", []) if isinstance(data, dict) else []

        for match in matches:
            try:
                if "Soccer" not in str(match.get("sportToken", "")):
                    continue

                match_id = match.get("id")
                if not match_id:
                    continue

                home = match.get("home", "")
                away = match.get("away", "")
                if not home or not away:
                    continue

                match_url = CHAMPIONBET_MATCH_API.format(match_id=match_id)
                match_data = http.get_json(match_url)

                bet_map = match_data.get("betMap", {}) if isinstance(match_data, dict) else {}

                home_odd, draw_odd, away_odd = extract_championbet_1x2(bet_map)
                if home_odd and away_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "ChampionBet",
                            home_odd,
                            draw_odd,
                            away_odd,
                            competition=match.get("leagueName", ""),
                        )
                    )

                over_odd, under_odd = extract_championbet_ou(bet_map)
                if over_odd and under_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "ChampionBet",
                            over_odd,
                            under_odd,
                            None,
                            market_type="Over/Under 2.5",
                        )
                    )

                ah_odds, dc_odds, btts_odds = extract_championbet_extra(bet_map)

                if ah_odds.get(5) and ah_odds.get(6):
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "ChampionBet",
                            ah_odds[5],
                            None,
                            ah_odds[6],
                            market_type="Asian Handicap",
                            market_specifier="-1.5",
                        )
                    )

                if ah_odds.get(7) and ah_odds.get(8):
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "ChampionBet",
                            ah_odds[7],
                            None,
                            ah_odds[8],
                            market_type="Asian Handicap",
                            market_specifier="-0.5",
                        )
                    )

                if dc_odds.get(20):
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "ChampionBet",
                            dc_odds[20],
                            None,
                            None,
                            market_type="Double Chance",
                            market_specifier="1X",
                        )
                    )

                if dc_odds.get(21):
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "ChampionBet",
                            None,
                            None,
                            dc_odds[21],
                            market_type="Double Chance",
                            market_specifier="X2",
                        )
                    )

                if dc_odds.get(22):
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "ChampionBet",
                            dc_odds[22],
                            None,
                            None,
                            market_type="Double Chance",
                            market_specifier="12",
                        )
                    )

                if btts_odds.get(19) and btts_odds.get(20):
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "ChampionBet",
                            btts_odds[19],
                            None,
                            btts_odds[20],
                            market_type="BTTS",
                        )
                    )

                time.sleep(0.1)

            except Exception:
                logger.exception("ChampionBet match failed")
                continue

        logger.info("ChampionBet: %s records", len(odds))

    except Exception as exc:
        logger.error("ChampionBet error: %s", exc)

    return odds


def extract_championbet_1x2(
    bet_map: Dict[str, Any],
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    def pick(market_keys: List[int]) -> Optional[float]:
        for key in market_keys:
            market = bet_map.get(str(key), {})
            if not isinstance(market, dict):
                continue
            for item in market.values():
                if not isinstance(item, dict):
                    continue
                odd = clean_odd(item.get("ov"))
                if odd is not None:
                    return odd
        return None

    return (pick([1, 4, 7]), pick([2, 5, 8]), pick([3, 6, 9]))


def extract_championbet_ou(
    bet_map: Dict[str, Any],
) -> Tuple[Optional[float], Optional[float]]:
    def pick(market_keys: List[int]) -> Optional[float]:
        for key in market_keys:
            market = bet_map.get(str(key), {})
            if not isinstance(market, dict):
                continue
            for item in market.values():
                if not isinstance(item, dict):
                    continue
                odd = clean_odd(item.get("ov"))
                if odd is not None:
                    return odd
        return None

    return pick([51, 21]), pick([52, 22])


def extract_championbet_extra(
    bet_map: Dict[str, Any],
) -> Tuple[Dict[int, float], Dict[int, float], Dict[int, float]]:
    asian_handicap = {}
    double_chance = {}
    btts = {}

    for key in [5, 6, 7, 8]:
        market = bet_map.get(str(key), {})
        if not isinstance(market, dict):
            continue
        for item in market.values():
            if not isinstance(item, dict):
                continue
            odd = clean_odd(item.get("ov"))
            if odd is not None:
                asian_handicap[key] = odd

    for key in [20, 21, 22]:
        market = bet_map.get(str(key), {})
        if not isinstance(market, dict):
            continue
        for item in market.values():
            if not isinstance(item, dict):
                continue
            odd = clean_odd(item.get("ov"))
            if odd is not None:
                double_chance[key] = odd

    for key in [19, 20]:
        market = bet_map.get(str(key), {})
        if not isinstance(market, dict):
            continue
        for item in market.values():
            if not isinstance(item, dict):
                continue
            odd = clean_odd(item.get("ov"))
            if odd is not None:
                btts[key] = odd

    return asian_handicap, double_chance, btts


# =============================================================================
# AbaBet scraper
# =============================================================================

def scrape_ababet() -> List[Dict[str, Any]]:
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

            headers = [cell.get_text(" ", strip=True) for cell in first_row.find_all(["th", "td"])]

            if "Home" not in headers or "Away" not in headers:
                continue

            for row_element in table.find_all("tr")[1:]:
                cells = [cell.get_text(" ", strip=True) for cell in row_element.find_all(["td", "th"])]

                if len(cells) < 5:
                    continue

                row = dict(zip(headers, cells[: len(headers)]))

                home = row.get("Home")
                away = row.get("Away")

                if not home or not away or home == "-" or away == "-":
                    continue

                home_odd = clean_odd(row.get("1"))
                draw_odd = clean_odd(row.get("X"))
                away_odd = clean_odd(row.get("2"))

                if home_odd and away_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "AbaBet",
                            home_odd,
                            draw_odd,
                            away_odd,
                            competition=row.get("League", ""),
                        )
                    )

                over_odd = clean_odd(row.get("Over"))
                under_odd = clean_odd(row.get("Under"))

                if over_odd and under_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "AbaBet",
                            over_odd,
                            under_odd,
                            None,
                            market_type="Over/Under 2.5",
                        )
                    )

        logger.info("AbaBet: %s records", len(odds))

    except Exception as exc:
        logger.error("AbaBet error: %s", exc)

    return odds


# =============================================================================
# Fortebet scraper
# =============================================================================

def scrape_fortebet() -> List[Dict[str, Any]]:
    logger.info("Fetching Fortebet...")
    odds = []

    try:
        url = "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en"
        data = http.get_json(
            url,
            headers={"Referer": "https://desktop.fortebet.ug/prematch/landing"},
        )

        inner = data.get("data", {})
        events = inner.get("event", {})
        markets = inner.get("markets", {})
        competitors = inner.get("competitors", {})

        event_markets = {}

        for market in markets.values():
            event_id = str(market.get("eventId", ""))
            event_markets.setdefault(event_id, []).append(market)

        for event_id, event in events.items():
            try:
                competitor_ids = event.get("competitors", [])
                if len(competitor_ids) < 2:
                    continue

                home = competitors.get(str(competitor_ids[0]), {}).get("name", "")
                away = competitors.get(str(competitor_ids[1]), {}).get("name", "")
                if not home or not away:
                    continue

                home_odd = None
                draw_odd = None
                away_odd = None
                over_odd = None
                under_odd = None
                ah_home = None
                ah_away = None
                dc_home = None
                dc_away = None
                btts_yes = None
                btts_no = None

                for market in event_markets.get(str(event_id), []):
                    market_id = market.get("marketId")
                    market_odds = market.get("odds", {})

                    if market_id == 1:
                        odd_list = []
                        for value in market_odds.values():
                            if not isinstance(value, dict):
                                continue
                            if "odds" not in value:
                                continue
                            odd = clean_odd(value.get("odds"))
                            if odd is not None:
                                odd_list.append((value.get("outcomeId", 0), odd))
                        odd_list.sort(key=lambda item: item[0])
                        if len(odd_list) >= 3:
                            home_odd = odd_list[0][1]
                            draw_odd = odd_list[1][1]
                            away_odd = odd_list[2][1]
                        elif len(odd_list) == 2:
                            home_odd = odd_list[0][1]
                            away_odd = odd_list[1][1]

                    elif market_id == 5:
                        for value in market_odds.values():
                            if not isinstance(value, dict):
                                continue
                            odd = clean_odd(value.get("odds"))
                            if odd is None:
                                continue
                            outcome_id = value.get("outcomeId")
                            if outcome_id == 1:
                                over_odd = odd
                            elif outcome_id == 2:
                                under_odd = odd

                    elif market_id == 2:
                        for value in market_odds.values():
                            if not isinstance(value, dict):
                                continue
                            odd = clean_odd(value.get("odds"))
                            if odd is None:
                                continue
                            outcome_id = value.get("outcomeId")
                            if outcome_id == 1:
                                ah_home = odd
                            elif outcome_id == 2:
                                ah_away = odd

                    elif market_id == 8:
                        for value in market_odds.values():
                            if not isinstance(value, dict):
                                continue
                            odd = clean_odd(value.get("odds"))
                            if odd is None:
                                continue
                            outcome_id = value.get("outcomeId")
                            if outcome_id == 1:
                                dc_home = odd
                            elif outcome_id == 3:
                                dc_away = odd

                    elif market_id == 12:
                        for value in market_odds.values():
                            if not isinstance(value, dict):
                                continue
                            odd = clean_odd(value.get("odds"))
                            if odd is None:
                                continue
                            outcome_id = value.get("outcomeId")
                            if outcome_id == 1:
                                btts_yes = odd
                            elif outcome_id == 2:
                                btts_no = odd

                event_sport = str(event.get("sportName") or event.get("sport") or "").lower()
                if "basketball" in event_sport:
                    sport_name = "Basketball"
                elif "tennis" in event_sport:
                    sport_name = "Tennis"
                elif draw_odd is None:
                    sport_name = "Netball"
                else:
                    sport_name = "Football"

                if home_odd and away_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "Fortebet",
                            home_odd,
                            draw_odd,
                            away_odd,
                            sport=sport_name,
                        )
                    )

                if over_odd and under_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "Fortebet",
                            over_odd,
                            under_odd,
                            None,
                            sport=sport_name,
                            market_type="Over/Under 2.5",
                        )
                    )

                if ah_home and ah_away:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "Fortebet",
                            ah_home,
                            None,
                            ah_away,
                            sport=sport_name,
                            market_type="Asian Handicap",
                            market_specifier="-0.5",
                        )
                    )

                if dc_home and dc_away:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "Fortebet",
                            dc_home,
                            None,
                            dc_away,
                            sport=sport_name,
                            market_type="Double Chance",
                            market_specifier="1X",
                        )
                    )

                if btts_yes and btts_no:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "Fortebet",
                            btts_yes,
                            None,
                            btts_no,
                            sport=sport_name,
                            market_type="BTTS",
                        )
                    )

            except Exception:
                logger.exception("Fortebet event failed")
                continue

        logger.info("Fortebet: %s records", len(odds))

    except Exception as exc:
        logger.error("Fortebet error: %s", exc)

    return odds


# =============================================================================
# Shared bookmaker scrapers
# =============================================================================

def scrape_1xbet() -> List[Dict[str, Any]]:
    config = SHARED_BOOKMAKERS["1xBet"]
    return scrape_shared_1x_like("1xBet", config["base_url"], config["partner"])


def scrape_22bet() -> List[Dict[str, Any]]:
    config = SHARED_BOOKMAKERS["22Bet"]
    return scrape_shared_1x_like("22Bet", config["base_url"], config["partner"])


def scrape_melbet() -> List[Dict[str, Any]]:
    config = SHARED_BOOKMAKERS["Melbet"]
    return scrape_shared_1x_like("Melbet", config["base_url"], config["partner"])


def scrape_shared_1x_like(
    bookmaker: str,
    base_url: str,
    partner: str,
) -> List[Dict[str, Any]]:
    logger.info("Fetching %s...", bookmaker)
    odds = []

    try:
        url = (
            f"{base_url}/service-api/LineFeed/Get1x2_VZip"
            "?sports=1&count=1000&lng=en&mode=4&country=191"
            f"&partner={partner}&getEmpty=true&virtualSports=true"
        )

        data = http.get_json(url)

        values = data.get("Value", []) if isinstance(data, dict) else []

        for match in values:
            home = match.get("O1", "")
            away = match.get("O2", "")

            if not home or not away:
                continue
            if home.strip() == "Home" and away.strip() == "Away":
                continue

            home_odd = None
            draw_odd = None
            away_odd = None

            for outcome in match.get("E", []):
                outcome_type = str(outcome.get("T", "")).strip()
                odd = clean_odd(outcome.get("C"))
                if odd is None:
                    continue

                if outcome_type == "1":
                    home_odd = odd
                elif outcome_type == "2":
                    away_odd = odd
                elif outcome_type == "3":
                    draw_odd = odd

            if home_odd is not None and away_odd is not None:
                odds.append(
                    build_match_record(
                        home,
                        away,
                        bookmaker,
                        home_odd,
                        draw_odd,
                        away_odd,
                    )
                )

        logger.info("%s: %s records", bookmaker, len(odds))

    except Exception as exc:
        logger.error("%s error: %s", bookmaker, exc)

    return odds


def scrape_shared_extra_markets() -> List[Dict[str, Any]]:
    all_odds = []

    for bookmaker, config in SHARED_BOOKMAKERS.items():
        base_url = config["base_url"]
        partner = config["partner"]

        # Over/Under 2.5
        try:
            url = (
                f"{base_url}/service-api/LineFeed/GetEvents_VZip"
                "?count=1000&lng=en&mode=4&country=191"
                f"&partner={partner}&market=5,6&getEmpty=true&virtualSports=true&eventType=1"
            )
            data = http.get_json(url)

            for match in data.get("Value", []):
                home = match.get("O1", "")
                away = match.get("O2", "")
                if not home or not away:
                    continue

                over_odd = None
                under_odd = None

                for outcome in match.get("E", []):
                    outcome_type = str(outcome.get("T", "")).strip()
                    odd = clean_odd(outcome.get("C"))
                    if odd is None:
                        continue

                    if outcome_type == "5":
                        over_odd = odd
                    elif outcome_type == "6":
                        under_odd = odd

                if over_odd and under_odd:
                    all_odds.append(
                        build_match_record(
                            home,
                            away,
                            bookmaker,
                            over_odd,
                            under_odd,
                            None,
                            market_type="Over/Under 2.5",
                        )
                    )
        except Exception as exc:
            logger.error("%s Over/Under error: %s", bookmaker, exc)

        # Asian Handicap, Double Chance, BTTS
        try:
            url = (
                f"{base_url}/service-api/LineFeed/Get1x2_VZip"
                "?sports=1&count=1000&lng=en&mode=4&country=191"
                f"&partner={partner}&getEmpty=true"
            )
            data = http.get_json(url)

            for match in data.get("Value", []):
                home = match.get("O1", "")
                away = match.get("O2", "")
                if not home or not away:
                    continue
                if home.strip() == "Home" and away.strip() == "Away":
                    continue

                ah_home = None
                ah_away = None
                dc_home = None
                dc_away = None
                btts_yes = None
                btts_no = None

                for outcome in match.get("E", []):
                    outcome_type = str(outcome.get("T", "")).strip()
                    odd = clean_odd(outcome.get("C"))
                    if odd is None:
                        continue

                    specifier = outcome.get("P")

                    if outcome_type == "7" and specifier is not None:
                        ah_home = odd
                    elif outcome_type == "8" and specifier is not None:
                        ah_away = odd
                    elif outcome_type in {"4", "180"}:
                        dc_home = odd
                    elif outcome_type == "181":
                        dc_away = odd
                    elif outcome_type == "19":
                        btts_yes = odd
                    elif outcome_type == "20":
                        btts_no = odd

                if ah_home and ah_away:
                    all_odds.append(
                        build_match_record(
                            home,
                            away,
                            bookmaker,
                            ah_home,
                            None,
                            ah_away,
                            market_type="Asian Handicap",
                            market_specifier="-0.5",
                        )
                    )

                if dc_home and dc_away:
                    all_odds.append(
                        build_match_record(
                            home,
                            away,
                            bookmaker,
                            dc_home,
                            None,
                            dc_away,
                            market_type="Double Chance",
                            market_specifier="1X",
                        )
                    )

                if btts_yes and btts_no:
                    all_odds.append(
                        build_match_record(
                            home,
                            away,
                            bookmaker,
                            btts_yes,
                            None,
                            btts_no,
                            market_type="BTTS",
                        )
                    )
        except Exception as exc:
            logger.error("%s extra markets error: %s", bookmaker, exc)

    return all_odds


# =============================================================================
# Arbitrage finder
# =============================================================================

def create_two_outcome_opportunity(
    match: str,
    sport: str,
    market_type: str,
    market_specifier: str,
    first_bookmaker: str,
    first_outcome: str,
    first_odd: float,
    second_bookmaker: str,
    second_outcome: str,
    second_odd: float,
    stake: int = DEFAULT_STAKE,
) -> Optional[Dict[str, Any]]:
    if not first_odd or not second_odd:
        return None

    arb_sum = (1 / first_odd) + (1 / second_odd)
    if arb_sum >= 1:
        return None

    profit_percent = round((1 - arb_sum) * 100, 2)
    if not 0.5 <= profit_percent <= 50.0:
        return None

    first_stake = round(stake * (1 / first_odd) / arb_sum)
    second_stake = round(stake * (1 / second_odd) / arb_sum)

    return {
        "match": match,
        "sport": sport,
        "type": market_type,
        "market_type": market_type,
        "market_specifier": market_specifier,
        "profit_percent": profit_percent,
        "profit_ugx": round(stake * (1 - arb_sum)),
        "total_stake": stake,
        "arb_sum": round(arb_sum, 4),
        "bets": [
            {
                "bookmaker": first_bookmaker,
                "outcome": first_outcome,
                "odd": first_odd,
                "stake": first_stake,
                "win": round(first_stake * first_odd),
            },
            {
                "bookmaker": second_bookmaker,
                "outcome": second_outcome,
                "odd": second_odd,
                "stake": second_stake,
                "win": round(second_stake * second_odd),
            },
        ],
    }


def create_three_outcome_opportunity(
    match: str,
    sport: str,
    first_bookmaker: str,
    first_odd: float,
    draw_bookmaker: str,
    draw_odd: float,
    second_bookmaker: str,
    second_odd: float,
    stake: int = DEFAULT_STAKE,
) -> Optional[Dict[str, Any]]:
    if not first_odd or not draw_odd or not second_odd:
        return None

    arb_sum = (1 / first_odd) + (1 / draw_odd) + (1 / second_odd)
    if arb_sum >= 1:
        return None

    profit_percent = round((1 - arb_sum) * 100, 2)
    if not 0.5 <= profit_percent <= 50.0:
        return None

    first_stake = round(stake * (1 / first_odd) / arb_sum)
    draw_stake = round(stake * (1 / draw_odd) / arb_sum)
    second_stake = round(stake * (1 / second_odd) / arb_sum)

    return {
        "match": match,
        "sport": sport,
        "type": "3-way",
        "market_type": "1x2",
        "market_specifier": "",
        "profit_percent": profit_percent,
        "profit_ugx": round(stake * (1 - arb_sum)),
        "total_stake": stake,
        "arb_sum": round(arb_sum, 4),
        "bets": [
            {
                "bookmaker": first_bookmaker,
                "outcome": "Home",
                "odd": first_odd,
                "stake": first_stake,
                "win": round(first_stake * first_odd),
            },
            {
                "bookmaker": draw_bookmaker,
                "outcome": "Draw",
                "odd": draw_odd,
                "stake": draw_stake,
                "win": round(draw_stake * draw_odd),
            },
            {
                "bookmaker": second_bookmaker,
                "outcome": "Away",
                "odd": second_odd,
                "stake": second_stake,
                "win": round(second_stake * second_odd),
            },
        ],
    }


def find_arbitrage(all_odds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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

        for index, first_key in enumerate(keys):
            if first_key in processed:
                continue

            group = list(groups[first_key])
            processed.add(first_key)

            for second_key in keys[index + 1:]:
                if second_key in processed:
                    continue
                if match_key_similarity(first_key, second_key):
                    group.extend(groups[second_key])
                    processed.add(second_key)

            merged[first_key] = group

        for match_key, bookmaker_records in merged.items():
            if len(bookmaker_records) < 2:
                continue

            first_record = bookmaker_records[0]
            market_type = first_record.get("market_type", "1x2")
            market_specifier = first_record.get("market_specifier", "")

            bookmaker_odds = {}

            for record in bookmaker_records:
                bookmaker = record["bookmaker"]
                bookmaker_odds.setdefault(
                    bookmaker,
                    {"home": 0.0, "draw": 0.0, "away": 0.0},
                )

                home_odd = clean_odd(record.get("home"))
                draw_odd = clean_odd(record.get("draw"))
                away_odd = clean_odd(record.get("away"))

                if home_odd is not None and home_odd > bookmaker_odds[bookmaker]["home"]:
                    bookmaker_odds[bookmaker]["home"] = home_odd
                if draw_odd is not None and draw_odd > bookmaker_odds[bookmaker]["draw"]:
                    bookmaker_odds[bookmaker]["draw"] = draw_odd
                if away_odd is not None and away_odd > bookmaker_odds[bookmaker]["away"]:
                    bookmaker_odds[bookmaker]["away"] = away_odd

            bookmakers = list(bookmaker_odds.keys())
            display_match = match_key.split(" | ")[0] if " | " in match_key else match_key

            # Two-outcome markets
            if market_type in {"Over/Under 2.5", "Asian Handicap", "Double Chance", "BTTS"}:
                for index, bookmaker_one in enumerate(bookmakers):
                    for bookmaker_two in bookmakers[index + 1:]:
                        first_home = bookmaker_odds[bookmaker_one]["home"]
                        first_away = bookmaker_odds[bookmaker_one]["away"]
                        second_home = bookmaker_odds[bookmaker_two]["home"]
                        second_away = bookmaker_odds[bookmaker_two]["away"]

                        candidates = []
                        if first_home and second_away:
                            candidates.append(
                                (first_home, second_away, bookmaker_one, bookmaker_two)
                            )
                        if second_home and first_away:
                            candidates.append(
                                (second_home, first_away, bookmaker_two, bookmaker_one)
                            )

                        for (first_odd, second_odd, first_bm, second_bm) in candidates:
                            if market_type == "Over/Under 2.5":
                                first_outcome = "Over 2.5"
                                second_outcome = "Under 2.5"
                            elif market_type == "Asian Handicap":
                                first_outcome = f"AH {market_specifier} (Home)"
                                second_outcome = f"AH {market_specifier} (Away)"
                            elif market_type == "BTTS":
                                first_outcome = "BTTS Yes"
                                second_outcome = "BTTS No"
                            elif market_type == "Double Chance":
                                first_outcome = "Outcome 1"
                                second_outcome = "Outcome 2"
                                if market_specifier == "1X":
                                    first_outcome = "1X"
                                    second_outcome = "X2"
                                elif market_specifier == "12":
                                    first_outcome = "12"
                                    second_outcome = "12 (other)"
                            else:
                                first_outcome = "Outcome 1"
                                second_outcome = "Outcome 2"

                            opportunity = create_two_outcome_opportunity(
                                display_match,
                                sport,
                                market_type,
                                market_specifier,
                                first_bm,
                                first_outcome,
                                first_odd,
                                second_bm,
                                second_outcome,
                                second_odd,
                                stake=DEFAULT_STAKE,
                            )
                            if opportunity:
                                opportunities.append(opportunity)

            # Three-outcome markets (1x2 for football, rugby, futsal)
            elif market_type == "1x2" and sport in {"Football", "Rugby", "Futsal"}:
                for home_bookmaker in bookmakers:
                    for draw_bookmaker in bookmakers:
                        for away_bookmaker in bookmakers:
                            if len({home_bookmaker, draw_bookmaker, away_bookmaker}) < 3:
                                continue

                            home_odd = bookmaker_odds[home_bookmaker]["home"]
                            draw_odd = bookmaker_odds[draw_bookmaker]["draw"]
                            away_odd = bookmaker_odds[away_bookmaker]["away"]

                            opportunity = create_three_outcome_opportunity(
                                display_match,
                                sport,
                                home_bookmaker,
                                home_odd,
                                draw_bookmaker,
                                draw_odd,
                                away_bookmaker,
                                away_odd,
                                stake=DEFAULT_STAKE,
                            )
                            if opportunity:
                                opportunities.append(opportunity)

            # Two-outcome sports (tennis, basketball, etc.)
            elif market_type == "1x2" and sport not in {"Football", "Rugby", "Futsal"}:
                for home_bookmaker in bookmakers:
                    for away_bookmaker in bookmakers:
                        if home_bookmaker == away_bookmaker:
                            continue
                        home_odd = bookmaker_odds[home_bookmaker]["home"]
                        away_odd = bookmaker_odds[away_bookmaker]["away"]

                        opportunity = create_two_outcome_opportunity(
                            display_match,
                            sport,
                            "2-way",
                            "",
                            home_bookmaker,
                            "Home",
                            home_odd,
                            away_bookmaker,
                            "Away",
                            away_odd,
                            stake=DEFAULT_STAKE,
                        )
                        if opportunity:
                            opportunities.append(opportunity)

    # ---------------------------------------------------------------
    # DEDUPLICATE: keep only the highest-profit opportunity per match
    # ---------------------------------------------------------------
    best_by_match = {}

    for opp in opportunities:
        match_key = opp.get("match", "")
        profit = opp.get("profit_percent", 0)

        if match_key not in best_by_match:
            best_by_match[match_key] = opp
        else:
            if profit > best_by_match[match_key].get("profit_percent", 0):
                best_by_match[match_key] = opp

    opportunities = list(best_by_match.values())

    return opportunities


# =============================================================================
# Telegram alerts
# =============================================================================

def send_telegram_alert(opportunity: Dict[str, Any]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    match = opportunity.get("match", "Unknown")
    profit = opportunity.get("profit_percent", 0)
    profit_ugx = opportunity.get("profit_ugx", 0)

    lines = [
        f"⚽ *{match}*",
        f"💰 Profit: *{profit}%* (UGX {profit_ugx:,})",
    ]

    for bet in opportunity.get("bets", []):
        bookmaker = bet.get("bookmaker", "Unknown")
        outcome = bet.get("outcome", "Unknown")
        odd = bet.get("odd", 0)
        stake = bet.get("stake", 0)
        lines.append(
            f"▶ {bookmaker} ({outcome}) @ {odd} - Stake: UGX {stake:,}"
        )

    message = "\n".join(lines)

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Telegram alert sent for %s", match)
    except Exception as exc:
        logger.error("Telegram error: %s", exc)


# =============================================================================
# Scanner
# =============================================================================

def run_scan() -> List[Dict[str, Any]]:
    logger.info("Starting arbitrage scan...")

    all_odds = []

    scrapers = [
        scrape_sportybet,
        scrape_championbet,
        scrape_ababet,
        scrape_fortebet,
        scrape_1xbet,
        scrape_22bet,
        scrape_melbet,
        scrape_shared_extra_markets,
    ]

    for scraper in scrapers:
        try:
            all_odds.extend(scraper())
        except Exception as exc:
            logger.exception("Scraper failed: %s", exc)

    opportunities = find_arbitrage(all_odds)
    logger.info("Found %s arbitrage opportunities", len(opportunities))

    history = load_arbitrage_history()
    timestamp = utc_timestamp()

    for opportunity in opportunities:
        key = opportunity_key(opportunity)
        if key not in history:
            if opportunity.get("profit_percent", 0) >= 5.0:
                send_telegram_alert(opportunity)

    update_arbitrage_history(opportunities, history, timestamp)
    save_arbitrage_history(history)

    with open(OPPORTUNITIES_FILE, "w", encoding="utf-8") as file:
        json.dump(opportunities, file, indent=2)

    logger.info("Scan complete. Output written to %s", OPPORTUNITIES_FILE)
    return opportunities


# =============================================================================
# Flask application
# =============================================================================

app = Flask(__name__)

database_url = os.getenv("DATABASE_URL", "sqlite:///users.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-in-production")

db = SQLAlchemy(app)

frontend_origin = os.getenv("FRONTEND_ORIGIN", "*")
CORS(
    app,
    resources={r"/api/*": {"origins": frontend_origin}},
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)


# =============================================================================
# Database models
# =============================================================================

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    subscription_status = db.Column(db.String(50), default="free", nullable=False)
    subscription_expires_at = db.Column(db.DateTime, nullable=True)

    def set_password(self, password: str) -> None:
        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        self.password_hash = hashed.decode("utf-8")

    def check_password(self, password: str) -> bool:
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                self.password_hash.encode("utf-8"),
            )
        except (TypeError, ValueError):
            return False


class CompletedArb(db.Model):
    __tablename__ = "completed_arbs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    match = db.Column(db.String(255), nullable=False)
    profit = db.Column(db.Float, default=0.0, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    plan = db.Column(db.String(50), nullable=False)
    transaction_id = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    approved_at = db.Column(db.DateTime, nullable=True)


with app.app_context():
    db.create_all()


# =============================================================================
# Authentication helpers
# =============================================================================

def serialize_user(user: User) -> Dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "subscription_status": user.subscription_status,
        "subscription_expires_at": (
            user.subscription_expires_at.isoformat()
            if user.subscription_expires_at
            else None
        ),
    }


def generate_token(user_id: int) -> str:
    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(days=7),
    }
    return jwt.encode(payload, app.config["SECRET_KEY"], algorithm="HS256")


def token_required(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return jsonify({"ok": False, "error": "Missing authentication token"}), 401
        token = authorization[7:].strip()
        if not token:
            return jsonify({"ok": False, "error": "Missing authentication token"}), 401
        try:
            payload = jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])
            g.user_id = int(payload["user_id"])
        except jwt.ExpiredSignatureError:
            return jsonify({"ok": False, "error": "Token has expired"}), 401
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid authentication token"}), 401
        return function(*args, **kwargs)
    return decorated


def subscription_required(function):
    @wraps(function)
    @token_required
    def decorated(*args, **kwargs):
        user = db.session.get(User, g.user_id)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404

        now = datetime.utcnow()
        if (
            user.subscription_status == "free"
            or not user.subscription_expires_at
            or user.subscription_expires_at < now
        ):
            return jsonify({
                "ok": False,
                "error": "Active subscription required",
                "code": "SUBSCRIPTION_REQUIRED"
            }), 403

        return function(*args, **kwargs)
    return decorated


# =============================================================================
# Health and preflight
# =============================================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "service": "Arbitrage Scanner",
        "time": datetime.utcnow().isoformat(),
    })


@app.route("/api/<path:path>", methods=["OPTIONS"])
def api_options(path: str):
    return ("", 204)


# =============================================================================
# Authentication routes
# =============================================================================

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    name = str(data.get("name", "")).strip()

    if not email or not password:
        return jsonify({"ok": False, "error": "Email and password are required"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Password must contain at least 6 characters"}), 400

    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({"ok": False, "error": "Email already registered"}), 409

    user = User(
        email=email,
        name=name or None,
        subscription_status="free",
    )
    user.set_password(password)

    try:
        db.session.add(user)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.exception("Signup database error: %s", exc)
        return jsonify({"ok": False, "error": "Unable to create account"}), 500

    return jsonify({
        "ok": True,
        "token": generate_token(user.id),
        "user": serialize_user(user),
    }), 201


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    if not email or not password:
        return jsonify({"ok": False, "error": "Email and password are required"}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401

    return jsonify({
        "ok": True,
        "token": generate_token(user.id),
        "user": serialize_user(user),
    })


@app.route("/api/me", methods=["GET"])
@token_required
def get_current_user():
    user = db.session.get(User, g.user_id)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    return jsonify({"ok": True, "user": serialize_user(user)})


# =============================================================================
# Arbitrage routes
# =============================================================================

@app.route("/api/arbs", methods=["GET"])
@subscription_required
def get_arbitrage_opportunities():
    try:
        with open(OPPORTUNITIES_FILE, "r", encoding="utf-8") as file:
            opportunities = json.load(file)
    except FileNotFoundError:
        opportunities = []
    except json.JSONDecodeError:
        logger.exception("Invalid opportunities JSON")
        opportunities = []
    return jsonify({"ok": True, "arbs": opportunities, "count": len(opportunities)})


@app.route("/api/history", methods=["GET"])
@token_required
def get_history():
    json_history = load_arbitrage_history()
    db_history = CompletedArb.query.filter_by(
        user_id=g.user_id
    ).order_by(CompletedArb.timestamp.desc()).all()

    merged = []

    for key, entry in json_history.items():
        merged.append({
            "id": key,
            "match": entry.get("match", "Unknown"),
            "profit_ugx": entry.get("profit_ugx", 0),
            "timestamp": entry.get("last_seen", ""),
            "valid": entry.get("valid", False),
            "versions": entry.get("versions", [])
        })

    for entry in db_history:
        merged.append({
            "id": f"db_{entry.id}",
            "match": entry.match,
            "profit_ugx": entry.profit,
            "timestamp": entry.timestamp.isoformat(),
            "valid": True,
            "source": "completed_arb"
        })

    merged.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify({"ok": True, "history": merged})


@app.route("/api/scan", methods=["POST"])
@token_required
def trigger_scan():
    try:
        opportunities = run_scan()
        return jsonify({"ok": True, "message": "Scan completed", "count": len(opportunities)})
    except Exception as exc:
        logger.exception("Manual scan failed: %s", exc)
        return jsonify({"ok": False, "error": "Scan failed"}), 500


# =============================================================================
# Complete Arb endpoint
# =============================================================================

@app.route("/api/complete", methods=["POST"])
@token_required
def complete_arb():
    try:
        data = request.get_json() or {}
        match = data.get("match", "Unknown")
        profit = data.get("profit", 0.0)

        record = CompletedArb(
            user_id=g.user_id,
            match=match,
            profit=profit,
        )
        db.session.add(record)
        db.session.commit()
        return jsonify({"ok": True, "message": "Arbitrage recorded successfully"})
    except Exception as exc:
        db.session.rollback()
        logger.exception("Failed to record completed arb: %s", exc)
        return jsonify({"ok": False, "error": "Failed to record arbitrage"}), 500


# =============================================================================
# Payment endpoint
# =============================================================================

@app.route("/api/payments", methods=["POST"])
@token_required
def submit_payment():
    try:
        data = request.get_json() or {}
        plan = data.get("plan")
        transaction_id = data.get("transaction_id")

        if not plan or not transaction_id:
            return jsonify({"ok": False, "error": "Missing plan or transaction ID"}), 400

        existing = Payment.query.filter_by(transaction_id=transaction_id).first()
        if existing:
            return jsonify({"ok": False, "error": "This transaction ID has already been used"}), 409

        payment = Payment(
            user_id=g.user_id,
            plan=plan,
            transaction_id=transaction_id,
            status="pending"
        )
        db.session.add(payment)
        db.session.commit()

        return jsonify({
            "ok": True,
            "message": "Payment submitted for review. Your account will be upgraded once the admin verifies the transaction."
        })
    except Exception as exc:
        db.session.rollback()
        logger.exception("Payment submission failed: %s", exc)
        return jsonify({"ok": False, "error": "Payment submission failed"}), 500


# =============================================================================
# Sitemap and robots.txt - placed BEFORE fallback
# =============================================================================

@app.route("/sitemap.xml")
def sitemap():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://abrt-scraper-1-51d7.onrender.com/</loc>
    <lastmod>2026-08-16</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>'''
    return Response(xml, mimetype='application/xml')


@app.route("/robots.txt")
def robots():
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Sitemap: https://abrt-scraper-1-51d7.onrender.com/sitemap.xml\n"
    )
    return Response(content, mimetype='text/plain')


# =============================================================================
# Frontend serving
# =============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, "index.html")


@app.route("/", methods=["GET"])
def serve_frontend():
    if not os.path.exists(INDEX_FILE):
        return jsonify({"ok": False, "error": "index.html was not found"}), 404
    return send_file(INDEX_FILE)


# Fallback route must come LAST
@app.route("/<path:path>", methods=["GET"])
def frontend_fallback(path: str):
    if path.startswith("api/"):
        return jsonify({"ok": False, "error": "API route not found"}), 404
    if not os.path.exists(INDEX_FILE):
        return jsonify({"ok": False, "error": "index.html was not found"}), 404
    return send_file(INDEX_FILE)


# =============================================================================
# Background Scheduler (automated scanning every 2 minutes)
# =============================================================================

_scheduler_started = False
_scheduler_lock = threading.Lock()


def start_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            func=run_scan,
            trigger=IntervalTrigger(minutes=2),
            id="arbitrage_scanner",
            replace_existing=True,
        )
        scheduler.start()
        _scheduler_started = True
        logger.info("Background scheduler started – scanning every 2 minutes.")


# Start scheduler immediately (works under Gunicorn)
start_scheduler()


# =============================================================================
# Application startup (for local development)
# =============================================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
