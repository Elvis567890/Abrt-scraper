# =============================================================================
# scanner.py – Pure Arbitrage Scanner (Expanded Markets + Safety Enhancements)
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
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

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
MAX_HTML_PAGES = env_int("MAX_HTML_PAGES", 5, minimum=1)

# Minimum and maximum profit percentage for an arbitrage
MIN_ARB_PROFIT_PERCENT = env_float("MIN_ARB_PROFIT_PERCENT", 0.2, minimum=0.0)
MAX_ARB_PROFIT_PERCENT = env_float("MAX_ARB_PROFIT_PERCENT", 50.0, minimum=MIN_ARB_PROFIT_PERCENT)

# Warn if profit exceeds this (suspiciously high)
WARN_PROFIT_PERCENT = env_float("WARN_PROFIT_PERCENT", 15.0, minimum=MIN_ARB_PROFIT_PERCENT)

# Reject records older than this many minutes (0 = disabled)
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
    # Extra aliases
    "borussia monchengladbach": "gladbach",
    "monchengladbach": "gladbach",
    "bayer 04 leverkusen": "leverkusen",
    "eintracht": "frankfurt",
    "athletic club": "bilbao",
    "real betis": "betis",
    "sevilla": "sevilla",
    "valencia": "valencia",
    # Add more as needed
}

GENERIC_TEAM_NAMES = {
    "home",
    "away",
    "team a",
    "team b",
    "tbd",
    "tba",
    "unknown",
    "none",
    "null",
    "draw",
    "over",
    "under",
}


def normalize_team(name: Any) -> str:
    if name is None:
        return ""
    value = str(name).lower().strip()
    if not value:
        return ""

    # If the name is generic, return empty to prevent matching
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
    value = re.sub(r"\s+", " ", value).strip()

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

    # Reject if either team name contains only one word (too generic)
    if len(one_words) < 2 or len(two_words) < 2:
        return False

    overlap = one_words & two_words
    if not overlap:
        return False

    # Calculate Jaccard similarity to be more precise
    union = one_words | two_words
    jaccard = len(overlap) / len(union)

    # Require high similarity (>= 0.7) to merge
    if jaccard < 0.7:
        return False

    # Additional safeguard: both teams must share at least 2 words,
    # or the entire shorter name must be contained in the longer.
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
    """Return True if the record is not older than MAX_ODDS_AGE_MINUTES."""
    if MAX_ODDS_AGE_MINUTES <= 0:
        return True
    scraped_at = record.get("scraped_at")
    if not scraped_at:
        return True  # If no timestamp, assume fresh
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

        # Apply freshness filter
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
    return re.sub(r"([_*`\[\]])", r"\\\1", value)


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
    message = "\n".join(lines)

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
# Generic HTML scraper
# =============================================================================

