# =============================================================================
# scanner.py – Pure Arbitrage Scanner (No Flask, No Database)
# For use in GitHub Actions or command-line cron jobs
# =============================================================================

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("arbitrage_scanner")

# =============================================================================
# Configuration
# =============================================================================

DEFAULT_STAKE = int(os.getenv("DEFAULT_STAKE", "100000"))
HISTORY_FILE = os.getenv("HISTORY_FILE", "arb_history.json")
OPPORTUNITIES_FILE = os.getenv("OPPORTUNITIES_FILE", "current_opportunities.json")
SCANNER_STATUS_FILE = os.getenv("SCANNER_STATUS_FILE", "scanner_status.json")
MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN = max(
    1, int(os.getenv("MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN", "2"))
)
ZERO_RECORDS_ARE_ERRORS = os.getenv("ZERO_RECORDS_ARE_ERRORS", "true").lower() == "true"

# =============================================================================
# Bookmaker API constants
# =============================================================================

SPORTYBET_API = (
    "https://betting-odds-scraper--hkltfsmjgkfde.replit.app/api/odds/simple"
)
CHAMPIONBET_API = (
    "https://www.championbet.ug/restapi/offer/en/top/mob"
    "?annex=13&offset=30&mobileVersion=2.47.4.3&locale=en"
)
CHAMPIONBET_MATCH_API = (
    "https://www.championbet.ug/restapi/offer/en/match/{match_id}"
    "?annex=13&mobileVersion=2.47.4.3&locale=en"
)
KBET_API_BASE = "https://kbet.ug/api/events"

