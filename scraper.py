# =============================================================================
# scanner.py – Full Arbitrage Scanner with All Bookmakers
# =============================================================================

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv()

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("arbitrage_scanner")


# =============================================================================
# Configuration helpers
# =============================================================================

def env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            logger.warning("Invalid integer environment variable %s=%r; using %s", name, raw, default)
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_float(name: str, default: float, minimum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw)
        except ValueError:
            logger.warning("Invalid float environment variable %s=%r; using %s", name, raw, default)
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    logger.warning("Invalid boolean environment variable %s=%r; using %s", name, raw, default)
    return default


DEFAULT_STAKE = env_int("DEFAULT_STAKE", 100000, minimum=1)

HISTORY_FILE = os.getenv("HISTORY_FILE", "arb_history.json")
OPPORTUNITIES_FILE = os.getenv("OPPORTUNITIES_FILE", "current_opportunities.json")
SCANNER_STATUS_FILE = os.getenv("SCANNER_STATUS_FILE", "scanner_status.json")

MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN = env_int("MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN", 2, minimum=1)
ZERO_RECORDS_ARE_ERRORS = env_bool("ZERO_RECORDS_ARE_ERRORS", True)
MAX_HISTORY_VERSIONS = env_int("MAX_HISTORY_VERSIONS", 100, minimum=1)
HTTP_TIMEOUT = env_int("HTTP_TIMEOUT", 30, minimum=1)
MAX_HTML_PAGES = env_int("MAX_HTML_PAGES", 10, minimum=1)

MIN_ARB_PROFIT_PERCENT = env_float("MIN_ARB_PROFIT_PERCENT", 0.1, minimum=0.0)
MAX_ARB_PROFIT_PERCENT = env_float("MAX_ARB_PROFIT_PERCENT", 50.0, minimum=MIN_ARB_PROFIT_PERCENT)

WARN_PROFIT_PERCENT = env_float("WARN_PROFIT_PERCENT", 15.0, minimum=MIN_ARB_PROFIT_PERCENT)

MAX_ODDS_AGE_MINUTES = env_int("MAX_ODDS_AGE_MINUTES", 10, minimum=0)

TELEGRAM_MIN_PROFIT = env_float("TELEGRAM_MIN_PROFIT", 5.0, minimum=0.0)


# =============================================================================
# Bookmaker API constants
# =============================================================================

SPORTYBET_API = (
    "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/"
    "api/odds/simple"
)

CHAMPIONBET_API = (
    "https://www.championbet.ug/restapi/offer/en/top/mob"
    "?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
)

CHAMPIONBET_MATCH_API = (
    "https://www.championbet.ug/restapi/offer/en/match/{match_id}"
    "?annex=13&mobileVersion=2.47.4.3&locale=en"
)

CHAMPIONBET_TOP_API = (
    "https://www.championbet.ug/restapi/offer/en/top/mob"
    "?annex=13&mobileVersion=2.47.4.6&locale=en"
)

KBET_API_BASE = "https://kbet.ug/api/events"

SHARED_BOOKMAKERS = {
    "1xBet": {
        "base_url": "https://1xbet.ug",
        "partner": "135",
        "lng": "en",
        "tz": 3,
        "gr": 525,
        "referer": "https://1xbet.ug/line",
    },
    "22Bet": {
        "base_url": "https://22bet.ug",
        "partner": "151",
        "lng": "en_GB",
        "tz": 3,
        "gr": 525,
        "referer": "https://22bet.ug/line",
    },
    "Melbet": {
        "base_url": "https://melbet.ug",
        "partner": "263",
        "lng": "en",
        "tz": 3,
        "gr": 525,
        "referer": "https://melbet.ug/line",
    },
}


# =============================================================================
# Bookmaker identity
# =============================================================================

BOOKMAKER_CANONICAL = {
    "SportyBet": "SportyBet",
    "SportyBetOfficial": "SportyBet",
    "ChampionBet": "ChampionBet",
    "ChampionBetOfficial": "ChampionBet",
    "1xBet": "1xBet",
    "22Bet": "22Bet",
    "AbaBet": "AbaBet",
    "Fortebet": "Fortebet",
    "Melbet": "Melbet",
    "kbet": "kbet",
    "Betway": "Betway",
    "BetPawa": "BetPawa",
    "PremierBet": "PremierBet",
    "Bongobongo": "Bongobongo",
    "SaharaGames": "SaharaGames",
}


def canonical_bookmaker(name: Any) -> str:
    value = str(name or "").strip()
    return BOOKMAKER_CANONICAL.get(value, value)


# =============================================================================
# Global scanner state
# =============================================================================

history_lock = threading.Lock()
scan_lock = threading.Lock()
status_lock = threading.Lock()

scanner_status: Dict[str, Any] = {
    "last_scan_started": None,
    "last_scan_finished": None,
    "last_scan_success": False,
    "last_scan_error": None,
    "last_scan_valid": False,
    "opportunities_count": 0,
    "total_odds": 0,
    "healthy_bookmakers": 0,
    "bookmakers": {},
}


# =============================================================================
# HTTP client
# =============================================================================