def scrape_generic_html(
    bookmaker_name: str,
    base_url: str,
    match_selector: str,
    home_selector: str,
    away_selector: str,
    odds_1_selector: str,
    odds_x_selector: Optional[str] = None,
    odds_2_selector: Optional[str] = None,
    over_selector: Optional[str] = None,
    under_selector: Optional[str] = None,
    next_page_selector: Optional[str] = None,
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
        if re.search(r"([?&])page=\d+", base_url, re.IGNORECASE):
            url = re.sub(r"([?&])page=\d+", lambda m: f"{m.group(1)}page={page}", base_url, flags=re.IGNORECASE)
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
        matches = soup.select(match_selector)
        if not matches:
            logger.warning("%s page %s contained no matches", bookmaker_name, page)
            break

        page_records_before = len(all_odds)

        for match in matches:
            home_el = match.select_one(home_selector)
            away_el = match.select_one(away_selector)
            if not home_el or not away_el:
                continue

            home_team = home_el.get_text(" ", strip=True)
            away_team = away_el.get_text(" ", strip=True)
            if not home_team or not away_team:
                continue
            if normalize_team(home_team) == "" or normalize_team(away_team) == "":
                continue

            def extract_selector_odd(selector: Optional[str]) -> Optional[float]:
                if not selector:
                    return None
                element = match.select_one(selector)
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
            next_button = soup.select_one(next_page_selector)
            if not next_button:
                break
            classes = set(next_button.get("class", []))
            if "disabled" in classes or next_button.has_attr("disabled"):
                break
        else:
            next_link = soup.find("a", string=re.compile(r"^\s*(Next|»|→)\s*$", re.IGNORECASE))
            if not next_link:
                break

        page += 1
        if page <= max_pages:
            time.sleep(1)

    logger.info("%s: %s records", bookmaker_name, len(all_odds))
    return all_odds


# =============================================================================
# SportyBet
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
# SportyBet official
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
        categories = football.get("categories", [])
        for category in categories:
            if not isinstance(category, dict):
                continue
            tournaments = category.get("tournaments", [])
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
# ChampionBet helpers
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
        item_market_key = item.get("_market_key")
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
        if item_market_key in {"1", "4", "7"} and home is None:
            home = odd
        elif item_market_key in {"2", "5", "8"} and draw is None:
            draw = odd
        elif item_market_key in {"3", "6", "9"} and away is None:
            away = odd
    return home, draw, away


def extract_championbet_ou_all(bet_map: Dict[str, Any]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    ou: Dict[str, Dict[str, Optional[float]]] = {}
    items = championbet_market_items(bet_map, [21, 22, 51, 52])
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = normalize_label(item_label(item))
        line = item.get("P")
        if line is None:
            line = item.get("p")
        if line is None:
            line = item.get("specifier")
        if line is None:
            line = item.get("line")
        if line is None:
            continue
        line_key = normalize_market_specifier(line)
        if not line_key:
            continue
        ou.setdefault(line_key, {"over": None, "under": None})
        if "over" in label:
            if ou[line_key]["over"] is None or odd > ou[line_key]["over"]:
                ou[line_key]["over"] = odd
        elif "under" in label:
            if ou[line_key]["under"] is None or odd > ou[line_key]["under"]:
                ou[line_key]["under"] = odd
    # Fallback by market key
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        line = item.get("P")
        if line is None:
            line = item.get("p")
        if line is None:
            line = item.get("specifier")
        if line is None:
            line = item.get("line")
        if line is None:
            continue
        line_key = normalize_market_specifier(line)
        if not line_key:
            continue
        ou.setdefault(line_key, {"over": None, "under": None})
        item_market_key = item.get("_market_key")
        if item_market_key in {"21", "51"} and ou[line_key]["over"] is None:
            ou[line_key]["over"] = odd
        elif item_market_key in {"22", "52"} and ou[line_key]["under"] is None:
            ou[line_key]["under"] = odd
    result = {}
    for line, vals in ou.items():
        if vals["over"] is not None and vals["under"] is not None:
            result[line] = (vals["over"], vals["under"])
    return result


def extract_championbet_double_chance(bet_map: Dict[str, Any]) -> Dict[str, Optional[float]]:
    dc = {"1X": None, "12": None, "X2": None}
    items = championbet_market_items(bet_map, [20, 21, 22])
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = normalize_label(item_label(item))
        if label in {"1x", "homedraw", "homeordraw"}:
            dc["1X"] = odd
        elif label in {"12", "homeaway"}:
            dc["12"] = odd
        elif label in {"x2", "drawaway", "draworaway"}:
            dc["X2"] = odd
    return dc


def extract_championbet_ah(bet_map: Dict[str, Any]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    asian: Dict[str, List[Optional[float]]] = {}
    items = championbet_market_items(bet_map, [5, 6, 7, 8])
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        line = item.get("P")
        if line is None:
            line = item.get("p")
        if line is None:
            line = item.get("specifier")
        if line is None:
            line = item.get("line")
        if line is None:
            continue
        line_key = normalize_market_specifier(line)
        if not line_key:
            continue
        asian.setdefault(line_key, [None, None])
        label = normalize_label(item_label(item))
        market_key_value = item.get("_market_key")
        if label in {"home", "1", "1handicap", "homehandicap"} or market_key_value in {"5", "7"}:
            asian[line_key][0] = odd
        elif label in {"away", "2", "2handicap", "awayhandicap"} or market_key_value in {"6", "8"}:
            asian[line_key][1] = odd
    result = {}
    for line, values in asian.items():
        if values[0] is not None and values[1] is not None:
            result[line] = (values[0], values[1])
    return result


def extract_championbet_btts(bet_map: Dict[str, Any]) -> Dict[str, Optional[float]]:
    btts = {"yes": None, "no": None}
    items = championbet_market_items(bet_map, [19, 20])
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = normalize_label(item_label(item))
        if label in {"yes", "y", "bothteamstoscoreyes"}:
            btts["yes"] = odd
        elif label in {"no", "n", "bothteamstoscoreno"}:
            btts["no"] = odd
    return btts


def append_championbet_match(odds: List[Dict[str, Any]], match: Dict[str, Any], bookmaker: str) -> None:
    match_id = match.get("id") or match.get("matchId")
    if not match_id:
        return
    home = match.get("home") or match.get("homeName") or match.get("homeTeam")
    away = match.get("away") or match.get("awayName") or match.get("awayTeam")
    if not home or not away:
        return
    if normalize_team(home) == "" or normalize_team(away) == "":
        return
    bet_map = match.get("betMap", {})
    if not isinstance(bet_map, dict):
        bet_map = {}
    competition = match.get("leagueName") or match.get("competitionName") or ""

    home_odd, draw_odd, away_odd = extract_championbet_1x2(bet_map)
    if home_odd and away_odd:
        odds.append(build_match_record(home, away, bookmaker, home_odd, draw_odd, away_odd, competition=competition, event_id=match_id))

    ou_lines = extract_championbet_ou_all(bet_map)
    for line, (over_odd, under_odd) in ou_lines.items():
        odds.append(
            build_match_record(
                home,
                away,
                bookmaker,
                over_odd,
                under_odd,
                None,
                competition=competition,
                market_type=f"Over/Under {line}",
                market_specifier=line,
                event_id=match_id,
            )
        )

    ah_lines = extract_championbet_ah(bet_map)
    for line, (ah_home, ah_away) in ah_lines.items():
        odds.append(
            build_match_record(
                home,
                away,
                bookmaker,
                ah_home,
                None,
                ah_away,
                competition=competition,
                market_type="Asian Handicap",
                market_specifier=line,
                event_id=match_id,
            )
        )

    btts = extract_championbet_btts(bet_map)
    if btts.get("yes") and btts.get("no"):
        odds.append(
            build_match_record(
                home,
                away,
                bookmaker,
                btts["yes"],
                None,
                btts["no"],
                competition=competition,
                market_type="BTTS",
                event_id=match_id,
            )
        )

    # Double Chance – only add if we have both components and they are not identical
    dc = extract_championbet_double_chance(bet_map)
    if dc.get("1X") and away_odd and dc["1X"] != away_odd:
        odds.append(
            build_match_record(
                home,
                away,
                bookmaker,
                dc["1X"],
                None,
                away_odd,
                competition=competition,
                market_type="Double Chance 1X vs 2",
                event_id=match_id,
            )
        )
    if dc.get("X2") and home_odd and dc["X2"] != home_odd:
        odds.append(
            build_match_record(
                home,
                away,
                bookmaker,
                dc["X2"],
                None,
                home_odd,
                competition=competition,
                market_type="Double Chance X2 vs 1",
                event_id=match_id,
            )
        )
    if dc.get("12") and draw_odd and dc["12"] != draw_odd:
        odds.append(
            build_match_record(
                home,
                away,
                bookmaker,
                dc["12"],
                None,
                draw_odd,
                competition=competition,
                market_type="Double Chance 12 vs X",
                event_id=match_id,
            )
        )


def scrape_championbet() -> List[Dict[str, Any]]:
    logger.info("Fetching ChampionBet...")
    odds = []
    try:
        data = http.get_json(CHAMPIONBET_API, headers={"Referer": "https://www.championbet.ug/"})
        matches = data.get("esMatches", []) if isinstance(data, dict) else []
        if not isinstance(matches, list):
            raise ValueError("ChampionBet esMatches is not a list")
        for match in matches:
            if not isinstance(match, dict):
                continue
            try:
                match_id = match.get("id") or match.get("matchId")
                if not match_id:
                    continue
                match_data = http.get_json(
                    CHAMPIONBET_MATCH_API.format(match_id=match_id),
                    headers={"Referer": "https://www.championbet.ug/"},
                )
                if not isinstance(match_data, dict):
                    continue
                enriched = dict(match)
                enriched["betMap"] = match_data.get("betMap", {})
                append_championbet_match(odds, enriched, "ChampionBet")
                time.sleep(0.1)
            except Exception:
                logger.exception("ChampionBet match failed")
        logger.info("ChampionBet: %s records", len(odds))
    except Exception as exc:
        logger.exception("ChampionBet error: %s", exc)
    return odds


def scrape_championbet_official() -> List[Dict[str, Any]]:
    logger.info("Fetching ChampionBet official API...")
    odds = []
    try:
        offset = 0
        page_size = 100
        seen_match_ids = set()
        while True:
            url = f"{CHAMPIONBET_TOP_API}&offset={offset}&limit={page_size}"
            data = http.get_json(url, headers={"Referer": "https://www.championbet.ug/"})
            if not isinstance(data, dict):
                raise ValueError("ChampionBet official API returned invalid JSON")
            matches = data.get("esMatches", [])
            if not matches:
                break
            if not isinstance(matches, list):
                break
            new_matches = 0
            for match in matches:
                if not isinstance(match, dict):
                    continue
                match_id = match.get("id")
                if not match_id:
                    continue
                if str(match_id) in seen_match_ids:
                    continue
                seen_match_ids.add(str(match_id))
                new_matches += 1
                try:
                    append_championbet_match(odds, match, "ChampionBetOfficial")
                except Exception:
                    logger.exception("ChampionBet official match processing error")
            total = safe_int(data.get("totalMatchCount"), 0)
            if total and offset + len(matches) >= total:
                break
            if new_matches == 0:
                logger.warning("ChampionBet pagination returned no new matches")
                break
            offset += page_size
            time.sleep(0.5)
        logger.info("ChampionBetOfficial: %s records", len(odds))
    except Exception as exc:
        logger.exception("ChampionBet official API error: %s", exc)
    return odds


# =============================================================================
# AbaBet
# =============================================================================

def scrape_ababet() -> List[Dict[str, Any]]:
    logger.info("Fetching AbaBet...")
    odds = []
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.7827.159 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.ababet.ug/",
        }
    )
    base_url = "https://www.ababet.ug/soccer/match_result?mobile=1"
    page = 1
    while page <= MAX_HTML_PAGES:
        url = f"{base_url}&page={page}"
        try:
            response = session.get(url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            html = response.text
        except requests.RequestException as exc:
            logger.error("AbaBet page %s fetch error: %s", page, exc)
            break
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        found_table = False
        for table in tables:
            first_row = table.find("tr")
            if not first_row:
                continue
            headers = [cell.get_text(" ", strip=True) for cell in first_row.find_all(["th", "td"])]
            if "Home" not in headers or "Away" not in headers:
                continue
            found_table = True
            for row in table.find_all("tr")[1:]:
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
                if len(cells) < 5:
                    continue
                row_data = dict(zip(headers, cells))
                home = row_data.get("Home")
                away = row_data.get("Away")
                if not home or not away or home == "-" or away == "-":
                    continue
                if normalize_team(home) == "" or normalize_team(away) == "":
                    continue
                home_odd = clean_odd(row_data.get("1"))
                draw_odd = clean_odd(row_data.get("X"))
                away_odd = clean_odd(row_data.get("2"))
                competition = row_data.get("League", "") or ""
                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "AbaBet", home_odd, draw_odd, away_odd, competition=competition))
                over_odd = clean_odd(row_data.get("Over"))
                under_odd = clean_odd(row_data.get("Under"))
                if over_odd and under_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "AbaBet",
                            over_odd,
                            under_odd,
                            None,
                            competition=competition,
                            market_type="Over/Under 2.5",
                            market_specifier="2.5",
                        )
                    )
        if not found_table:
            break
        next_link = soup.find("a", string=re.compile(r"^\s*(Next|»|→)\s*$", re.IGNORECASE))
        if not next_link:
            break
        page += 1
        time.sleep(1)
    logger.info("AbaBet: %s records", len(odds))
    return odds