CHAMPIONBET_TOP_API = (
    "https://www.championbet.ug/restapi/offer/en/top/mob"
    "?annex=13&mobileVersion=2.47.4.6&locale=en"
)

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
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/149.0.7827.159 Mobile Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        })

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.RequestException, ConnectionError, TimeoutError)
        ),
    )
    def get_response(self, url: str, headers: Optional[Dict[str, str]] = None,
                     params: Optional[Dict[str, Any]] = None) -> requests.Response:
        request_headers = self.session.headers.copy()
        if headers:
            request_headers.update(headers)
        response = self.session.get(url, headers=request_headers, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        return self.get_response(url, headers, params).json()

    def get_text(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        return self.get_response(url, headers).text

http = HTTPClient()

# =============================================================================
# General helpers
# =============================================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_timestamp() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")

# --- TEAM ALIASES ---
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
}

def normalize_team(name: str) -> str:
    if not name:
        return ""
    value = str(name).lower().strip()
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
    for pattern, repl in replacements.items():
        value = re.sub(pattern, repl, value)
    value = re.sub(r"[^a-z0-9 ]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value

def teams_match(name1: str, name2: str) -> bool:
    one = normalize_team(name1)
    two = normalize_team(name2)
    if not one or not two:
        return False
    if one == two:
        return True
    one_words = set(one.split())
    two_words = set(two.split())
    overlap = one_words & two_words
    if not overlap:
        return False
    min_words = min(len(one_words), len(two_words))
    if min_words <= 2:
        return len(overlap) >= min_words
    else:
        return len(overlap) >= 2

def market_key(home: str, away: str, market_type: str = "1x2", market_specifier: str = "") -> str:
    base = f"{normalize_team(home)} vs {normalize_team(away)}"
    return f"{base}|{(market_type or '1x2').strip()}|{(market_specifier or '').strip()}"

def match_key_similarity(key1: str, key2: str) -> bool:
    if key1 == key2:
        return True
    parts1 = key1.split("|")
    parts2 = key2.split("|")
    if len(parts1) != 3 or len(parts2) != 3:
        return False
    if parts1[1] != parts2[1]:
        return False
    if parts1[2] != parts2[2]:
        return False
    teams1 = parts1[0].split(" vs ", 1)
    teams2 = parts2[0].split(" vs ", 1)
    if len(teams1) != 2 or len(teams2) != 2:
        return False
    return teams_match(teams1[0], teams1[1]) and teams_match(teams2[0], teams2[1])

def clean_odd(value: Any, min_odd: float = 1.01, max_odd: float = 100.0) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.replace(",", ".").strip()
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

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def first_not_empty(data: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None

def atomic_json_write(filename: str, data: Any) -> None:
    directory = os.path.dirname(os.path.abspath(filename)) or "."
    temporary = os.path.join(directory, f".{os.path.basename(filename)}.tmp")
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, filename)

# =============================================================================
# Scanner status helpers
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

def update_bookmaker_status(bookmaker: str, *, success: bool, records: int,
                            error: Optional[str] = None, duration: Optional[float] = None) -> None:
    with status_lock:
        scanner_status["bookmakers"][bookmaker] = {
            "success": success,
            "records": records,
            "error": error,
            "duration_seconds": round(duration, 2) if duration is not None else None,
            "last_updated": utc_now().isoformat(),
        }

def save_scanner_status() -> None:
    with status_lock:
        data = dict(scanner_status)
    try:
        atomic_json_write(SCANNER_STATUS_FILE, data)
    except Exception:
        logger.exception("Unable to save scanner status")

def scraper_call(bookmaker: str, scraper) -> List[Dict[str, Any]]:
    started = time.monotonic()
    try:
        records = scraper()
        if records is None:
            records = []
        records = list(records)
        duration = time.monotonic() - started
        if ZERO_RECORDS_ARE_ERRORS and len(records) == 0:
            update_bookmaker_status(bookmaker, success=False, records=0,
                                    error="Bookmaker returned zero usable odds records.",
                                    duration=duration)
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
    home_team: str, away_team: str, bookmaker: str,
    home: Optional[float], draw: Optional[float], away: Optional[float],
    sport: str = "Football", competition: str = "",
    market_type: str = "1x2", market_specifier: str = "",
    event_id: Optional[str] = None,
) -> Dict[str, Any]:
    sport_lower = (sport or "Football").lower()
    sport_map = {
        "soccer": "football",
        "futbol": "football",
        "football": "football",
        "rugby": "rugby",
        "futsal": "futsal",
    }
    sport = sport_map.get(sport_lower, sport_lower).capitalize()

    return {
        "match": f"{home_team} vs {away_team}",
        "home_team": home_team,
        "away_team": away_team,
        "match_key": market_key(home_team, away_team, market_type, market_specifier),
        "bookmaker": bookmaker,
        "competition": competition or "",
        "home": home,
        "draw": draw,
        "away": away,
        "sport": sport,
        "market_type": market_type or "1x2",
        "market_specifier": market_specifier or "",
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
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to load history: %s", exc)
        return {}

def save_arbitrage_history(history: Dict[str, Any]) -> None:
    atomic_json_write(HISTORY_FILE, history)

def opportunity_key(opportunity: Dict[str, Any]) -> str:
    return "::".join([
        str(opportunity.get("sport", "Football")),
        str(opportunity.get("market_type", opportunity.get("type", "1x2"))),
        str(opportunity.get("market_specifier", "")),
        str(opportunity.get("match", "")),
    ])

def update_arbitrage_history(current: List[Dict[str, Any]], history: Dict[str, Any], timestamp: str) -> None:
    for entry in history.values():
        entry["updated_this_cycle"] = False

    for opp in current:
        key = opportunity_key(opp)
        if key not in history:
            history[key] = {
                "match": opp.get("match", ""),
                "sport": opp.get("sport", "Football"),
                "market_type": opp.get("market_type", opp.get("type", "1x2")),
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
        entry.setdefault("versions", []).append({
            "timestamp": timestamp,
            "profit_percent": opp.get("profit_percent", 0),
            "profit_ugx": opp.get("profit_ugx", 0),
            "arb_sum": opp.get("arb_sum", 0),
            "bets": opp.get("bets", []),
        })

    for entry in history.values():
        if not entry.get("updated_this_cycle"):
            entry["cycles_missed"] = entry.get("cycles_missed", 0) + 1
            if entry["cycles_missed"] >= 2:
                entry["valid"] = False
        entry.pop("updated_this_cycle", None)

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
        lines.append(f"▶ {bookmaker} ({outcome}) @ {odd} - Stake: UGX {stake:,}")
    message = "\n".join(lines)

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram alert sent for %s", match)
    except Exception as exc:
        logger.error("Telegram error: %s", exc)

# =============================================================================
# Generic HTML scraper helper
# =============================================================================

def scrape_generic_html(
    bookmaker_name: str,
    base_url: str,
    match_selector: str,
    home_selector: str,
    away_selector: str,
    odds_1_selector: str,
    odds_x_selector: Optional[str] = None,
    odds_2_selector: str = None,
    over_selector: Optional[str] = None,
    under_selector: Optional[str] = None,
    next_page_selector: Optional[str] = None,
    max_pages: int = 5,
    sport: str = "Football",
) -> List[Dict[str, Any]]:
    logger.info(f"Fetching {bookmaker_name} via HTML...")
    all_odds = []
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.7827.159 Mobile Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": base_url,
        "Connection": "keep-alive",
    })

    page = 1
    while page <= max_pages:
        # FIX: previously used base_url.replace("page=\\d+", ...) which is a
        # literal string replace, not a regex, so it could never match and
        # would silently fall through. Use re.sub against the actual base_url
        # so a "page=N" parameter already present is correctly replaced.
        if re.search(r"[?&]page=\d+", base_url):
            url = re.sub(r"page=\d+", f"page={page}", base_url)
        elif "?" in base_url:
            url = f"{base_url}&page={page}"
        else:
            url = f"{base_url}?page={page}"

        try:
            html = session.get(url, timeout=30).text
        except Exception as e:
            logger.error(f"{bookmaker_name} page {page} error: {e}")
            break

        soup = BeautifulSoup(html, "html.parser")
        matches = soup.select(match_selector)
        if not matches:
            # NOTE: table fallback intentionally left unimplemented — the
            # generic selectors below are guesses and haven't been verified
            # against each bookmaker's real markup. See summary notes.
            break

        for match in matches:
            home_el = match.select_one(home_selector)
            away_el = match.select_one(away_selector)
            if not home_el or not away_el:
                continue
            home_team = home_el.text.strip()
            away_team = away_el.text.strip()
            if not home_team or not away_team:
                continue

            odd_1 = None
            odd_x = None
            odd_2 = None
            if odds_1_selector:
                el = match.select_one(odds_1_selector)
                if el:
                    odd_1 = clean_odd(el.text.strip())
            if odds_x_selector:
                el = match.select_one(odds_x_selector)
                if el:
                    odd_x = clean_odd(el.text.strip())
            if odds_2_selector:
                el = match.select_one(odds_2_selector)
                if el:
                    odd_2 = clean_odd(el.text.strip())

            if odd_1 and odd_2:
                all_odds.append(build_match_record(
                    home_team, away_team, bookmaker_name,
                    odd_1, odd_x, odd_2,
                    sport=sport
                ))

            if over_selector and under_selector:
                over_el = match.select_one(over_selector)
                under_el = match.select_one(under_selector)
                over_odd = clean_odd(over_el.text.strip()) if over_el else None
                under_odd = clean_odd(under_el.text.strip()) if under_el else None
                if over_odd and under_odd:
                    all_odds.append(build_match_record(
                        home_team, away_team, bookmaker_name,
                        over_odd, under_odd, None,
                        sport=sport,
                        market_type="Over/Under 2.5",
                        market_specifier="2.5"
                    ))

        if next_page_selector:
            next_btn = soup.select_one(next_page_selector)
            if not next_btn or "disabled" in next_btn.get("class", []):
                break
        else:
            # FIX: `text=` is deprecated in newer BeautifulSoup/bs4 releases
            # in favor of `string=`. Kept behavior identical, just future-proofed.
            next_link = soup.find("a", string=re.compile(r"Next|»|→", re.I))
            if not next_link:
                break

        page += 1
        time.sleep(1)

    logger.info(f"{bookmaker_name}: {len(all_odds)} records")
    return all_odds

# =============================================================================
# Scrapers
# =============================================================================

# ---------- SportyBet ----------
def scrape_sportybet() -> List[Dict[str, Any]]:
    logger.info("Fetching SportyBet...")
    odds = []
    try:
        data = http.get_json(SPORTYBET_API)
        if not isinstance(data, list):
            return odds
        for event in data:
            home = first_not_empty(event, ["home_team", "homeTeam", "home"])
            away = first_not_empty(event, ["away_team", "awayTeam", "away"])
            if not home or not away:
                continue
            sport = event.get("sport", "Football") or "Football"
            home_odd = clean_odd(event.get("home"))
            draw_odd = clean_odd(event.get("draw"))
            away_odd = clean_odd(event.get("away"))
            if home_odd and away_odd:
                odds.append(build_match_record(home, away, "SportyBet", home_odd, draw_odd, away_odd, sport=sport))
            over_odd = clean_odd(event.get("over_odd"))
            under_odd = clean_odd(event.get("under_odd"))
            if over_odd and under_odd:
                odds.append(build_match_record(home, away, "SportyBet", over_odd, under_odd, None,
                                               sport=sport, market_type="Over/Under 2.5", market_specifier="2.5"))
        logger.info("SportyBet: %s records", len(odds))
    except Exception as exc:
        logger.error("SportyBet error: %s", exc)
    return odds

# ---------- SportyBet Official (experimental) ----------
def scrape_sportybet_official() -> List[Dict[str, Any]]:
    logger.info("Fetching SportyBet (official API)...")
    odds = []
    try:
        base_headers = {"Referer": "https://www.sportybet.com/ng/m/"}
        sport_list_url = "https://www.sportybet.com/factsCenter/wapPopularAndSportOption/v2"
        data = http.get_json(sport_list_url, headers=base_headers)
        if data.get("bizCode") != 10000:
            logger.warning("SportyBet official API error: %s", data.get("message"))
            return odds
        sport_data = data.get("data", {})
        sport_list = sport_data.get("sportList", [])
        football = next((s for s in sport_list if s.get("id") == "sr:sport:1"), None)
        if not football:
            logger.warning("Football not found in SportyBet sports list")
            return odds
        categories = football.get("categories", [])
        for category in categories:
            tournaments = category.get("tournaments", [])
            for tournament in tournaments:
                tournament_id = tournament.get("id")
                if not tournament_id:
                    continue
                event_list_url = f"https://www.sportybet.com/factsCenter/event/list?tournamentId={tournament_id}"
                try:
                    event_data = http.get_json(event_list_url, headers=base_headers)
                except Exception:
                    logger.warning("Failed to fetch events for tournament %s", tournament_id)
                    continue
                if event_data.get("bizCode") != 10000:
                    continue
                events = event_data.get("data", {}).get("events", [])
                for event in events:
                    event_id = event.get("id")
                    home = event.get("homeTeamName") or event.get("home")
                    away = event.get("awayTeamName") or event.get("away")
                    if not event_id or not home or not away:
                        continue
                    odds_url = f"https://www.sportybet.com/factsCenter/event/odds?eventId={event_id}"
                    try:
                        odds_data = http.get_json(odds_url, headers=base_headers)
                    except Exception:
                        logger.warning("Failed to fetch odds for event %s", event_id)
                        continue
                    if odds_data.get("bizCode") != 10000:
                        continue
                    ods = odds_data.get("data", {})
                    home_odd = clean_odd(ods.get("homeOdd") or ods.get("home") or ods.get("1"))
                    draw_odd = clean_odd(ods.get("drawOdd") or ods.get("draw") or ods.get("x"))
                    away_odd = clean_odd(ods.get("awayOdd") or ods.get("away") or ods.get("2"))
                    if home_odd and away_odd:
                        odds.append(build_match_record(home, away, "SportyBetOfficial", home_odd, draw_odd, away_odd, event_id=event_id))
                    over_odd = clean_odd(ods.get("overOdd") or ods.get("over") or ods.get("over_odd"))
                    under_odd = clean_odd(ods.get("underOdd") or ods.get("under") or ods.get("under_odd"))
                    if over_odd and under_odd:
                        odds.append(build_match_record(home, away, "SportyBetOfficial", over_odd, under_odd, None,
                                                       market_type="Over/Under 2.5", market_specifier="2.5", event_id=event_id))
                    time.sleep(0.05)
        logger.info("SportyBetOfficial: %s records", len(odds))
    except Exception as exc:
        logger.error("SportyBet official API error: %s", exc)
    return odds

# ---------- ChampionBet helpers ----------
def championbet_market_items(bet_map: Dict[str, Any], keys: List[Any]) -> List[Dict[str, Any]]:
    result = []
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
    for k in ("ov", "odds", "odd", "value", "C", "c"):
        if k in item:
            odd = clean_odd(item.get(k))
            if odd is not None:
                return odd
    return None

def normalize_label(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())

def item_label(item: Dict[str, Any]) -> str:
    for k in ("name", "label", "n", "desc", "description", "title", "outcome", "outcomeName", "caption"):
        value = item.get(k)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""

def extract_championbet_1x2(bet_map: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    home = draw = away = None
    items = championbet_market_items(bet_map, [1, 2, 3, 4, 5, 6, 7, 8, 9])
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = normalize_label(item_label(item))
        # FIX: this local variable previously shadowed the module-level
        # market_key(home, away, market_type, market_specifier) function.
        # Renamed to item_market_key to avoid any future accidental collision.
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

def extract_championbet_ou(bet_map: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    over = under = None
    items = championbet_market_items(bet_map, [21, 22, 51, 52])
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = normalize_label(item_label(item))
        if "over" in label and over is None:
            over = odd
        elif "under" in label and under is None:
            under = odd
    if over is None:
        for item in championbet_market_items(bet_map, [51, 21]):
            odd = extract_odd_from_item(item)
            if odd is not None:
                over = odd
                break
    if under is None:
        for item in championbet_market_items(bet_map, [52, 22]):
            odd = extract_odd_from_item(item)
            if odd is not None:
                under = odd
                break
    return over, under

def extract_championbet_extra(bet_map: Dict[str, Any]) -> Tuple[
    Dict[str, Tuple[Optional[float], Optional[float]]],
    Dict[str, Optional[float]],
    Dict[str, Optional[float]]
]:
    asian = {}
    double_chance = {"1X": None, "12": None, "X2": None}
    btts = {"yes": None, "no": None}

    items = championbet_market_items(bet_map, [5, 6, 7, 8])
    for item in items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = item_label(item)
        line = item.get("P") or item.get("p") or item.get("specifier") or item.get("line") or ""
        line_key = str(line)
        if line_key not in asian:
            asian[line_key] = [None, None]
        normalized = normalize_label(label)
        if normalized in {"home", "1", "1handicap", "homehandicap"} or item.get("_market_key") in {"5", "7"}:
            asian[line_key][0] = odd
        elif normalized in {"away", "2", "2handicap", "awayhandicap"} or item.get("_market_key") in {"6", "8"}:
            asian[line_key][1] = odd

    # FIX: previously any handicap line whose actual line value couldn't be
    # read (line_key == "") was still included, and would then get matched
    # against a *different* bookmaker's real-lined handicap in
    # match_key_similarity (since market_specifier would be "" == "").
    # That risks flagging a false arbitrage between two different handicap
    # lines. Now we only keep entries where we actually know the line.
    asian_result = {}
    for line, values in asian.items():
        if not line:
            continue
        if values[0] is not None and values[1] is not None:
            asian_result[line] = (values[0], values[1])

    dc_items = championbet_market_items(bet_map, [20, 21, 22])
    for item in dc_items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = normalize_label(item_label(item))
        if label in {"1x", "homedraw", "homeordraw"}:
            double_chance["1X"] = odd
        elif label in {"12", "homeaway"}:
            double_chance["12"] = odd
        elif label in {"x2", "drawaway", "draworaway"}:
            double_chance["X2"] = odd

    btts_items = championbet_market_items(bet_map, [19, 20])
    for item in btts_items:
        odd = extract_odd_from_item(item)
        if odd is None:
            continue
        label = normalize_label(item_label(item))
        if label in {"yes", "y", "bothteamstoscoreyes"}:
            btts["yes"] = odd
        elif label in {"no", "n", "bothteamstoscoreno"}:
            btts["no"] = odd

    return asian_result, double_chance, btts

# ---------- ChampionBet (old) ----------
def scrape_championbet() -> List[Dict[str, Any]]:
    logger.info("Fetching ChampionBet...")
    odds = []
    try:
        data = http.get_json(CHAMPIONBET_API, headers={"Referer": "https://www.championbet.ug/"})
        matches = data.get("esMatches", []) if isinstance(data, dict) else []
        for match in matches:
            try:
                match_id = match.get("id") or match.get("matchId")
                if not match_id:
                    continue
                home = match.get("home") or match.get("homeName") or match.get("homeTeam")
                away = match.get("away") or match.get("awayName") or match.get("awayTeam")
                if not home or not away:
                    continue
                match_data = http.get_json(CHAMPIONBET_MATCH_API.format(match_id=match_id),
                                           headers={"Referer": "https://www.championbet.ug/"})
                bet_map = match_data.get("betMap", {}) if isinstance(match_data, dict) else {}
                home_odd, draw_odd, away_odd = extract_championbet_1x2(bet_map)
                competition = match.get("leagueName", "") or match.get("competitionName", "")
                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "ChampionBet", home_odd, draw_odd, away_odd,
                                                   competition=competition, event_id=match_id))
                over_odd, under_odd = extract_championbet_ou(bet_map)
                if over_odd and under_odd:
                    odds.append(build_match_record(home, away, "ChampionBet", over_odd, under_odd, None,
                                                   competition=competition, market_type="Over/Under 2.5",
                                                   market_specifier="2.5", event_id=match_id))
                ah_odds, dc_odds, btts_odds = extract_championbet_extra(bet_map)
                for line, values in ah_odds.items():
                    ah_home, ah_away = values
                    if ah_home and ah_away:
                        odds.append(build_match_record(home, away, "ChampionBet", ah_home, None, ah_away,
                                                       competition=competition, market_type="Asian Handicap",
                                                       market_specifier=str(line), event_id=match_id))
                if dc_odds.get("1X") and dc_odds.get("X2"):
                    odds.append(build_match_record(home, away, "ChampionBet", dc_odds["1X"], None, dc_odds["X2"],
                                                   competition=competition, market_type="Double Chance",
                                                   market_specifier="1X_X2", event_id=match_id))
                if btts_odds.get("yes") and btts_odds.get("no"):
                    odds.append(build_match_record(home, away, "ChampionBet", btts_odds["yes"], None, btts_odds["no"],
                                                   competition=competition, market_type="BTTS", event_id=match_id))
                time.sleep(0.1)
            except Exception:
                logger.exception("ChampionBet match failed")
                continue
        logger.info("ChampionBet: %s records", len(odds))
    except Exception as exc:
        logger.error("ChampionBet error: %s", exc)
    return odds

# ---------- ChampionBet Official ----------
def scrape_championbet_official() -> List[Dict[str, Any]]:
    # FIX: this previously called a separate "sports list" endpoint and
    # searched for a sport token by checking `token == "S"` against entries
    # of that list. That can never match real API data (the list holds
    # dicts/ids, not the literal string "S"), so this scraper was silently
    # returning zero records every single scan. The known-working top/mob
    # endpoint (same one scrape_championbet() already uses successfully)
    # doesn't require a sport token, so we paginate that directly instead.
    logger.info("Fetching ChampionBet (official API)...")
    odds = []
    try:
        offset = 0
        limit = 100
        while True:
            url = f"{CHAMPIONBET_TOP_API}&offset={offset}"
            data = http.get_json(url, headers={"Referer": "https://www.championbet.ug/"})
            matches = data.get("esMatches", []) if isinstance(data, dict) else []
            if not matches:
                break
            for match in matches:
                try:
                    match_id = match.get("id")
                    if not match_id:
                        continue
                    home = match.get("home")
                    away = match.get("away")
                    if not home or not away:
                        continue
                    bet_map = match.get("betMap", {})
                    home_odd, draw_odd, away_odd = extract_championbet_1x2(bet_map)
                    competition = match.get("leagueName", "")
                    if home_odd and away_odd:
                        odds.append(build_match_record(home, away, "ChampionBetOfficial", home_odd, draw_odd, away_odd,
                                                       competition=competition, event_id=match_id))
                    over_odd, under_odd = extract_championbet_ou(bet_map)
                    if over_odd and under_odd:
                        odds.append(build_match_record(home, away, "ChampionBetOfficial", over_odd, under_odd, None,
                                                       competition=competition, market_type="Over/Under 2.5",
                                                       market_specifier="2.5", event_id=match_id))
                    ah_odds, dc_odds, btts_odds = extract_championbet_extra(bet_map)
                    for line, values in ah_odds.items():
                        ah_home, ah_away = values
                        if ah_home and ah_away:
                            odds.append(build_match_record(home, away, "ChampionBetOfficial", ah_home, None, ah_away,
                                                           competition=competition, market_type="Asian Handicap",
                                                           market_specifier=str(line), event_id=match_id))
                    if dc_odds.get("1X") and dc_odds.get("X2"):
                        odds.append(build_match_record(home, away, "ChampionBetOfficial", dc_odds["1X"], None, dc_odds["X2"],
                                                       competition=competition, market_type="Double Chance",
                                                       market_specifier="1X_X2", event_id=match_id))
                    if btts_odds.get("yes") and btts_odds.get("no"):
                        odds.append(build_match_record(home, away, "ChampionBetOfficial", btts_odds["yes"], None, btts_odds["no"],
                                                       competition=competition, market_type="BTTS", event_id=match_id))
                except Exception:
                    logger.exception("ChampionBet official match processing error")
                    continue
            total = data.get("totalMatchCount")
            if total is not None and offset + limit >= total:
                break
            if len(matches) < limit:
                break
            offset += limit
            time.sleep(0.5)
        logger.info("ChampionBetOfficial: %s records", len(odds))
    except Exception as exc:
        logger.error("ChampionBet official API error: %s", exc)
    return odds

# ---------- AbaBet ----------
def scrape_ababet() -> List[Dict[str, Any]]:
    logger.info("Fetching AbaBet...")
    odds = []
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.7827.159 Mobile Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.ababet.ug/",
        "Connection": "keep-alive",
    })

    base_url = "https://www.ababet.ug/soccer/match_result?mobile=1"
    page = 1
    max_pages = 5

    while page <= max_pages:
        url = f"{base_url}&page={page}" if "?" in base_url else f"{base_url}?page={page}"
        try:
            html = session.get(url, timeout=30).text
        except Exception as e:
            logger.error("AbaBet page %s fetch error: %s", page, e)
            break

        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        found = False
        for table in tables:
            first_row = table.find("tr")
            if not first_row:
                continue
            headers = [cell.get_text(" ", strip=True) for cell in first_row.find_all(["th", "td"])]
            if "Home" not in headers or "Away" not in headers:
                continue
            found = True
            for row in table.find_all("tr")[1:]:
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
                if len(cells) < 5:
                    continue
                row_data = dict(zip(headers, cells[:len(headers)]))
                home = row_data.get("Home")
                away = row_data.get("Away")
                if not home or not away or home == "-" or away == "-":
                    continue
                home_odd = clean_odd(row_data.get("1"))
                draw_odd = clean_odd(row_data.get("X"))
                away_odd = clean_odd(row_data.get("2"))
                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "AbaBet", home_odd, draw_odd, away_odd,
                                                   competition=row_data.get("League", "")))
                over_odd = clean_odd(row_data.get("Over"))
                under_odd = clean_odd(row_data.get("Under"))
                if over_odd and under_odd:
                    odds.append(build_match_record(home, away, "AbaBet", over_odd, under_odd, None,
                                                   market_type="Over/Under 2.5", market_specifier="2.5"))
        if not found:
            break
        # FIX: `text=` deprecated in newer bs4 in favor of `string=`.
        next_link = soup.find("a", string=re.compile(r"Next|»|→", re.I))
        if not next_link:
            break
        page += 1
        time.sleep(1)

    logger.info("AbaBet: %s records", len(odds))
    return odds