class HTTPClient:
    def __init__(self, timeout: int = HTTP_TIMEOUT):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.7827.159 Mobile Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.RequestException, ConnectionError, TimeoutError)),
        reraise=True,
    )
    def get_response(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        request_headers = self.session.headers.copy()
        if headers:
            request_headers.update(headers)
        response = self.session.get(url, headers=request_headers, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None) -> Any:
        response = self.get_response(url, headers=headers, params=params)
        return response.json()

    def get_text(self, url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None) -> str:
        response = self.get_response(url, headers=headers, params=params)
        return response.text


http = HTTPClient()


# =============================================================================
# General helpers
# =============================================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_timestamp() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


TEAM_ALIASES = {
    "manchester united": "man utd",
    "man utd": "man utd",
    "man united": "man utd",
    "manchester city": "man city",
    "man city": "man city",
    "wolverhampton wanderers": "wolves",
    "wolverhampton": "wolves",
    "wolves": "wolves",
    "tottenham hotspur": "spurs",
    "tottenham": "spurs",
    "spurs": "spurs",
    "newcastle united": "newcastle",
    "leicester city": "leicester",
    "west ham united": "west ham",
    "brighton and hove albion": "brighton",
    "sheffield united": "sheffield utd",
    "nottingham forest": "forest",
    "luton town": "luton",
    "bayern munich": "bayern",
    "bayern munchen": "bayern",
    "borussia dortmund": "dortmund",
    "eintracht frankfurt": "frankfurt",
    "bayer leverkusen": "leverkusen",
    "real madrid": "real madrid",
    "barcelona": "barcelona",
    "atletico madrid": "atletico",
    "athletic bilbao": "bilbao",
    "real sociedad": "sociedad",
    "paris saint-germain": "psg",
    "psg": "psg",
    "inter milan": "inter",
    "ac milan": "milan",
    "juventus": "juventus",
    "napoli": "napoli",
    "borussia monchengladbach": "gladbach",
    "monchengladbach": "gladbach",
    "bayer 04 leverkusen": "leverkusen",
    "eintracht": "frankfurt",
    "athletic club": "bilbao",
    "real betis": "betis",
    "sevilla": "sevilla",
    "valencia": "valencia",
}

GENERIC_TEAM_NAMES = {
    "home", "away", "team a", "team b", "tbd", "tba",
    "unknown", "none", "null", "draw", "over", "under",
}


def normalize_team(name: Any) -> str:
    if name is None:
        return ""
    value = str(name).lower().strip()
    if not value:
        return ""

    if value in GENERIC_TEAM_NAMES:
        return ""

    value = TEAM_ALIASES.get(value, value)

    replacements = {
        r"\brovers\b": "rvs",
        r"\brvs\b": "rvs",
        r"\bunited\b": "utd",
        r"\butd\b": "utd",
        r"\bmunich\b": "mun",
        r"\bfc\b": "",
        r"\bsc\b": "",
        r"\bcf\b": "",
        r"\bac\b": "",
        r"\breserves\b": "",
        r"\breserve\b": "",
        r"\bu21\b": "",
        r"\bu23\b": "",
        r"\bwomen\b": "",
        r"\bmen\b": "",
    }
    for pattern, replacement in replacements.items():
        value = re.sub(pattern, replacement, value)

    value = re.sub(r"[^a-z0-9 ]", "", value)
    value = re.sub(r"s+", " ", value).strip()

    return value


def teams_match(name1: Any, name2: Any) -> bool:
    one = normalize_team(name1)
    two = normalize_team(name2)

    if not one or not two:
        return False

    if one == two:
        return True

    one_words = set(one.split())
    two_words = set(two.split())

    if not one_words or not two_words:
        return False

    overlap = one_words & two_words
    if not overlap:
        return False

    union = one_words | two_words
    jaccard = len(overlap) / len(union)

    if jaccard < 0.5:
        return False

    if one_words.issubset(two_words) or two_words.issubset(one_words):
        return True

    min_words = min(len(one_words), len(two_words))
    if len(overlap) < min_words and len(overlap) < 2:
        return False

    return True


def normalize_market_specifier(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = float(text)
        if math.isfinite(number):
            return f"{number:g}"
    except (TypeError, ValueError):
        pass
    return text.lower()


def market_key(
    home: str,
    away: str,
    market_type: str = "1x2",
    market_specifier: str = "",
) -> str:
    base = f"{normalize_team(home)} vs {normalize_team(away)}"
    return (
        f"{base}|"
        f"{(market_type or '1x2').strip().lower()}|"
        f"{normalize_market_specifier(market_specifier)}"
    )


def match_key_similarity(key1: str, key2: str) -> bool:
    if key1 == key2:
        return True
    parts1 = key1.split("|")
    parts2 = key2.split("|")
    if len(parts1) != 3 or len(parts2) != 3:
        return False
    if parts1[1] != parts2[1]:
        return False
    if not parts1[2] or not parts2[2]:
        return False
    if parts1[2] != parts2[2]:
        return False
    teams1 = parts1[0].split(" vs ", 1)
    teams2 = parts2[0].split(" vs ", 1)
    if len(teams1) != 2 or len(teams2) != 2:
        return False
    return teams_match(teams1[0], teams2[0]) and teams_match(teams1[1], teams2[1])


def clean_odd(
    value: Any,
    min_odd: float = 1.01,
    max_odd: float = 100.0,
) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.replace(",", ".").strip()
        odd = float(value)
        if not math.isfinite(odd):
            return None
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


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        if not math.isfinite(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def first_not_empty(data: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def atomic_json_write(filename: str, data: Any) -> None:
    directory = os.path.dirname(os.path.abspath(filename)) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(directory, f".{os.path.basename(filename)}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, filename)
    except Exception:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        raise


# =============================================================================
# Odds freshness check
# =============================================================================

def is_odds_fresh(record: Dict[str, Any]) -> bool:
    if MAX_ODDS_AGE_MINUTES <= 0:
        return True
    scraped_at = record.get("scraped_at")
    if not scraped_at:
        return True
    try:
        scraped_dt = datetime.strptime(scraped_at, "%Y-%m-%d %H:%M:%S")
        scraped_dt = scraped_dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - scraped_dt
        return age <= timedelta(minutes=MAX_ODDS_AGE_MINUTES)
    except ValueError:
        return True


# =============================================================================
# Scanner status
# =============================================================================

def reset_scanner_status() -> None:
    global scanner_status
    with status_lock:
        scanner_status = {
            "last_scan_started": utc_now().isoformat(),
            "last_scan_finished": None,
            "last_scan_success": False,
            "last_scan_error": None,
            "last_scan_valid": False,
            "opportunities_count": 0,
            "total_odds": 0,
            "healthy_bookmakers": 0,
            "bookmakers": {},
        }


def update_bookmaker_status(
    bookmaker: str,
    *,
    success: bool,
    records: int,
    error: Optional[str] = None,
    duration: Optional[float] = None,
) -> None:
    with status_lock:
        scanner_status["bookmakers"][bookmaker] = {
            "success": success,
            "records": records,
            "error": error,
            "duration_seconds": round(duration, 2) if duration is not None else None,
            "last_updated": utc_now().isoformat(),
        }


def save_scanner_status() -> bool:
    with status_lock:
        data = json.loads(json.dumps(scanner_status, ensure_ascii=False))
    try:
        atomic_json_write(SCANNER_STATUS_FILE, data)
        return True
    except Exception:
        logger.exception("Unable to save scanner status")
        return False


def scraper_call(
    bookmaker: str,
    scraper: Callable[[], List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    started = time.monotonic()
    try:
        records = scraper()
        if records is None:
            records = []
        records = list(records)
        duration = time.monotonic() - started

        if MAX_ODDS_AGE_MINUTES > 0:
            records = [r for r in records if is_odds_fresh(r)]

        if ZERO_RECORDS_ARE_ERRORS and not records:
            error = "Bookmaker returned zero usable odds records."
            update_bookmaker_status(bookmaker, success=False, records=0, error=error, duration=duration)
            logger.error("%s returned ZERO records", bookmaker)
        else:
            update_bookmaker_status(bookmaker, success=True, records=len(records), duration=duration)
            logger.info("%s: %s usable records", bookmaker, len(records))

        return records
    except Exception as exc:
        duration = time.monotonic() - started
        update_bookmaker_status(bookmaker, success=False, records=0, error=str(exc), duration=duration)
        logger.exception("%s scraper failed", bookmaker)
        return []


# =============================================================================
# Match record
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
    event_id: Optional[str] = None,
) -> Dict[str, Any]:
    sport_lower = (sport or "Football").strip().lower()
    sport_map = {
        "soccer": "football",
        "futbol": "football",
        "football": "football",
        "rugby": "rugby",
        "futsal": "futsal",
        "basketball": "basketball",
        "tennis": "tennis",
    }
    normalized_sport = sport_map.get(sport_lower, sport_lower).capitalize()

    canonical_bm = canonical_bookmaker(bookmaker)
    normalized_market = (market_type or "1x2").strip()
    normalized_specifier = normalize_market_specifier(market_specifier)

    return {
        "match": f"{str(home_team).strip()} vs {str(away_team).strip()}",
        "home_team": str(home_team).strip(),
        "away_team": str(away_team).strip(),
        "match_key": market_key(home_team, away_team, normalized_market, normalized_specifier),
        "bookmaker": canonical_bm,
        "source_bookmaker": str(bookmaker).strip(),
        "competition": competition or "",
        "home": home,
        "draw": draw,
        "away": away,
        "sport": normalized_sport,
        "market_type": normalized_market,
        "market_specifier": normalized_specifier,
        "event_id": str(event_id) if event_id is not None else None,
        "scraped_at": utc_timestamp(),
    }


# =============================================================================
# History
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
    atomic_json_write(HISTORY_FILE, history)


def opportunity_key(opportunity: Dict[str, Any]) -> str:
    base = "::".join(
        [
            str(opportunity.get("sport", "Football")),
            str(opportunity.get("market_type", opportunity.get("type", "1x2"))),
            str(opportunity.get("market_specifier", "")),
            str(opportunity.get("match", "")),
        ]
    )
    bets = opportunity.get("bets", [])
    bets_signature = "|".join(
        sorted(f"{bet.get('bookmaker')}:{bet.get('outcome')}" for bet in bets)
    )
    return f"{base}::{bets_signature}"


def update_arbitrage_history(
    current: List[Dict[str, Any]],
    history: Dict[str, Any],
    timestamp: str,
) -> None:
    for entry in history.values():
        if isinstance(entry, dict):
            entry["updated_this_cycle"] = False

    for opportunity in current:
        key = opportunity_key(opportunity)
        if key not in history:
            history[key] = {
                "match": opportunity.get("match", ""),
                "sport": opportunity.get("sport", "Football"),
                "market_type": opportunity.get("market_type", opportunity.get("type", "1x2")),
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

        versions = entry.setdefault("versions", [])
        versions.append(
            {
                "timestamp": timestamp,
                "profit_percent": opportunity.get("profit_percent", 0),
                "profit_ugx": opportunity.get("profit_ugx", 0),
                "arb_sum": opportunity.get("arb_sum", 0),
                "bets": opportunity.get("bets", []),
            }
        )
        if len(versions) > MAX_HISTORY_VERSIONS:
            del versions[: len(versions) - MAX_HISTORY_VERSIONS]

    for key, entry in list(history.items()):
        if not isinstance(entry, dict):
            continue
        if not entry.get("updated_this_cycle", False):
            entry["cycles_missed"] = safe_int(entry.get("cycles_missed", 0)) + 1
            if entry["cycles_missed"] >= 2:
                entry["valid"] = False
        entry.pop("updated_this_cycle", None)


# =============================================================================
# Telegram
# =============================================================================

def escape_telegram_markdown(text: Any) -> str:
    value = str(text or "")
    return re.sub(r"([_*`[]])", r"\\\u0001", value)


def send_telegram_alert(opportunity: Dict[str, Any]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    match = escape_telegram_markdown(opportunity.get("match", "Unknown"))
    profit = safe_float(opportunity.get("profit_percent", 0))
    profit_ugx = safe_int(opportunity.get("profit_ugx", 0))

    lines = [
        f"⚽ *{match}*",
        f"💰 Profit: *{profit:.2f}%* (UGX {profit_ugx:,})",
    ]
    for bet in opportunity.get("bets", []):
        bookmaker = escape_telegram_markdown(bet.get("bookmaker", "Unknown"))
        outcome = escape_telegram_markdown(bet.get("outcome", "Unknown"))
        odd = safe_float(bet.get("odd", 0))
        stake = safe_int(bet.get("stake", 0))
        lines.append(f"▶ {bookmaker} ({outcome}) @ {odd:g} - Stake: UGX {stake:,}")
    message = "
".join(lines)

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Telegram alert sent for %s", opportunity.get("match"))
    except Exception as exc:
        logger.error("Telegram error: %s", exc)


# =============================================================================
# Generic HTML scraper (improved)
# =============================================================================

def _select_first(element, selectors):
    """Try multiple CSS selectors on an element and return first match."""
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        if not sel:
            continue
        found = element.select_one(sel)
        if found:
            return found
    return None


def scrape_generic_html(
    bookmaker_name: str,
    base_url: str,
    match_selector: Union[str, List[str]],
    home_selector: Union[str, List[str]],
    away_selector: Union[str, List[str]],
    odds_1_selector: Union[str, List[str]],
    odds_x_selector: Optional[Union[str, List[str]]] = None,
    odds_2_selector: Union[str, List[str]],
    over_selector: Optional[Union[str, List[str]]] = None,
    under_selector: Optional[Union[str, List[str]]] = None,
    next_page_selector: Optional[Union[str, List[str]]] = None,
    max_pages: int = MAX_HTML_PAGES,
    sport: str = "Football",
) -> List[Dict[str, Any]]:
    logger.info("Fetching %s via HTML...", bookmaker_name)
    all_odds: List[Dict[str, Any]] = []

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": base_url,
            "Connection": "keep-alive",
        }
    )

    page = 1
    while page <= max_pages:
        if re.search(r"([?&])page=d+", base_url, re.IGNORECASE):
            url = re.sub(r"([?&])page=d+", lambda m: f"{m.group(1)}page={page}", base_url, flags=re.IGNORECASE)
        elif "?" in base_url:
            url = f"{base_url}&page={page}"
        else:
            url = f"{base_url}?page={page}"

        try:
            response = session.get(url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            html = response.text
        except requests.RequestException as exc:
            logger.error("%s page %s error: %s", bookmaker_name, page, exc)
            break

        soup = BeautifulSoup(html, "html.parser")
        matches = soup.select(match_selector) if isinstance(match_selector, str) else []
        if not matches and isinstance(match_selector, list):
            for sel in match_selector:
                matches = soup.select(sel)
                if matches:
                    break

        if not matches:
            logger.warning("%s page %s contained no matches", bookmaker_name, page)
            break

        page_records_before = len(all_odds)

        for match in matches:
            home_el = _select_first(match, home_selector)
            away_el = _select_first(match, away_selector)
            if not home_el or not away_el:
                continue

            home_team = home_el.get_text(" ", strip=True)
            away_team = away_el.get_text(" ", strip=True)
            if not home_team or not away_team:
                continue
            if normalize_team(home_team) == "" or normalize_team(away_team) == "":
                continue

            def extract_selector_odd(selector: Optional[Union[str, List[str]]]) -> Optional[float]:
                if not selector:
                    return None
                element = _select_first(match, selector)
                if not element:
                    return None
                return clean_odd(element.get_text(" ", strip=True))

            odd_1 = extract_selector_odd(odds_1_selector)
            odd_x = extract_selector_odd(odds_x_selector)
            odd_2 = extract_selector_odd(odds_2_selector)

            if odd_1 and odd_2:
                all_odds.append(
                    build_match_record(home_team, away_team, bookmaker_name, odd_1, odd_x, odd_2, sport=sport)
                )

            if over_selector and under_selector:
                over_odd = extract_selector_odd(over_selector)
                under_odd = extract_selector_odd(under_selector)
                if over_odd and under_odd:
                    all_odds.append(
                        build_match_record(
                            home_team,
                            away_team,
                            bookmaker_name,
                            over_odd,
                            under_odd,
                            None,
                            sport=sport,
                            market_type="Over/Under 2.5",
                            market_specifier="2.5",
                        )
                    )

        page_records = len(all_odds) - page_records_before
        logger.info("%s page %s: %s records", bookmaker_name, page, page_records)

        if next_page_selector:
            next_button = _select_first(soup, next_page_selector)
            if not next_button:
                break
            classes = set(next_button.get("class", []))
            if "disabled" in classes or next_button.has_attr("disabled"):
                break
        else:
            next_link = soup.find("a", string=re.compile(r"^s*(Next|»|→)s*$", re.IGNORECASE))
            if not next_link:
                break

        page += 1
        if page <= max_pages:
            time.sleep(1)

    logger.info("%s: %s records", bookmaker_name, len(all_odds))
    return all_odds


# =============================================================================
# SportyBet (simple API)
# =============================================================================

def scrape_sportybet() -> List[Dict[str, Any]]:
    logger.info("Fetching SportyBet...")
    odds = []
    try:
        data = http.get_json(SPORTYBET_API)
        if not isinstance(data, list):
            raise ValueError("SportyBet API did not return a list")
        for event in data:
            if not isinstance(event, dict):
                continue
            home = first_not_empty(event, ["home_team", "homeTeam", "home"])
            away = first_not_empty(event, ["away_team", "awayTeam", "away"])
            if not home or not away:
                continue
            if normalize_team(home) == "" or normalize_team(away) == "":
                continue
            sport = event.get("sport", "Football") or "Football"
            home_odd = clean_odd(event.get("home"))
            draw_odd = clean_odd(event.get("draw"))
            away_odd = clean_odd(event.get("away"))
            event_id = event.get("id") or event.get("event_id") or event.get("eventId")
            if home_odd and away_odd:
                odds.append(build_match_record(home, away, "SportyBet", home_odd, draw_odd, away_odd, sport=sport, event_id=event_id))
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
                        market_specifier="2.5",
                        event_id=event_id,
                    )
                )
        logger.info("SportyBet: %s records", len(odds))
    except Exception as exc:
        logger.exception("SportyBet error: %s", exc)
    return odds


# =============================================================================
# SportyBet official API
# =============================================================================

def scrape_sportybet_official() -> List[Dict[str, Any]]:
    logger.info("Fetching SportyBet official API...")
    odds = []
    base_headers = {"Referer": "https://www.sportybet.com/ng/m/"}
    try:
        sport_list_url = "https://www.sportybet.com/factsCenter/wapPopularAndSportOption/v2"
        data = http.get_json(sport_list_url, headers=base_headers)
        if not isinstance(data, dict):
            raise ValueError("SportyBet official API returned invalid JSON")
        if data.get("bizCode") != 10000:
            raise RuntimeError(f"SportyBet API error: {data.get('message')}")
        sport_data = data.get("data", {})
        sport_list = sport_data.get("sportList", []) if isinstance(sport_data, dict) else []
        football = next((s for s in sport_list if isinstance(s, dict) and s.get("id") == "sr:sport:1"), None)
        if not football:
            raise RuntimeError("Football not found in SportyBet sports list")
        categories = football.get("categories") or []
        for category in categories:
            if not isinstance(category, dict):
                continue
            tournaments = category.get("tournaments") or []
            for tournament in tournaments:
                if not isinstance(tournament, dict):
                    continue
                tournament_id = tournament.get("id")
                if not tournament_id:
                    continue
                event_list_url = f"https://www.sportybet.com/factsCenter/event/list?tournamentId={tournament_id}"
                try:
                    event_data = http.get_json(event_list_url, headers=base_headers)
                except Exception as exc:
                    logger.warning("Failed to fetch events for tournament %s: %s", tournament_id, exc)
                    continue
                if not isinstance(event_data, dict) or event_data.get("bizCode") != 10000:
                    continue
                event_data_payload = event_data.get("data", {})
                events = event_data_payload.get("events", []) if isinstance(event_data_payload, dict) else []
                if not isinstance(events, list):
                    events = []
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    event_id = event.get("id")
                    home = event.get("homeTeamName") or event.get("home")
                    away = event.get("awayTeamName") or event.get("away")
                    if not event_id or not home or not away:
                        continue
                    if normalize_team(home) == "" or normalize_team(away) == "":
                        continue
                    odds_url = f"https://www.sportybet.com/factsCenter/event/odds?eventId={event_id}"
                    try:
                        odds_data = http.get_json(odds_url, headers=base_headers)
                    except Exception as exc:
                        logger.warning("Failed to fetch odds for event %s: %s", event_id, exc)
                        continue
                    if not isinstance(odds_data, dict) or odds_data.get("bizCode") != 10000:
                        continue
                    payload = odds_data.get("data", {})
                    if not isinstance(payload, dict):
                        continue
                    home_odd = clean_odd(payload.get("homeOdd") or payload.get("home") or payload.get("1"))
                    draw_odd = clean_odd(payload.get("drawOdd") or payload.get("draw") or payload.get("x"))
                    away_odd = clean_odd(payload.get("awayOdd") or payload.get("away") or payload.get("2"))
                    if home_odd and away_odd:
                        odds.append(build_match_record(home, away, "SportyBetOfficial", home_odd, draw_odd, away_odd, event_id=event_id))
                    over_odd = clean_odd(payload.get("overOdd") or payload.get("over") or payload.get("over_odd"))
                    under_odd = clean_odd(payload.get("underOdd") or payload.get("under") or payload.get("under_odd"))
                    if over_odd and under_odd:
                        odds.append(
                            build_match_record(
                                home,
                                away,
                                "SportyBetOfficial",
                                over_odd,
                                under_odd,
                                None,
                                market_type="Over/Under 2.5",
                                market_specifier="2.5",
                                event_id=event_id,
                            )
                        )
                    time.sleep(0.05)
        logger.info("SportyBetOfficial: %s records", len(odds))
    except Exception as exc:
        logger.exception("SportyBet official API error: %s", exc)
    return odds


# =============================================================================
# ChampionBet helpers and scrapers
# =============================================================================

def championbet_market_items(bet_map: Dict[str, Any], keys: Iterable[Any]) -> List[Dict[str, Any]]:
    result = []
    if not isinstance(bet_map, dict):
        return result
    for key in keys:
        market = bet_map.get(str(key))
        if not isinstance(market, dict):
            continue
        for item_key, item in market.items():
            if not isinstance(item, dict):
                continue
            copy = dict(item)
            copy["_market_key"] = str(key)
            copy["_item_key"] = str(item_key)
            result.append(copy)
    return result


def extract_odd_from_item(item: Dict[str, Any]) -> Optional[float]:
    for key in ("ov", "odds", "odd", "value", "C", "c"):
        if key in item:
            odd = clean_odd(item.get(key))
            if odd is not None:
                return odd
    return None


def normalize_label(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def item_label(item: Dict[str, Any]) -> str:
    for key in ("name", "label", "n", "desc", "description", "title", "outcome", "outcomeName", "caption"):
        value = item.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def extract_championbet_1x2(bet_map: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    home = None
    draw = None
    away = None
    items = championbet_market_items(bet_map, [1, 2, 3, 4, 5, 6, 7, 8, 9])
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = normalize_label(item_label(item))
        if label in {"1", "home", "homewin"}:
            if home is None:
                home = odd
            continue
        if label in {"x", "draw", "tie"}:
            if draw is None:
                draw = odd
            continue
        if label in {"2", "away", "awaywin"}:
            if away is None:
                away = odd
            continue
    return home, draw, away


def extract_championbet_over_under(bet_map: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    over = None
    under = None
    items = championbet_market_items(bet_map, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = normalize_label(item_label(item))
        if "over" in label:
            if over is None:
                over = odd
            continue
        if "under" in label:
            if under is None:
                under = odd
            continue
    return over, under


def scrape_championbet() -> List[Dict[str, Any]]:
    logger.info("Fetching ChampionBet...")
    odds = []
    try:
        data = http.get_json(CHAMPIONBET_TOP_API)
        if not isinstance(data, dict):
            raise ValueError("ChampionBet API returned invalid JSON")
        
        events = data.get("events", []) if isinstance(data, dict) else []
        for event in events:
            if not isinstance(event, dict):
                continue
            
            home_team = event.get("home") or event.get("homeName")
            away_team = event.get("away") or event.get("awayName")
            if not home_team or not away_team:
                continue
            
            if normalize_team(home_team) == "" or normalize_team(away_team) == "":
                continue
            
            event_id = event.get("id") or event.get("eventId")
            bet_map = event.get("bets", {}) or event.get("markets", {}) or {}
            
            home_odd, draw_odd, away_odd = extract_championbet_1x2(bet_map)
            if home_odd and away_odd:
                odds.append(
                    build_match_record(
                        home_team, away_team, "ChampionBet",
                        home_odd, draw_odd, away_odd,
                        event_id=event_id
                    )
                )
            
            over_odd, under_odd = extract_championbet_over_under(bet_map)
            if over_odd and under_odd:
                odds.append(
                    build_match_record(
                        home_team, away_team, "ChampionBet",
                        over_odd, under_odd, None,
                        market_type="Over/Under 2.5",
                        market_specifier="2.5",
                        event_id=event_id
                    )
                )
        
        logger.info("ChampionBet: %s records", len(odds))
    except Exception as exc:
        logger.exception("ChampionBet error: %s", exc)
    return odds


# =============================================================================
# 1xBet and shared bookmakers scraper
# =============================================================================

def scrape_shared_bookmaker(
    bookmaker_name: str,
    config: Dict[str, Any],
    sport_id: int = 1,
) -> List[Dict[str, Any]]:
    logger.info("Fetching %s...", bookmaker_name)
    odds = []
    base_url = config["base_url"]
    
    try:
        line_url = f"{base_url}/lineFeed/{sport_id}"
        params = {
            "partner": config["partner"],
            "lng": config["lng"],
            "tz": config["tz"],
            "gr": config["gr"],
            "referer": config["referer"],
        }
        
        data = http.get_json(line_url, params=params)
        if not isinstance(data, dict):
            raise ValueError(f"{bookmaker_name} API returned invalid JSON")
        
        events = data.get("data", {}).get("events", []) if isinstance(data, dict) else []
        for event in events:
            if not isinstance(event, dict):
                continue
            
            home_team = event.get("home") or event.get("homeName")
            away_team = event.get("away") or event.get("awayName")
            if not home_team or not away_team:
                continue
            
            if normalize_team(home_team) == "" or normalize_team(away_team) == "":
                continue
            
            event_id = event.get("id")
            markets = event.get("bets", {}) or event.get("markets", {}) or {}
            
            market_1x2 = markets.get("1") or markets.get(1) or {}
            home_odd = clean_odd(market_1x2.get("1") or market_1x2.get("home"))
            draw_odd = clean_odd(market_1x2.get("X") or market_1x2.get("draw"))
            away_odd = clean_odd(market_1x2.get("2") or market_1x2.get("away"))
            
            if home_odd and away_odd:
                odds.append(
                    build_match_record(
                        home_team, away_team, bookmaker_name,
                        home_odd, draw_odd, away_odd,
                        event_id=event_id
                    )
                )
        
        logger.info("%s: %s records", bookmaker_name, len(odds))
    except Exception as exc:
        logger.exception("%s error: %s", bookmaker_name, exc)
    return odds


def scrape_1xbet() -> List[Dict[str, Any]]:
    return scrape_shared_bookmaker("1xBet", SHARED_BOOKMAKERS["1xBet"])


def scrape_22bet() -> List[Dict[str, Any]]:
    return scrape_shared_bookmaker("22Bet", SHARED_BOOKMAKERS["22Bet"])


def scrape_melbet() -> List[Dict[str, Any]]:
    return scrape_shared_bookmaker("Melbet", SHARED_BOOKMAKERS["Melbet"])


# =============================================================================
# Arbitrage Detection
# =============================================================================

def find_arbitrage_opportunities(
    all_odds: List[Dict[str, Any]],
    min_profit: float = MIN_ARB_PROFIT_PERCENT,
    max_profit: float = MAX_ARB_PROFIT_PERCENT,
) -> List[Dict[str, Any]]:
    logger.info("Finding arbitrage opportunities...")
    opportunities = []
    
    match_odds: Dict[str, List[Dict[str, Any]]] = {}
    for record in all_odds:
        key = record.get("match_key", "")
        if not key:
            continue
        if key not in match_odds:
            match_odds[key] = []
        match_odds[key].append(record)
    
    for match_key, records in match_odds.items():
        if len(records) < 2:
            continue
        
        market_groups: Dict[str, List[Dict[str, Any]]] = {}
        for record in records:
            market_type = record.get("market_type", "1x2")
            specifier = record.get("market_specifier", "")
            group_key = f"{market_type}|{specifier}"
            if group_key not in market_groups:
                market_groups[group_key] = []
            market_groups[group_key].append(record)
        
        for market_key, market_records in market_groups.items():
            market_type, specifier = market_key.split("|", 1)
            
            if market_type.lower() == "1x2":
                best_home = None
                best_draw = None
                best_away = None
                
                for record in market_records:
                    home = record.get("home")
                    draw = record.get("draw")
                    away = record.get("away")
                    
                    if home and (best_home is None or home > best_home["home"]):
                        best_home = record
                    if draw and (best_draw is None or draw > best_draw["draw"]):
                        best_draw = record
                    if away and (best_away is None or away > best_away["away"]):
                        best_away = record
                
                if not all([best_home, best_away]):
                    continue
                
                home_odd = best_home["home"]
                away_odd = best_away["away"]
                draw_odd = best_draw["draw"] if best_draw else None
                
                if home_odd and away_odd:
                    implied_prob = (1 / home_odd) + (1 / away_odd)
                    if implied_prob < 1.0:
                        profit_percent = (1 / implied_prob - 1) * 100
                        if min_profit <= profit_percent <= max_profit:
                            if best_home["bookmaker"] != best_away["bookmaker"]:
                                stake = DEFAULT_STAKE
                                stake_home = stake / (implied_prob * home_odd)
                                stake_away = stake / (implied_prob * away_odd)
                                profit_ugx = stake * (1 / implied_prob - 1)
                                
                                opportunities.append({
                                    "match": best_home.get("match", ""),
                                    "sport": best_home.get("sport", "Football"),
                                    "market_type": "1x2",
                                    "market_specifier": "",
                                    "profit_percent": profit_percent,
                                    "profit_ugx": profit_ugx,
                                    "arb_sum": implied_prob,
                                    "bets": [
                                        {
                                            "bookmaker": best_home["bookmaker"],
                                            "outcome": "home",
                                            "odd": home_odd,
                                            "stake": stake_home,
                                        },
                                        {
                                            "bookmaker": best_away["bookmaker"],
                                            "outcome": "away",
                                            "odd": away_odd,
                                            "stake": stake_away,
                                        },
                                    ],
                                })
                
                if all([best_home, best_draw, best_away]) and draw_odd:
                    implied_prob = (1 / home_odd) + (1 / draw_odd) + (1 / away_odd)
                    if implied_prob < 1.0:
                        profit_percent = (1 / implied_prob - 1) * 100
                        if min_profit <= profit_percent <= max_profit:
                            bookmakers = {best_home["bookmaker"], best_draw["bookmaker"], best_away["bookmaker"]}
                            if len(bookmakers) >= 2:
                                stake = DEFAULT_STAKE
                                stake_home = stake / (implied_prob * home_odd)
                                stake_draw = stake / (implied_prob * draw_odd)
                                stake_away = stake / (implied_prob * away_odd)
                                profit_ugx = stake * (1 / implied_prob - 1)
                                
                                opportunities.append({
                                    "match": best_home.get("match", ""),
                                    "sport": best_home.get("sport", "Football"),
                                    "market_type": "1x2",
                                    "market_specifier": "",
                                    "profit_percent": profit_percent,
                                    "profit_ugx": profit_ugx,
                                    "arb_sum": implied_prob,
                                    "bets": [
                                        {
                                            "bookmaker": best_home["bookmaker"],
                                            "outcome": "home",
                                            "odd": home_odd,
                                            "stake": stake_home,
                                        },
                                        {
                                            "bookmaker": best_draw["bookmaker"],
                                            "outcome": "draw",
                                            "odd": draw_odd,
                                            "stake": stake_draw,
                                        },
                                        {
                                            "bookmaker": best_away["bookmaker"],
                                            "outcome": "away",
                                            "odd": away_odd,
                                            "stake": stake_away,
                                        },
                                    ],
                                })
            
            elif market_type.lower() in ["over/under 2.5", "over/under"]:
                best_over = None
                best_under = None
                
                for record in market_records:
                    home = record.get("home")
                    draw = record.get("draw")
                    
                    if home and (best_over is None or home > best_over["home"]):
                        best_over = record
                    if draw and (best_under is None or draw > best_under["draw"]):
                        best_under = record
                
                if best_over and best_under:
                    over_odd = best_over["home"]
                    under_odd = best_under["draw"]
                    
                    implied_prob = (1 / over_odd) + (1 / under_odd)
                    if implied_prob < 1.0:
                        profit_percent = (1 / implied_prob - 1) * 100
                        if min_profit <= profit_percent <= max_profit:
                            if best_over["bookmaker"] != best_under["bookmaker"]:
                                stake = DEFAULT_STAKE
                                stake_over = stake / (implied_prob * over_odd)
                                stake_under = stake / (implied_prob * under_odd)
                                profit_ugx = stake * (1 / implied_prob - 1)
                                
                                opportunities.append({
                                    "match": best_over.get("match", ""),
                                    "sport": best_over.get("sport", "Football"),
                                    "market_type": "Over/Under 2.5",
                                    "market_specifier": "2.5",
                                    "profit_percent": profit_percent,
                                    "profit_ugx": profit_ugx,
                                    "arb_sum": implied_prob,
                                    "bets": [
                                        {
                                            "bookmaker": best_over["bookmaker"],
                                            "outcome": "over",
                                            "odd": over_odd,
                                            "stake": stake_over,
                                        },
                                        {
                                            "bookmaker": best_under["bookmaker"],
                                            "outcome": "under",
                                            "odd": under_odd,
                                            "stake": stake_under,
                                        },
                                    ],
                                })
    
    logger.info("Found %s arbitrage opportunities", len(opportunities))
    return opportunities


# =============================================================================
# Main scanner
# =============================================================================

def run_full_scan() -> Dict[str, Any]:
    reset_scanner_status()
    save_scanner_status()
    
    all_odds = []
    bookmaker_funcs = [
        ("SportyBet", scrape_sportybet),
        ("ChampionBet", scrape_championbet),
        ("1xBet", scrape_1xbet),
        ("22Bet", scrape_22bet),
        ("Melbet", scrape_melbet),
    ]
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(scraper_call, name, func): name
            for name, func in bookmaker_funcs
        }
        
        for future in as_completed(futures):
            bookmaker_name = futures[future]
            try:
                records = future.result()
                all_odds.extend(records)
            except Exception as exc:
                logger.exception("%s failed: %s", bookmaker_name, exc)
    
    healthy_count = sum(
        1 for bm in scanner_status["bookmakers"].values()
        if bm.get("success", False) and bm.get("records", 0) > 0
    )
    
    with status_lock:
        scanner_status["healthy_bookmakers"] = healthy_count
        scanner_status["total_odds"] = len(all_odds)
    
    save_scanner_status()
    
    if healthy_count < MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN:
        logger.error(
            "Only %s bookmakers returned data; minimum required is %s",
            healthy_count,
            MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN,
        )
        return {
            "success": False,
            "error": f"Insufficient bookmakers: {healthy_count}",
            "opportunities": [],
        }
    
    opportunities = find_arbitrage_opportunities(all_odds)
    
    history = load_arbitrage_history()
    timestamp = utc_timestamp()
    update_arbitrage_history(opportunities, history, timestamp)
    save_arbitrage_history(history)
    
    atomic_json_write(OPPORTUNITIES_FILE, opportunities)
    
    for opp in opportunities:
        if opp.get("profit_percent", 0) >= TELEGRAM_MIN_PROFIT:
            send_telegram_alert(opp)
    
    with status_lock:
        scanner_status["last_scan_finished"] = utc_now().isoformat()
        scanner_status["last_scan_success"] = True
        scanner_status["last_scan_valid"] = True
        scanner_status["opportunities_count"] = len(opportunities)
    
    save_scanner_status()
    
    return {
        "success": True,
        "opportunities": opportunities,
        "total_odds": len(all_odds),
        "healthy_bookmakers": healthy_count,
    }


if __name__ == "__main__":
    result = run_full_scan()
    print(json.dumps(result, indent=2, ensure_ascii=False))