# =============================================================================
# Fortebet
# =============================================================================

def scrape_fortebet() -> List[Dict[str, Any]]:
    logger.info("Fetching Fortebet...")
    odds = []
    try:
        url = "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en"
        data = http.get_json(url, headers={"Referer": "https://desktop.fortebet.ug/prematch/landing"})
        if not isinstance(data, dict):
            raise ValueError("Fortebet response is not an object")
        inner = data.get("data", {})
        if not isinstance(inner, dict):
            return odds
        events = inner.get("event", {})
        markets = inner.get("markets", {})
        competitors = inner.get("competitors", {})
        if not all(isinstance(v, dict) for v in (events, markets, competitors)):
            return odds
        event_markets: Dict[str, List[Dict[str, Any]]] = {}
        for market in markets.values():
            if not isinstance(market, dict):
                continue
            event_id = str(market.get("eventId", ""))
            if event_id:
                event_markets.setdefault(event_id, []).append(market)
        for event_id, event in events.items():
            if not isinstance(event, dict):
                continue
            try:
                comp_ids = event.get("competitors", [])
                if not isinstance(comp_ids, list) or len(comp_ids) < 2:
                    continue
                home_data = competitors.get(str(comp_ids[0]), {})
                away_data = competitors.get(str(comp_ids[1]), {})
                home = home_data.get("name") if isinstance(home_data, dict) else ""
                away = away_data.get("name") if isinstance(away_data, dict) else ""
                if not home or not away:
                    continue
                if normalize_team(home) == "" or normalize_team(away) == "":
                    continue
                home_odd = None
                draw_odd = None
                away_odd = None
                ou_lines: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
                asian: Dict[str, Dict[str, Optional[float]]] = {}
                dc = {"1X": None, "12": None, "X2": None}
                btts_yes = None
                btts_no = None
                for market in event_markets.get(str(event_id), []):
                    market_id = safe_int(market.get("marketId"), -1)
                    market_odds = market.get("odds", {})
                    if not isinstance(market_odds, dict):
                        continue
                    for value in market_odds.values():
                        if not isinstance(value, dict):
                            continue
                        odd = clean_odd(value.get("odds"))
                        if odd is None:
                            continue
                        outcome_id = safe_int(value.get("outcomeId"), -1)
                        if market_id == 1:  # 1X2
                            if outcome_id == 1:
                                home_odd = max(home_odd or 0, odd)
                            elif outcome_id == 2:
                                draw_odd = max(draw_odd or 0, odd)
                            elif outcome_id == 3:
                                away_odd = max(away_odd or 0, odd)
                        elif market_id == 5:  # Over/Under 2.5
                            if outcome_id == 1:
                                if "2.5" not in ou_lines:
                                    ou_lines["2.5"] = [None, None]
                                ou_lines["2.5"][0] = odd
                            elif outcome_id == 2:
                                if "2.5" not in ou_lines:
                                    ou_lines["2.5"] = [None, None]
                                ou_lines["2.5"][1] = odd
                        elif market_id == 2:  # Asian Handicap
                            line = value.get("specifier")
                            if line is None:
                                line = value.get("hcp")
                            if line is None:
                                line = value.get("line")
                            if line is None:
                                line = value.get("handicap")
                            if line is None:
                                line = market.get("specifier")
                            if line is None:
                                continue
                            line_key = normalize_market_specifier(line)
                            if not line_key:
                                continue
                            asian.setdefault(line_key, {"home": None, "away": None})
                            if outcome_id == 1:
                                asian[line_key]["home"] = odd
                            elif outcome_id == 2:
                                asian[line_key]["away"] = odd
                        elif market_id == 8:  # Double Chance
                            # Fortebet mapping: 1=1X, 2=12, 3=X2 (assumed)
                            if outcome_id == 1:
                                dc["1X"] = odd
                            elif outcome_id == 2:
                                dc["12"] = odd
                            elif outcome_id == 3:
                                dc["X2"] = odd
                        elif market_id == 12:  # BTTS
                            if outcome_id == 1:
                                btts_yes = odd
                            elif outcome_id == 2:
                                btts_no = odd
                sport_raw = str(event.get("sportName", event.get("sport", "")) or "").lower()
                if "basketball" in sport_raw:
                    sport_name = "Basketball"
                elif "tennis" in sport_raw:
                    sport_name = "Tennis"
                elif "rugby" in sport_raw:
                    sport_name = "Rugby"
                else:
                    sport_name = "Football"
                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "Fortebet", home_odd, draw_odd, away_odd, sport=sport_name, event_id=event_id))
                for line, vals in ou_lines.items():
                    over_odd, under_odd = vals
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
                                market_type=f"Over/Under {line}",
                                market_specifier=line,
                                event_id=event_id,
                            )
                        )
                for line, vals in asian.items():
                    ah_home, ah_away = vals.get("home"), vals.get("away")
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
                                market_specifier=line,
                                event_id=event_id,
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
                            event_id=event_id,
                        )
                    )
                # Double Chance
                if dc.get("1X") and away_odd and dc["1X"] != away_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "Fortebet",
                            dc["1X"],
                            None,
                            away_odd,
                            sport=sport_name,
                            market_type="Double Chance 1X vs 2",
                            event_id=event_id,
                        )
                    )
                if dc.get("X2") and home_odd and dc["X2"] != home_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "Fortebet",
                            dc["X2"],
                            None,
                            home_odd,
                            sport=sport_name,
                            market_type="Double Chance X2 vs 1",
                            event_id=event_id,
                        )
                    )
                if dc.get("12") and draw_odd and dc["12"] != draw_odd:
                    odds.append(
                        build_match_record(
                            home,
                            away,
                            "Fortebet",
                            dc["12"],
                            None,
                            draw_odd,
                            sport=sport_name,
                            market_type="Double Chance 12 vs X",
                            event_id=event_id,
                        )
                    )
            except Exception:
                logger.exception("Fortebet event failed")
        logger.info("Fortebet: %s records", len(odds))
    except Exception as exc:
        logger.exception("Fortebet error: %s", exc)
    return odds