# ---------- Fortebet ----------
def scrape_fortebet() -> List[Dict[str, Any]]:
    logger.info("Fetching Fortebet...")
    odds = []
    try:
        url = "https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en"
        data = http.get_json(url, headers={"Referer": "https://desktop.fortebet.ug/prematch/landing"})
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
                comp_ids = event.get("competitors", [])
                if len(comp_ids) < 2:
                    continue
                home = competitors.get(str(comp_ids[0]), {}).get("name", "")
                away = competitors.get(str(comp_ids[1]), {}).get("name", "")
                if not home or not away:
                    continue

                home_odd = draw_odd = away_odd = None
                over_odd = under_odd = None
                ah_home = ah_away = None
                ah_specifier = None
                dc_home = dc_away = None
                btts_yes = btts_no = None

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

                        if market_id == 1:
                            if outcome_id == 1:
                                home_odd = max(home_odd or 0, odd)
                            elif outcome_id == 2:
                                draw_odd = max(draw_odd or 0, odd)
                            elif outcome_id == 3:
                                away_odd = max(away_odd or 0, odd)
                        elif market_id == 5:
                            if outcome_id == 1:
                                over_odd = odd
                            elif outcome_id == 2:
                                under_odd = odd
                        elif market_id == 2:
                            # FIX: previously the actual handicap line was
                            # never read here, and the record below hardcoded
                            # market_specifier="-0.5" regardless of the real
                            # line. That would silently mislabel every AH
                            # line as "-0.5" and could match it against a
                            # genuinely different line from another
                            # bookmaker as if it were the same market --
                            # producing a false arbitrage opportunity with
                            # real money at stake. Now we try to read the
                            # actual line, and skip the record if we can't
                            # determine it rather than guessing.
                            if outcome_id == 1:
                                ah_home = odd
                            elif outcome_id == 2:
                                ah_away = odd
                            candidate_specifier = (
                                value.get("specifier")
                                or value.get("hcp")
                                or value.get("line")
                                or value.get("handicap")
                                or market.get("specifier")
                            )
                            if candidate_specifier is not None:
                                ah_specifier = candidate_specifier
                        elif market_id == 8:
                            if outcome_id == 1:
                                dc_home = odd
                            elif outcome_id == 3:
                                dc_away = odd
                        elif market_id == 12:
                            if outcome_id == 1:
                                btts_yes = odd
                            elif outcome_id == 2:
                                btts_no = odd

                sport = str(event.get("sportName", event.get("sport", ""))).lower()
                if "basketball" in sport:
                    sport_name = "Basketball"
                elif "tennis" in sport:
                    sport_name = "Tennis"
                elif "rugby" in sport:
                    sport_name = "Rugby"
                else:
                    sport_name = "Football"

                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "Fortebet", home_odd, draw_odd, away_odd,
                                                   sport=sport_name, event_id=event_id))
                if over_odd and under_odd:
                    odds.append(build_match_record(home, away, "Fortebet", over_odd, under_odd, None,
                                                   sport=sport_name, market_type="Over/Under 2.5",
                                                   market_specifier="2.5", event_id=event_id))
                if ah_home and ah_away:
                    if ah_specifier is not None:
                        odds.append(build_match_record(home, away, "Fortebet", ah_home, None, ah_away,
                                                       sport=sport_name, market_type="Asian Handicap",
                                                       market_specifier=str(ah_specifier), event_id=event_id))
                    else:
                        logger.debug(
                            "Fortebet: skipping Asian Handicap for %s vs %s — "
                            "actual handicap line could not be determined",
                            home, away,
                        )
                if dc_home and dc_away:
                    odds.append(build_match_record(home, away, "Fortebet", dc_home, None, dc_away,
                                                   sport=sport_name, market_type="Double Chance",
                                                   market_specifier="1X_X2", event_id=event_id))
                if btts_yes and btts_no:
                    odds.append(build_match_record(home, away, "Fortebet", btts_yes, None, btts_no,
                                                   sport=sport_name, market_type="BTTS", event_id=event_id))
            except Exception:
                logger.exception("Fortebet event failed")
                continue
        logger.info("Fortebet: %s records", len(odds))
    except Exception as exc:
        logger.error("Fortebet error: %s", exc)
    return odds

# ---------- Shared bookmakers (1xBet, 22Bet) ----------
def shared_feed_url(config: Dict[str, Any]) -> str:
    return (f"{config['base_url']}/service-api/LineFeed/Get1x2_VZip"
            f"?count=1000&lng={config.get('lng', 'en')}&tz={config.get('tz', 3)}"
            f"&mode=4&country=191&partner={config['partner']}&getEmpty=true&gr={config.get('gr', 525)}")

def shared_headers(config: Dict[str, Any]) -> Dict[str, str]:
    return {"Referer": config.get("referer", config["base_url"]),
            "Origin": config["base_url"],
            "X-Requested-With": "XMLHttpRequest"}

def extract_shared_outcomes(match: Dict[str, Any]) -> Dict[str, Any]:
    result = {"home": None, "draw": None, "away": None, "over": None, "under": None,
              "ah_home": None, "ah_away": None, "ah_line": None,
              "dc_home": None, "dc_away": None,
              "btts_yes": None, "btts_no": None}
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
        elif outcome_type == "7":
            result["ah_home"] = max(result["ah_home"] or 0, odd)
            if p is not None:
                result["ah_line"] = p
        elif outcome_type == "8":
            result["ah_away"] = max(result["ah_away"] or 0, odd)
            if p is not None:
                result["ah_line"] = p
        elif outcome_type == "9":
            if p is None or safe_float(p, -999) == 2.5:
                result["over"] = max(result["over"] or 0, odd)
        elif outcome_type == "10":
            if p is None or safe_float(p, -999) == 2.5:
                result["under"] = max(result["under"] or 0, odd)
        elif outcome_type == "4":
            result["dc_home"] = max(result["dc_home"] or 0, odd)
        elif outcome_type == "6":
            result["dc_away"] = max(result["dc_away"] or 0, odd)
        elif outcome_type == "19":
            result["btts_yes"] = max(result["btts_yes"] or 0, odd)
        elif outcome_type == "20":
            result["btts_no"] = max(result["btts_no"] or 0, odd)
    return result