# =============================================================================
# Shared 1xBet / 22Bet feed
# =============================================================================

def shared_feed_url(config: Dict[str, Any]) -> str:
    return (
        f"{config['base_url']}"
        "/service-api/LineFeed/Get1x2_VZip"
        "?count=1000"
        f"&lng={config.get('lng', 'en')}"
        f"&tz={config.get('tz', 3)}"
        "&mode=4"
        "&country=191"
        f"&partner={config['partner']}"
        "&getEmpty=true"
        f"&gr={config.get('gr', 525)}"
    )


def shared_headers(config: Dict[str, Any]) -> Dict[str, str]:
    return {
        "Referer": config.get("referer", config["base_url"]),
        "Origin": config["base_url"],
        "X-Requested-With": "XMLHttpRequest",
    }


def extract_shared_outcomes(match: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "home": None,
        "draw": None,
        "away": None,
        "ou": {},
        "ah": {},
        "dc": {"1X": None, "12": None, "X2": None},
        "btts_yes": None,
        "btts_no": None,
    }
    outcomes = match.get("E", [])
    if not isinstance(outcomes, list):
        return result
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        odd = clean_odd(outcome.get("C"))
        if odd is None:
            continue
        outcome_type = str(outcome.get("T", "")).strip()
        p = outcome.get("P")
        if outcome_type == "1":
            result["home"] = max(result["home"] or 0, odd)
        elif outcome_type == "2":
            result["draw"] = max(result["draw"] or 0, odd)
        elif outcome_type == "3":
            result["away"] = max(result["away"] or 0, odd)
        elif outcome_type in {"7", "8"}:
            if p is None:
                continue
            line = normalize_market_specifier(p)
            if not line:
                continue
            result["ah"].setdefault(line, {"home": None, "away": None})
            if outcome_type == "7":
                result["ah"][line]["home"] = max(result["ah"][line]["home"] or 0, odd)
            else:
                result["ah"][line]["away"] = max(result["ah"][line]["away"] or 0, odd)
        elif outcome_type in {"9", "10"}:
            if p is None:
                continue
            line = safe_float(p, None)
            if line is None:
                continue
            line_key = str(line)
            result["ou"].setdefault(line_key, {"over": None, "under": None})
            if outcome_type == "9":
                result["ou"][line_key]["over"] = max(result["ou"][line_key]["over"] or 0, odd)
            else:
                result["ou"][line_key]["under"] = max(result["ou"][line_key]["under"] or 0, odd)
        elif outcome_type == "4":
            result["dc"]["1X"] = max(result["dc"]["1X"] or 0, odd)
        elif outcome_type == "5":
            result["dc"]["12"] = max(result["dc"]["12"] or 0, odd)
        elif outcome_type == "6":
            result["dc"]["X2"] = max(result["dc"]["X2"] or 0, odd)
        elif outcome_type == "19":
            result["btts_yes"] = max(result["btts_yes"] or 0, odd)
        elif outcome_type == "20":
            result["btts_no"] = max(result["btts_no"] or 0, odd)
    return result