def scrape_shared_1x_like(bookmaker: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    logger.info("Fetching %s...", bookmaker)
    odds = []
    try:
        data = http.get_json(shared_feed_url(config), headers=shared_headers(config))
        values = data.get("Value", []) if isinstance(data, dict) else []
        for match in values:
            home = match.get("O1") or match.get("O1Name")
            away = match.get("O2") or match.get("O2Name")
            if not home or not away:
                continue
            if str(home).strip().lower() == "home" and str(away).strip().lower() == "away":
                continue
            extracted = extract_shared_outcomes(match)
            if extracted["home"] and extracted["away"]:
                odds.append(build_match_record(home, away, bookmaker,
                                               extracted["home"], extracted["draw"], extracted["away"],
                                               event_id=match.get("I") or match.get("Id")))
        logger.info("%s: %s records", bookmaker, len(odds))
    except Exception as exc:
        logger.error("%s error: %s", bookmaker, exc)
    return odds

# ---------- Melbet (HTML) ----------
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
        max_pages=5,
    )

# ---------- Shared extra markets ----------
def scrape_shared_extra_markets() -> List[Dict[str, Any]]:
    all_odds = []
    for bookmaker in ["1xBet", "22Bet"]:
        config = SHARED_BOOKMAKERS[bookmaker]
        try:
            data = http.get_json(shared_feed_url(config), headers=shared_headers(config))
            values = data.get("Value", []) if isinstance(data, dict) else []
            for match in values:
                home = match.get("O1", "")
                away = match.get("O2", "")
                if not home or not away:
                    continue
                extracted = extract_shared_outcomes(match)
                event_id = match.get("I") or match.get("Id")
                if extracted["ah_home"] and extracted["ah_away"]:
                    all_odds.append(build_match_record(home, away, bookmaker,
                                                       extracted["ah_home"], None, extracted["ah_away"],
                                                       market_type="Asian Handicap",
                                                       market_specifier=str(extracted["ah_line"] or ""),
                                                       event_id=event_id))
                if extracted["over"] and extracted["under"]:
                    all_odds.append(build_match_record(home, away, bookmaker,
                                                       extracted["over"], extracted["under"], None,
                                                       market_type="Over/Under 2.5", market_specifier="2.5",
                                                       event_id=event_id))
                if extracted["dc_home"] and extracted["dc_away"]:
                    all_odds.append(build_match_record(home, away, bookmaker,
                                                       extracted["dc_home"], None, extracted["dc_away"],
                                                       market_type="Double Chance", market_specifier="1X_X2",
                                                       event_id=event_id))
                if extracted["btts_yes"] and extracted["btts_no"]:
                    all_odds.append(build_match_record(home, away, bookmaker,
                                                       extracted["btts_yes"], None, extracted["btts_no"],
                                                       market_type="BTTS", event_id=event_id))
        except Exception:
            logger.exception("%s extra markets failed", bookmaker)
    return all_odds