def extract_shared_extra_from_values(bookmaker: str, values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    all_odds = []
    for match in values:
        if not isinstance(match, dict):
            continue
        home = match.get("O1") or match.get("O1Name")
        away = match.get("O2") or match.get("O2Name")
        if not home or not away:
            continue
        if normalize_team(home) == "" or normalize_team(away) == "":
            continue
        extracted = extract_shared_outcomes(match)
        event_id = match.get("I") or match.get("Id")
        # Asian Handicap
        for line, vals in extracted["ah"].items():
            ah_home, ah_away = vals.get("home"), vals.get("away")
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
                        market_specifier=line,
                        event_id=event_id,
                    )
                )
        # Over/Under all lines
        for line, vals in extracted["ou"].items():
            over_odd, under_odd = vals.get("over"), vals.get("under")
            if over_odd and under_odd:
                all_odds.append(
                    build_match_record(
                        home,
                        away,
                        bookmaker,
                        over_odd,
                        under_odd,
                        None,
                        market_type=f"Over/Under {line}",
                        market_specifier=line,
                        event_id=event_id,
                    )
                )
        # BTTS
        if extracted["btts_yes"] and extracted["btts_no"]:
            all_odds.append(
                build_match_record(
                    home,
                    away,
                    bookmaker,
                    extracted["btts_yes"],
                    None,
                    extracted["btts_no"],
                    market_type="BTTS",
                    event_id=event_id,
                )
            )
        # Double Chance – verify all components are present and distinct
        dc = extracted["dc"]
        if dc["1X"] and extracted["away"] and dc["1X"] != extracted["away"]:
            all_odds.append(
                build_match_record(
                    home,
                    away,
                    bookmaker,
                    dc["1X"],
                    None,
                    extracted["away"],
                    market_type="Double Chance 1X vs 2",
                    event_id=event_id,
                )
            )
        if dc["X2"] and extracted["home"] and dc["X2"] != extracted["home"]:
            all_odds.append(
                build_match_record(
                    home,
                    away,
                    bookmaker,
                    dc["X2"],
                    None,
                    extracted["home"],
                    market_type="Double Chance X2 vs 1",
                    event_id=event_id,
                )
            )
        if dc["12"] and extracted["draw"] and dc["12"] != extracted["draw"]:
            all_odds.append(
                build_match_record(
                    home,
                    away,
                    bookmaker,
                    dc["12"],
                    None,
                    extracted["draw"],
                    market_type="Double Chance 12 vs X",
                    event_id=event_id,
                )
            )
    return all_odds


def scrape_shared_full(bookmaker: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    logger.info("Fetching %s full feed...", bookmaker)
    odds = []
    try:
        data = http.get_json(shared_feed_url(config), headers=shared_headers(config))
        values = data.get("Value", []) if isinstance(data, dict) else []
        if not isinstance(values, list):
            raise ValueError("Shared feed Value is not a list")
        # 1X2
        for match in values:
            if not isinstance(match, dict):
                continue
            home = match.get("O1") or match.get("O1Name")
            away = match.get("O2") or match.get("O2Name")
            if not home or not away:
                continue
            if normalize_team(home) == "" or normalize_team(away) == "":
                continue
            extracted = extract_shared_outcomes(match)
            event_id = match.get("I") or match.get("Id")
            if extracted["home"] and extracted["away"]:
                odds.append(
                    build_match_record(
                        home,
                        away,
                        bookmaker,
                        extracted["home"],
                        extracted["draw"],
                        extracted["away"],
                        event_id=event_id,
                    )
                )
        # Extra markets
        odds.extend(extract_shared_extra_from_values(bookmaker, values))
        logger.info("%s: %s records (1x2 + extras)", bookmaker, len(odds))
    except Exception as exc:
        logger.exception("%s full feed error: %s", bookmaker, exc)
    return odds


# =============================================================================
# kbet
# =============================================================================

def scrape_kbet() -> List[Dict[str, Any]]:
    logger.info("Fetching kbet...")
    odds = []
    try:
        data = http.get_json(KBET_API_BASE, params={"status": "Scheduled", "sport_id": 1, "limit": 100})
        if isinstance(data, list):
            events = data
        elif isinstance(data, dict):
            events = data.get("data", [])
            if not events:
                events = data.get("events", [])
        else:
            events = []
        if not isinstance(events, list):
            events = []
        for event in events:
            if not isinstance(event, dict):
                continue
            home = event.get("home_team") or event.get("home") or ""
            away = event.get("away_team") or event.get("away") or ""
            if not home or not away:
                continue
            if normalize_team(home) == "" or normalize_team(away) == "":
                continue
            home_odd = clean_odd(event.get("home_odd") or event.get("odds_1"))
            draw_odd = clean_odd(event.get("draw_odd") or event.get("odds_x"))
            away_odd = clean_odd(event.get("away_odd") or event.get("odds_2"))
            if not home_odd and isinstance(event.get("odds"), dict):
                nested = event["odds"]
                home_odd = clean_odd(nested.get("1") or nested.get("home"))
                draw_odd = clean_odd(nested.get("x") or nested.get("draw"))
                away_odd = clean_odd(nested.get("2") or nested.get("away"))
            if home_odd and away_odd:
                odds.append(
                    build_match_record(
                        home,
                        away,
                        "kbet",
                        home_odd,
                        draw_odd,
                        away_odd,
                        sport="Football",
                        event_id=event.get("id"),
                    )
                )
        logger.info("kbet: %s records", len(odds))
    except Exception as exc:
        logger.exception("kbet error: %s", exc)
    return odds


# =============================================================================
# HTML bookmaker wrappers
# =============================================================================

def scrape_melbet_html() -> List[Dict[str, Any]]:
    return scrape_generic_html(
        bookmaker_name="Melbet",
        base_url="https://melbet.ug/line",
        match_selector="div.event, div.match, div.game-item, div.fixture",
        home_selector=".home, .team-home, .home-team, .home-name",
        away_selector=".away, .team-away, .away-team, .away-name",
        odds_1_selector=".odd-home, .home-odd, .odd-1, .btn-home",
        odds_x_selector=".odd-draw, .draw-odd, .odd-x, .btn-draw",
        odds_2_selector=".odd-away, .away-odd, .odd-2, .btn-away",
        over_selector=".over, .over-odd",
        under_selector=".under, .under-odd",
        next_page_selector="a.next, .pagination-next",
    )


def scrape_betway() -> List[Dict[str, Any]]:
    return scrape_generic_html(
        bookmaker_name="Betway",
        base_url="https://betway.ug/en/sports/football",
        match_selector="div.event-wrapper, div.event-item, div.match-row, div.game-row",
        home_selector="span.home-team, div.team-home, .home-name, .home",
        away_selector="span.away-team, div.team-away, .away-name, .away",
        odds_1_selector="button.odds-1, .odd-home, .home-odd, .btn-home",
        odds_x_selector="button.odds-x, .odd-draw, .draw-odd, .btn-draw",
        odds_2_selector="button.odds-2, .odd-away, .away-odd, .btn-away",
        over_selector=".over-odd, .over",
        under_selector=".under-odd, .under",
        next_page_selector="a.next, .pagination-next, .next-page",
    )


def scrape_betpawa() -> List[Dict[str, Any]]:
    return scrape_generic_html(
        bookmaker_name="BetPawa",
        base_url="https://www.betpawa.ug/en/sports/football",
        match_selector="div.fixture, div.match-item, div.game-row, div.event",
        home_selector=".home-team, .team-home, .home, .home-name",
        away_selector=".away-team, .team-away, .away, .away-name",
        odds_1_selector=".bet-button-home, .odd-1, .home-odd, .btn-home",
        odds_x_selector=".bet-button-draw, .odd-x, .draw-odd, .btn-draw",
        odds_2_selector=".bet-button-away, .odd-2, .away-odd, .btn-away",
        over_selector=".over, .over-odd",
        under_selector=".under, .under-odd",
        next_page_selector="a.next, .pagination-next",
    )


def scrape_premierbet() -> List[Dict[str, Any]]:
    return scrape_generic_html(
        bookmaker_name="PremierBet",
        base_url="https://www.premierbet.ug/en/sports/football",
        match_selector="div.event, div.match, div.game-item, div.fixture",
        home_selector=".home, .team-home, .home-team, .home-name",
        away_selector=".away, .team-away, .away-team, .away-name",
        odds_1_selector=".odd-home, .home-odd, .odd-1, .btn-home",
        odds_x_selector=".odd-draw, .draw-odd, .odd-x, .btn-draw",
        odds_2_selector=".odd-away, .away-odd, .odd-2, .btn-away",
        over_selector=".over, .over-odd",
        under_selector=".under, .under-odd",
        next_page_selector="a.next, .pagination-next",
    )


# =============================================================================
# Arbitrage calculations
# =============================================================================

def calculate_stakes(odds: List[float], total_stake: int) -> List[int]:
    if total_stake <= 0 or not odds:
        return []
    valid_odds = []
    for odd in odds:
        cleaned = clean_odd(odd)
        if cleaned is None or cleaned <= 1:
            return []
        valid_odds.append(cleaned)
    inverse_sum = sum(1 / odd for odd in valid_odds)
    if not math.isfinite(inverse_sum) or inverse_sum <= 0:
        return []
    raw_stakes = [total_stake * (1 / odd) / inverse_sum for odd in valid_odds]
    stakes = [int(round(value)) for value in raw_stakes]
    difference = total_stake - sum(stakes)
    if not stakes:
        return []
    stakes[-1] += difference
    if any(stake <= 0 for stake in stakes):
        return []
    return stakes


def create_two_outcome_opportunity(
    match: str,
    sport: str,
    market_type: str,
    market_specifier: str,
    first_bm: str,
    first_outcome: str,
    first_odd: float,
    second_bm: str,
    second_outcome: str,
    second_odd: float,
    stake: int = DEFAULT_STAKE,
) -> Optional[Dict[str, Any]]:
    first_odd = clean_odd(first_odd)
    second_odd = clean_odd(second_odd)
    if (
        first_odd is None
        or second_odd is None
        or first_odd <= 1
        or second_odd <= 1
        or first_bm == second_bm
    ):
        return None
    arb_sum = 1 / first_odd + 1 / second_odd
    if not math.isfinite(arb_sum) or arb_sum >= 1:
        return None
    profit_percent = (1 - arb_sum) * 100
    if not (MIN_ARB_PROFIT_PERCENT <= profit_percent <= MAX_ARB_PROFIT_PERCENT):
        return None
    # Log warning for suspiciously high profit
    if profit_percent > WARN_PROFIT_PERCENT:
        logger.warning(
            "High profit %.2f%% for %s (%s vs %s). Could be stale odds or mismatch.",
            profit_percent,
            match,
            first_outcome,
            second_outcome,
        )
    stakes = calculate_stakes([first_odd, second_odd], stake)
    if len(stakes) != 2:
        return None
    return {
        "match": match,
        "sport": sport,
        "type": market_type,
        "market_type": market_type,
        "market_specifier": normalize_market_specifier(market_specifier),
        "profit_percent": round(profit_percent, 2),
        "profit_ugx": round(stake * (1 - arb_sum)),
        "total_stake": stake,
        "arb_sum": round(arb_sum, 6),
        "bets": [
            {
                "bookmaker": first_bm,
                "outcome": first_outcome,
                "odd": first_odd,
                "stake": stakes[0],
                "win": round(stakes[0] * first_odd),
            },
            {
                "bookmaker": second_bm,
                "outcome": second_outcome,
                "odd": second_odd,
                "stake": stakes[1],
                "win": round(stakes[1] * second_odd),
            },
        ],
    }


def create_three_outcome_opportunity(
    match: str,
    sport: str,
    home_bm: str,
    home_odd: float,
    draw_bm: str,
    draw_odd: float,
    away_bm: str,
    away_odd: float,
    stake: int = DEFAULT_STAKE,
) -> Optional[Dict[str, Any]]:
    home_odd = clean_odd(home_odd)
    draw_odd = clean_odd(draw_odd)
    away_odd = clean_odd(away_odd)
    if any(odd is None or odd <= 1 for odd in (home_odd, draw_odd, away_odd)):
        return None
    if len({home_bm, draw_bm, away_bm}) != 3:
        return None
    arb_sum = 1 / home_odd + 1 / draw_odd + 1 / away_odd
    if not math.isfinite(arb_sum) or arb_sum >= 1:
        return None
    profit_percent = (1 - arb_sum) * 100
    if not (MIN_ARB_PROFIT_PERCENT <= profit_percent <= MAX_ARB_PROFIT_PERCENT):
        return None
    if profit_percent > WARN_PROFIT_PERCENT:
        logger.warning(
            "High profit %.2f%% for %s (3-way). Could be stale odds or mismatch.",
            profit_percent,
            match,
        )
    stakes = calculate_stakes([home_odd, draw_odd, away_odd], stake)
    if len(stakes) != 3:
        return None
    return {
        "match": match,
        "sport": sport,
        "type": "3-way",
        "market_type": "1x2",
        "market_specifier": "",
        "profit_percent": round(profit_percent, 2),
        "profit_ugx": round(stake * (1 - arb_sum)),
        "total_stake": stake,
        "arb_sum": round(arb_sum, 6),
        "bets": [
            {"bookmaker": home_bm, "outcome": "Home", "odd": home_odd, "stake": stakes[0], "win": round(stakes[0] * home_odd)},
            {"bookmaker": draw_bm, "outcome": "Draw", "odd": draw_odd, "stake": stakes[1], "win": round(stakes[1] * draw_odd)},
            {"bookmaker": away_bm, "outcome": "Away", "odd": away_odd, "stake": stakes[2], "win": round(stakes[2] * away_odd)},
        ],
    }


# =============================================================================
# Arbitrage finder
# =============================================================================

def merge_duplicate_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, Optional[str], str], Dict[str, Any]] = {}
    for record in records:
        key = (
            canonical_bookmaker(record.get("bookmaker")),
            record.get("event_id"),
            record.get("match_key", ""),
        )
        if key not in grouped:
            grouped[key] = dict(record)
        else:
            existing = grouped[key]
            for field in ("home", "draw", "away"):
                old_val = existing.get(field)
                new_val = record.get(field)
                old_odd = clean_odd(old_val)
                new_odd = clean_odd(new_val)
                if new_odd is not None and (old_odd is None or new_odd > old_odd):
                    existing[field] = new_odd
    return list(grouped.values())