# ---------- kbet ----------
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
        for event in events:
            home = event.get("home_team") or event.get("home") or ""
            away = event.get("away_team") or event.get("away") or ""
            if not home or not away:
                continue
            home_odd = clean_odd(event.get("home_odd") or event.get("odds_1"))
            draw_odd = clean_odd(event.get("draw_odd") or event.get("odds_x"))
            away_odd = clean_odd(event.get("away_odd") or event.get("odds_2"))
            if not home_odd and isinstance(event.get("odds"), dict):
                od = event["odds"]
                home_odd = clean_odd(od.get("1") or od.get("home"))
                draw_odd = clean_odd(od.get("x") or od.get("draw"))
                away_odd = clean_odd(od.get("2") or od.get("away"))
            if home_odd and away_odd:
                odds.append(build_match_record(home, away, "kbet", home_odd, draw_odd, away_odd,
                                               sport="Football", event_id=event.get("id")))
        logger.info("kbet: %s records", len(odds))
    except Exception as exc:
        logger.error("kbet error: %s", exc)
    return odds

# ---------- Betway, BetPawa, PremierBet ----------
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
        max_pages=5,
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
        max_pages=5,
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
        max_pages=5,
    )

# =============================================================================
# Arbitrage calculations
# =============================================================================

def calculate_stakes(odds: List[float], total_stake: int) -> List[int]:
    inverse_sum = sum(1 / o for o in odds)
    if inverse_sum <= 0:
        return []
    raw = [total_stake * (1 / o) / inverse_sum for o in odds]
    stakes = [round(v) for v in raw]
    diff = total_stake - sum(stakes)
    if stakes:
        stakes[-1] += diff
    return stakes