def merge_matching_groups(groups: Dict[str, List[Dict[str, Any]]]) -> List[List[Dict[str, Any]]]:
    keys = list(groups.keys())
    merged = []
    visited = set()
    for index, first_key in enumerate(keys):
        if first_key in visited:
            continue
        group = list(groups[first_key])
        visited.add(first_key)
        for second_key in keys[index + 1:]:
            if second_key in visited:
                continue
            if match_key_similarity(first_key, second_key):
                group.extend(groups[second_key])
                visited.add(second_key)
        merged.append(group)
    return merged


def find_arbitrage(all_odds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    opportunities = []
    all_odds = merge_duplicate_records(all_odds)
    sports: Dict[str, List[Dict[str, Any]]] = {}
    for odd in all_odds:
        if not isinstance(odd, dict):
            continue
        sport = odd.get("sport", "Football") or "Football"
        sports.setdefault(sport, []).append(odd)
    for sport, sport_odds in sports.items():
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for odd in sport_odds:
            key = odd.get("match_key", "")
            if key:
                groups.setdefault(key, []).append(odd)
        merged_groups = merge_matching_groups(groups)
        for records in merged_groups:
            if len(records) < 2:
                continue
            first_record = records[0]
            market_type = first_record.get("market_type", "1x2")
            specifier = first_record.get("market_specifier", "")
            match_name = first_record.get("match", "")
            if market_type == "Double Chance":
                continue  # Legacy, ignore
            # 3-way 1X2
            if market_type == "1x2":
                best_home: Dict[str, float] = {}
                best_draw: Dict[str, float] = {}
                best_away: Dict[str, float] = {}
                for record in records:
                    bm = canonical_bookmaker(record.get("bookmaker"))
                    if not bm:
                        continue
                    home = clean_odd(record.get("home"))
                    draw = clean_odd(record.get("draw"))
                    away = clean_odd(record.get("away"))
                    if home and (bm not in best_home or home > best_home[bm]):
                        best_home[bm] = home
                    if draw and (bm not in best_draw or draw > best_draw[bm]):
                        best_draw[bm] = draw
                    if away and (bm not in best_away or away > best_away[bm]):
                        best_away[bm] = away
                for home_bm, home_odd in best_home.items():
                    for draw_bm, draw_odd in best_draw.items():
                        for away_bm, away_odd in best_away.items():
                            if len({home_bm, draw_bm, away_bm}) != 3:
                                continue
                            opportunity = create_three_outcome_opportunity(
                                match_name,
                                sport,
                                home_bm,
                                home_odd,
                                draw_bm,
                                draw_odd,
                                away_bm,
                                away_odd,
                            )
                            if opportunity:
                                opportunities.append(opportunity)
                continue
            # 2-way markets
            bookmakers: Dict[str, Dict[str, float]] = {}
            for record in records:
                bm = canonical_bookmaker(record.get("bookmaker"))
                if not bm:
                    continue
                bookmakers.setdefault(bm, {"home": 0.0, "away": 0.0})
                home = clean_odd(record.get("home"))
                away = clean_odd(record.get("away"))
                if home:
                    bookmakers[bm]["home"] = max(bookmakers[bm]["home"], home)
                if away:
                    bookmakers[bm]["away"] = max(bookmakers[bm]["away"], away)
            # Determine outcome names
            if market_type == "Over/Under 2.5":
                outcome1, outcome2 = "Over", "Under"
            elif market_type.startswith("Over/Under "):
                line = market_type.split(" ")[-1]
                outcome1, outcome2 = f"Over {line}", f"Under {line}"
            elif market_type == "Asian Handicap":
                outcome1, outcome2 = "Home", "Away"
            elif market_type == "BTTS":
                outcome1, outcome2 = "Yes", "No"
            elif market_type == "Double Chance 1X vs 2":
                outcome1, outcome2 = "1X", "2"
            elif market_type == "Double Chance X2 vs 1":
                outcome1, outcome2 = "X2", "1"
            elif market_type == "Double Chance 12 vs X":
                outcome1, outcome2 = "12", "X"
            else:
                logger.debug("Skipping unsupported market type: %s", market_type)
                continue
            bookmaker_names = list(bookmakers.keys())
            for i, bm1 in enumerate(bookmaker_names):
                for bm2 in bookmaker_names[i + 1:]:
                    if bm1 == bm2:
                        continue
                    combinations = [
                        (bm1, bookmakers[bm1]["home"], bm2, bookmakers[bm2]["away"]),
                        (bm2, bookmakers[bm2]["home"], bm1, bookmakers[bm1]["away"]),
                    ]
                    for first_bm, first_odd, second_bm, second_odd in combinations:
                        if not (first_odd and second_odd):
                            continue
                        opportunity = create_two_outcome_opportunity(
                            match_name,
                            sport,
                            market_type,
                            specifier,
                            first_bm,
                            outcome1,
                            first_odd,
                            second_bm,
                            outcome2,
                            second_odd,
                        )
                        if opportunity:
                            opportunities.append(opportunity)
    # Final deduplication
    best: Dict[str, Dict[str, Any]] = {}
    for opportunity in opportunities:
        bets_key = "|".join(
            sorted(f"{bet.get('bookmaker')}:{bet.get('outcome')}" for bet in opportunity.get("bets", []))
        )
        identifier = "::".join(
            [
                str(opportunity.get("match", "")),
                str(opportunity.get("sport", "")),
                str(opportunity.get("market_type", "")),
                str(opportunity.get("market_specifier", "")),
                bets_key,
            ]
        )
        existing = best.get(identifier)
        if existing is None or opportunity["profit_percent"] > existing["profit_percent"]:
            best[identifier] = opportunity
    result = list(best.values())
    result.sort(key=lambda x: x.get("profit_percent", 0), reverse=True)
    return result


# =============================================================================
# Current opportunities
# =============================================================================

def load_current_opportunities() -> List[Dict[str, Any]]:
    if not os.path.exists(OPPORTUNITIES_FILE):
        return []
    try:
        with open(OPPORTUNITIES_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except Exception:
        logger.exception("Unable to read current opportunities")
        return []


# =============================================================================
# Scanner core
# =============================================================================

def run_scan() -> List[Dict[str, Any]]:
    with scan_lock:
        reset_scanner_status()
        logger.info("=" * 60)
        logger.info("STARTING ARBITRAGE SCAN")
        logger.info("=" * 60)
        all_odds: List[Dict[str, Any]] = []
        scrapers = [
            ("SportyBet", scrape_sportybet),
            ("SportyBetOfficial", scrape_sportybet_official),
            ("ChampionBet", scrape_championbet),
            ("ChampionBetOfficial", scrape_championbet_official),
            ("AbaBet", scrape_ababet),
            ("Fortebet", scrape_fortebet),
            ("1xBet", lambda: scrape_shared_full("1xBet", SHARED_BOOKMAKERS["1xBet"])),
            ("22Bet", lambda: scrape_shared_full("22Bet", SHARED_BOOKMAKERS["22Bet"])),
            ("Melbet", scrape_melbet_html),
            ("kbet", scrape_kbet),
            ("Betway", scrape_betway),
            ("BetPawa", scrape_betpawa),
            ("PremierBet", scrape_premierbet),
        ]
        for bookmaker, scraper in scrapers:
            records = scraper_call(bookmaker, scraper)
            all_odds.extend(records)
        # Count healthy bookmakers
        with status_lock:
            bookmaker_status = dict(scanner_status["bookmakers"])
            healthy_canonical = set()
            for source_name, status in bookmaker_status.items():
                if source_name == "SharedExtraMarkets":
                    continue
                if status.get("success", False) and status.get("records", 0) > 0:
                    healthy_canonical.add(canonical_bookmaker(source_name))
            healthy_bookmakers = len(healthy_canonical)
            scanner_status["healthy_bookmakers"] = healthy_bookmakers
            scanner_status["total_odds"] = len(all_odds)
        logger.info("Total usable odds records: %s", len(all_odds))
        logger.info("Healthy real bookmakers: %s", healthy_bookmakers)
        if healthy_bookmakers < MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN:
            error = (
                "Scanner returned too few healthy bookmakers. "
                "Current opportunities were NOT overwritten."
            )
            logger.error("%s Healthy=%s Required=%s", error, healthy_bookmakers, MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN)
            with status_lock:
                scanner_status["last_scan_success"] = False
                scanner_status["last_scan_valid"] = False
                scanner_status["last_scan_error"] = error
                scanner_status["last_scan_finished"] = utc_now().isoformat()
            save_scanner_status()
            return load_current_opportunities()
        opportunities = find_arbitrage(all_odds)
        logger.info("Found %s arbitrage opportunities", len(opportunities))
        with history_lock:
            history = load_arbitrage_history()
            existing_keys = set(history.keys())
        new_opportunities = [opp for opp in opportunities if opportunity_key(opp) not in existing_keys]
        logger.info("New opportunities: %s", len(new_opportunities))
        try:
            atomic_json_write(OPPORTUNITIES_FILE, opportunities)
            logger.info("Opportunities written to %s", OPPORTUNITIES_FILE)
        except Exception as exc:
            error = f"Failed to write opportunities: {exc}"
            logger.exception(error)
            with status_lock:
                scanner_status["last_scan_success"] = False
                scanner_status["last_scan_valid"] = False
                scanner_status["last_scan_error"] = error
                scanner_status["last_scan_finished"] = utc_now().isoformat()
            save_scanner_status()
            return load_current_opportunities()
        try:
            with history_lock:
                timestamp = utc_timestamp()
                update_arbitrage_history(opportunities, history, timestamp)
                save_arbitrage_history(history)
        except Exception as exc:
            error = f"Failed to update arbitrage history: {exc}"
            logger.exception(error)
            with status_lock:
                scanner_status["last_scan_success"] = False
                scanner_status["last_scan_valid"] = False
                scanner_status["last_scan_error"] = error
                scanner_status["last_scan_finished"] = utc_now().isoformat()
            save_scanner_status()
            return opportunities
        for opportunity in new_opportunities:
            profit = safe_float(opportunity.get("profit_percent", 0))
            if profit >= TELEGRAM_MIN_PROFIT:
                send_telegram_alert(opportunity)
        with status_lock:
            scanner_status["opportunities_count"] = len(opportunities)
            scanner_status["last_scan_success"] = True
            scanner_status["last_scan_valid"] = True
            scanner_status["last_scan_error"] = None
            scanner_status["last_scan_finished"] = utc_now().isoformat()
        if not save_scanner_status():
            logger.warning("Scan succeeded but scanner status could not be persisted.")
        logger.info("Scan completed successfully.")
        return opportunities


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    try:
        opportunities = run_scan()
        logger.info("Final opportunities: %s", len(opportunities))
    except KeyboardInterrupt:
        logger.warning("Scanner interrupted by user.")
    except Exception:
        logger.exception("Fatal scanner error.")
        raise