def create_two_outcome_opportunity(
    match: str, sport: str, market_type: str, market_specifier: str,
    first_bm: str, first_outcome: str, first_odd: float,
    second_bm: str, second_outcome: str, second_odd: float,
    stake: int = DEFAULT_STAKE,
) -> Optional[Dict[str, Any]]:
    if not first_odd or not second_odd or first_odd <= 1 or second_odd <= 1:
        return None
    arb_sum = 1 / first_odd + 1 / second_odd
    if arb_sum >= 1:
        return None
    profit_percent = round((1 - arb_sum) * 100, 2)
    if not 0.5 <= profit_percent <= 50:
        return None
    stakes = calculate_stakes([first_odd, second_odd], stake)
    if len(stakes) != 2:
        return None
    return {
        "match": match,
        "sport": sport,
        "type": market_type,
        "market_type": market_type,
        "market_specifier": market_specifier,
        "profit_percent": profit_percent,
        "profit_ugx": round(stake * (1 - arb_sum)),
        "total_stake": stake,
        "arb_sum": round(arb_sum, 6),
        "bets": [
            {"bookmaker": first_bm, "outcome": first_outcome, "odd": first_odd,
             "stake": stakes[0], "win": round(stakes[0] * first_odd)},
            {"bookmaker": second_bm, "outcome": second_outcome, "odd": second_odd,
             "stake": stakes[1], "win": round(stakes[1] * second_odd)},
        ],
    }

def create_three_outcome_opportunity(
    match: str, sport: str,
    home_bm: str, home_odd: float,
    draw_bm: str, draw_odd: float,
    away_bm: str, away_odd: float,
    stake: int = DEFAULT_STAKE,
) -> Optional[Dict[str, Any]]:
    if not home_odd or not draw_odd or not away_odd or home_odd <= 1 or draw_odd <= 1 or away_odd <= 1:
        return None
    arb_sum = 1 / home_odd + 1 / draw_odd + 1 / away_odd
    if arb_sum >= 1:
        return None
    profit_percent = round((1 - arb_sum) * 100, 2)
    if not 0.5 <= profit_percent <= 50:
        return None
    stakes = calculate_stakes([home_odd, draw_odd, away_odd], stake)
    if len(stakes) != 3:
        return None
    return {
        "match": match,
        "sport": sport,
        "type": "3-way",
        "market_type": "1x2",
        "market_specifier": "",
        "profit_percent": profit_percent,
        "profit_ugx": round(stake * (1 - arb_sum)),
        "total_stake": stake,
        "arb_sum": round(arb_sum, 6),
        "bets": [
            {"bookmaker": home_bm, "outcome": "Home", "odd": home_odd,
             "stake": stakes[0], "win": round(stakes[0] * home_odd)},
            {"bookmaker": draw_bm, "outcome": "Draw", "odd": draw_odd,
             "stake": stakes[1], "win": round(stakes[1] * draw_odd)},
            {"bookmaker": away_bm, "outcome": "Away", "odd": away_odd,
             "stake": stakes[2], "win": round(stakes[2] * away_odd)},
        ],
    }

# =============================================================================
# Arbitrage finder
# =============================================================================

def merge_matching_groups(groups: Dict[str, List[Dict[str, Any]]]) -> List[List[Dict[str, Any]]]:
    keys = list(groups.keys())
    merged = []
    visited = set()
    for i, first_key in enumerate(keys):
        if first_key in visited:
            continue
        group = list(groups[first_key])
        visited.add(first_key)
        for second_key in keys[i+1:]:
            if second_key in visited:
                continue
            if match_key_similarity(first_key, second_key):
                group.extend(groups[second_key])
                visited.add(second_key)
        merged.append(group)
    return merged

def find_arbitrage(all_odds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    opportunities = []
    sports = {}
    for odd in all_odds:
        sport = odd.get("sport", "Football") or "Football"
        sports.setdefault(sport, []).append(odd)

    for sport, sport_odds in sports.items():
        groups = {}
        for odd in sport_odds:
            key = odd.get("match_key", "")
            if not key:
                continue
            groups.setdefault(key, []).append(odd)

        merged_groups = merge_matching_groups(groups)

        for records in merged_groups:
            if len(records) < 2:
                continue
            first_record = records[0]
            market_type = first_record.get("market_type", "1x2")
            specifier = first_record.get("market_specifier", "")
            match_name = first_record.get("match", "")

            has_draw = any(clean_odd(rec.get("draw")) is not None for rec in records)

            # 3-way markets
            if market_type == "1x2" and has_draw:
                best_home, best_draw, best_away = {}, {}, {}
                for rec in records:
                    bm = rec.get("bookmaker")
                    if not bm:
                        continue
                    home = clean_odd(rec.get("home"))
                    draw = clean_odd(rec.get("draw"))
                    away = clean_odd(rec.get("away"))
                    if home and (bm not in best_home or home > best_home[bm]):
                        best_home[bm] = home
                    if draw and (bm not in best_draw or draw > best_draw[bm]):
                        best_draw[bm] = draw
                    if away and (bm not in best_away or away > best_away[bm]):
                        best_away[bm] = away

                for home_bm, home_odd in best_home.items():
                    for draw_bm, draw_odd in best_draw.items():
                        for away_bm, away_odd in best_away.items():
                            if len({home_bm, draw_bm, away_bm}) < 2:
                                continue
                            opp = create_three_outcome_opportunity(
                                match_name, sport, home_bm, home_odd,
                                draw_bm, draw_odd, away_bm, away_odd
                            )
                            if opp:
                                opportunities.append(opp)

            # 2-way markets
            else:
                bookmakers = {}
                for rec in records:
                    bm = rec.get("bookmaker")
                    if not bm:
                        continue
                    bookmakers.setdefault(bm, {"home": 0, "away": 0})
                    home = clean_odd(rec.get("home"))
                    away = clean_odd(rec.get("away"))
                    if home:
                        bookmakers[bm]["home"] = max(bookmakers[bm]["home"], home)
                    if away:
                        bookmakers[bm]["away"] = max(bookmakers[bm]["away"], away)

                if market_type == "Over/Under 2.5":
                    outcome1, outcome2 = "Over", "Under"
                elif market_type == "Asian Handicap":
                    outcome1, outcome2 = "Home", "Away"
                elif market_type == "BTTS":
                    outcome1, outcome2 = "Yes", "No"
                elif market_type == "Double Chance":
                    outcome1, outcome2 = "1X", "X2"
                else:
                    outcome1, outcome2 = "Home", "Away"

                # FIX: previously only checked (bm1's "home"/outcome1 side)
                # against (bm2's "away"/outcome2 side) for i < j, i.e. only
                # one direction of every bookmaker pair. That silently
                # discarded roughly half of all real two-way arbitrage
                # opportunities — e.g. cases where the *second* bookmaker in
                # the pair actually had the better outcome1 price and the
                # *first* had the better outcome2 price. Now both directions
                # are checked for every pair.
                bms = list(bookmakers.keys())
                for i, bm1 in enumerate(bms):
                    for bm2 in bms[i+1:]:
                        combos = [
                            (bm1, bookmakers[bm1]["home"], bm2, bookmakers[bm2]["away"]),
                            (bm2, bookmakers[bm2]["home"], bm1, bookmakers[bm1]["away"]),
                        ]
                        for first_bm, first_odd, second_bm, second_odd in combos:
                            if first_odd and second_odd:
                                opp = create_two_outcome_opportunity(
                                    match_name, sport, market_type, specifier,
                                    first_bm, outcome1, first_odd,
                                    second_bm, outcome2, second_odd
                                )
                                if opp:
                                    opportunities.append(opp)

    best = {}
    for opp in opportunities:
        ident = f"{opp['match']}::{opp['sport']}::{opp['market_type']}::{opp['market_specifier']}"
        if ident not in best or opp["profit_percent"] > best[ident]["profit_percent"]:
            best[ident] = opp

    result = list(best.values())
    result.sort(key=lambda x: x.get("profit_percent", 0), reverse=True)
    return result

# =============================================================================
# Scanner core
# =============================================================================

def load_current_opportunities() -> List[Dict[str, Any]]:
    if not os.path.exists(OPPORTUNITIES_FILE):
        return []
    try:
        with open(OPPORTUNITIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        logger.exception("Unable to read current opportunities")
        return []

def run_scan() -> List[Dict[str, Any]]:
    # FIX: scan_lock was declared at module level but never actually used
    # anywhere, so nothing prevented two overlapping run_scan() calls (e.g.
    # an overrunning cron job plus a manually triggered run) from writing to
    # the same history/opportunities files concurrently. Now the whole scan
    # body is guarded by it. If a scan is already running, a second call
    # will simply wait for it to finish rather than racing it.
    with scan_lock:
        reset_scanner_status()
        logger.info("=" * 48)
        logger.info("STARTING ARBITRAGE SCAN")
        logger.info("=" * 48)

        all_odds = []
        scrapers = [
            ("SportyBet", scrape_sportybet),
            ("SportyBetOfficial", scrape_sportybet_official),
            ("ChampionBet", scrape_championbet),
            ("ChampionBetOfficial", scrape_championbet_official),
            ("AbaBet", scrape_ababet),
            ("Fortebet", scrape_fortebet),
            ("1xBet", lambda: scrape_shared_1x_like("1xBet", SHARED_BOOKMAKERS["1xBet"])),
            ("22Bet", lambda: scrape_shared_1x_like("22Bet", SHARED_BOOKMAKERS["22Bet"])),
            ("Melbet", scrape_melbet_html),
            ("SharedExtraMarkets", scrape_shared_extra_markets),
            ("kbet", scrape_kbet),
            ("Betway", scrape_betway),
            ("BetPawa", scrape_betpawa),
            ("PremierBet", scrape_premierbet),
        ]

        for bookmaker, scraper in scrapers:
            records = scraper_call(bookmaker, scraper)
            all_odds.extend(records)

        with status_lock:
            bookmaker_status = scanner_status["bookmakers"]
            real_bookmakers = [name for name in bookmaker_status if name != "SharedExtraMarkets"]
            healthy_bookmakers = sum(
                1 for name in real_bookmakers
                if bookmaker_status[name].get("success", False) and bookmaker_status[name].get("records", 0) > 0
            )
            scanner_status["healthy_bookmakers"] = healthy_bookmakers
            scanner_status["total_odds"] = len(all_odds)

        logger.info("Total usable odds records: %s", len(all_odds))
        logger.info("Healthy bookmakers: %s", healthy_bookmakers)

        if healthy_bookmakers < MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN:
            error = ("Scanner returned too few healthy bookmakers. "
                     "Current opportunities were NOT overwritten.")
            logger.error("%s Healthy=%s Required=%s", error, healthy_bookmakers,
                         MIN_HEALTHY_BOOKMAKERS_FOR_VALID_SCAN)
            with status_lock:
                scanner_status["last_scan_success"] = False
                scanner_status["last_scan_valid"] = False
                scanner_status["last_scan_error"] = error
                scanner_status["last_scan_finished"] = utc_now().isoformat()
            save_scanner_status()
            return load_current_opportunities()

        opportunities = find_arbitrage(all_odds)
        logger.info("Found %s arbitrage opportunities", len(opportunities))

        # Write opportunities to JSON
        try:
            atomic_json_write(OPPORTUNITIES_FILE, opportunities)
            logger.info("Opportunities written to %s", OPPORTUNITIES_FILE)
        except Exception as e:
            logger.exception("Failed to write opportunities to JSON: %s", e)

        # Update history
        with history_lock:
            history = load_arbitrage_history()
            timestamp = utc_timestamp()
            update_arbitrage_history(opportunities, history, timestamp)
            save_arbitrage_history(history)

        # Send Telegram alerts for new opportunities (profit >= 5%)
        for opp in opportunities:
            key = opportunity_key(opp)
            if key not in history:
                if opp.get("profit_percent", 0) >= 5.0:
                    send_telegram_alert(opp)

        with status_lock:
            scanner_status["opportunities_count"] = len(opportunities)
            scanner_status["last_scan_success"] = True
            scanner_status["last_scan_valid"] = True
            scanner_status["last_scan_error"] = None
            scanner_status["last_scan_finished"] = utc_now().isoformat()

        save_scanner_status()
        logger.info("Scan completed successfully.")
        return opportunities

# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    run_scan()
