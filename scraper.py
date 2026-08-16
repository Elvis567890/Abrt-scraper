import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from html import escape
from typing import Any, Dict, List, Optional, Tuple

import bcrypt
import jwt
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, request, send_file
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ============================================================
# Environment
# ============================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("arbitrage_scanner")

# ============================================================
# Configuration
# ============================================================

DEFAULT_STAKE = int(os.getenv("DEFAULT_STAKE", "100000"))

HISTORY_FILE = os.getenv(
    "HISTORY_FILE",
    "arb_history.json",
)

OPPORTUNITIES_FILE = os.getenv(
    "OPPORTUNITIES_FILE",
    "current_opportunities.json",
)

SECRET_KEY = os.getenv("SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY environment variable is required."
    )

FRONTEND_ORIGIN = os.getenv(
    "FRONTEND_ORIGIN",
    "*",
)

ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.getenv("ADMIN_EMAILS", "").split(",")
    if email.strip()
}

# Scheduler is disabled by default.
# In production, run the scanner as a separate worker/process.
ENABLE_SCHEDULER = (
    os.getenv("ENABLE_SCHEDULER", "false").lower()
    == "true"
)

SCAN_INTERVAL_MINUTES = int(
    os.getenv("SCAN_INTERVAL_MINUTES", "2")
)

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
        "partner": "8",
        "lng": "en",
        "tz": 3,
        "gr": 525,
        "referer": "https://melbet.ug/line",
    },
}

# ============================================================
# HTTP client
# ============================================================

class HTTPClient:
    def __init__(
        self,
        timeout: int = 30,
        retries: int = 3,
    ):
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
                "Accept": (
                    "application/json, text/plain, */*"
                ),
                "Accept-Language": (
                    "en-US,en;q=0.9"
                ),
                "Connection": "keep-alive",
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
        headers: Optional[
            Dict[str, str]
        ] = None,
        params: Optional[
            Dict[str, Any]
        ] = None,
    ) -> Any:
        request_headers = (
            self.session.headers.copy()
        )
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
                TimeoutError,
            )
        ),
    )
    def get_text(
        self,
        url: str,
        headers: Optional[
            Dict[str, str]
        ] = None,
    ) -> str:
        request_headers = (
            self.session.headers.copy()
        )
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

# ============================================================
# General helpers
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_timestamp() -> str:
    return utc_now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def normalize_team(name: str) -> str:
    if not name:
        return ""
    value = str(name).lower().strip()
    for pattern, replacement in {
        r"\brovers\b": "rvs",
        r"\brvs\b": "rvs",
        r"\bunited\b": "utd",
        r"\butd\b": "utd",
    }.items():
        value = re.sub(pattern, replacement, value)
    value = re.sub(
        r"\b(fc|sc|cf|ac|city|sports|club|"
        r"football|soccer|women|men|u21|u23)\b",
        "",
        value,
    )
    value = re.sub(r"[^a-z0-9 ]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def teams_match(name1: str, name2: str) -> bool:
    one = normalize_team(name1)
    two = normalize_team(name2)
    if not one or not two:
        return False
    if one == two:
        return True
    one_words = set(one.split())
    two_words = set(two.split())
    if one_words and two_words:
        overlap = one_words & two_words
        if len(overlap) >= 2 and min(len(one_words), len(two_words)) <= 3:
            return True
    if len(one) > 4 and len(two) > 4:
        if one in two or two in one:
            return True
    return False


def market_key(
    home: str,
    away: str,
    market_type: str = "1x2",
    market_specifier: str = "",
) -> str:
    base = f"{normalize_team(home)} vs {normalize_team(away)}"
    market_type = (market_type or "1x2").strip()
    specifier = (market_specifier or "").strip()
    return f"{base}|{market_type}|{specifier}"


def match_key_similarity(key1: str, key2: str) -> bool:
    if key1 == key2:
        return True
    parts1 = key1.split("|")
    parts2 = key2.split("|")
    if len(parts1) != 3 or len(parts2) != 3:
        return False
    home_away_1, home_away_2 = parts1[0], parts2[0]
    market1, market2 = parts1[1], parts2[1]
    spec1, spec2 = parts1[2], parts2[2]
    if market1 != market2 or spec1 != spec2:
        return False
    teams1 = home_away_1.split(" vs ", 1)
    teams2 = home_away_2.split(" vs ", 1)
    if len(teams1) != 2 or len(teams2) != 2:
        return False
    return teams_match(teams1[0], teams2[0]) and teams_match(teams1[1], teams2[1])


def clean_odd(value: Any, min_odd: float = 1.01, max_odd: float = 50.0) -> Optional[float]:
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


def atomic_json_write(filename: str, data: Any) -> None:
    directory = os.path.dirname(os.path.abspath(filename)) or "."
    temporary = os.path.join(directory, f".{os.path.basename(filename)}.tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, filename)


# ============================================================
# Match record
# ============================================================

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
    return {
        "match": f"{home_team} vs {away_team}",
        "home_team": home_team,
        "away_team": away_team,
        "match_key": market_key(home_team, away_team, market_type, market_specifier),
        "bookmaker": bookmaker,
        "competition": competition,
        "home": home,
        "draw": draw,
        "away": away,
        "sport": sport or "Football",
        "market_type": market_type or "1x2",
        "market_specifier": market_specifier or "",
        "event_id": str(event_id) if event_id is not None else None,
        "scraped_at": utc_timestamp(),
    }


# ============================================================
# History
# ============================================================

history_lock = threading.Lock()


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
    return "::".join(
        [
            str(opportunity.get("sport", "Football")),
            str(opportunity.get("market_type", opportunity.get("type", "1x2"))),
            str(opportunity.get("market_specifier", "")),
            str(opportunity.get("match", "")),
        ]
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
        if len(versions) > 500:
            del versions[:-500]

    for entry in history.values():
        if not entry.get("updated_this_cycle"):
            entry["cycles_missed"] = entry.get("cycles_missed", 0) + 1
            if entry["cycles_missed"] >= 2:
                entry["valid"] = False
        entry.pop("updated_this_cycle", None)


# ============================================================
# Flask app and database
# ============================================================

app = Flask(__name__)

database_url = os.getenv("DATABASE_URL", "sqlite:///users.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = SECRET_KEY

db = SQLAlchemy(app)

CORS(
    app,
    resources={r"/api/*": {"origins": FRONTEND_ORIGIN}},
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)


# ============================================================
# Database models
# ============================================================

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    subscription_status = db.Column(db.String(50), default="free", nullable=False)
    subscription_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def set_password(self, password: str) -> None:
        self.password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def check_password(self, password: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode("utf-8"), self.password_hash.encode("utf-8"))
        except (TypeError, ValueError):
            return False


class CompletedArb(db.Model):
    __tablename__ = "completed_arbs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    match = db.Column(db.String(255), nullable=False)
    profit = db.Column(db.Float, default=0.0, nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    plan = db.Column(db.String(50), nullable=False)
    transaction_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    status = db.Column(db.String(20), default="pending", nullable=False)
    submitted_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)


with app.app_context():
    db.create_all()


# ============================================================
# Authentication helpers
# ============================================================

def serialize_user(user: User) -> Dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "subscription_status": user.subscription_status,
        "subscription_expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
    }


def generate_token(user_id: int) -> str:
    payload = {"user_id": user_id, "exp": utc_now() + timedelta(days=7)}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


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
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            g.user_id = int(payload["user_id"])
        except jwt.ExpiredSignatureError:
            return jsonify({"ok": False, "error": "Token has expired"}), 401
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid authentication token"}), 401
        return function(*args, **kwargs)
    return decorated


def admin_required(function):
    @wraps(function)
    @token_required
    def decorated(*args, **kwargs):
        user = db.session.get(User, g.user_id)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404
        if user.email.lower() not in ADMIN_EMAILS:
            return jsonify({"ok": False, "error": "Admin access required"}), 403
        return function(*args, **kwargs)
    return decorated


def subscription_required(function):
    @wraps(function)
    @token_required
    def decorated(*args, **kwargs):
        user = db.session.get(User, g.user_id)
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404
        now = utc_now()
        expires = user.subscription_expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if user.subscription_status == "free" or not expires or expires <= now:
            return jsonify({"ok": False, "error": "Active subscription required", "code": "SUBSCRIPTION_REQUIRED"}), 403
        return function(*args, **kwargs)
    return decorated


# ============================================================
# Scraper functions
# ============================================================

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
                odds.append(build_match_record(home, away, "SportyBet", home_odd, draw_odd, away_odd, sport=sport))
            over_odd = clean_odd(event.get("over_odd"))
            under_odd = clean_odd(event.get("under_odd"))
            if over_odd and under_odd:
                odds.append(build_match_record(home, away, "SportyBet", over_odd, under_odd, None, sport=sport, market_type="Over/Under 2.5"))
        logger.info("SportyBet: %s records", len(odds))
    except Exception as exc:
        logger.error("SportyBet error: %s", exc)
    return odds


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
                    odds.append(build_match_record(home, away, "ChampionBet", home_odd, draw_odd, away_odd, competition=match.get("leagueName", "")))
                over_odd, under_odd = extract_championbet_ou(bet_map)
                if over_odd and under_odd:
                    odds.append(build_match_record(home, away, "ChampionBet", over_odd, under_odd, None, market_type="Over/Under 2.5"))
                ah_odds, dc_odds, btts_odds = extract_championbet_extra(bet_map)
                # Add Asian Handicap (use actual line)
                for line, (ah_home, ah_away) in ah_odds.items():
                    if ah_home and ah_away:
                        odds.append(build_match_record(home, away, "ChampionBet", ah_home, None, ah_away, market_type="Asian Handicap", market_specifier=str(line)))
                # Add Double Chance (1X vs X2)
                if dc_odds.get("1X") and dc_odds.get("X2"):
                    odds.append(build_match_record(home, away, "ChampionBet", dc_odds["1X"], None, dc_odds["X2"], market_type="Double Chance", market_specifier="1X_X2"))
                # Add BTTS
                if btts_odds.get("yes") and btts_odds.get("no"):
                    odds.append(build_match_record(home, away, "ChampionBet", btts_odds["yes"], None, btts_odds["no"], market_type="BTTS"))
                time.sleep(0.1)
            except Exception:
                logger.exception("ChampionBet match failed")
                continue
        logger.info("ChampionBet: %s records", len(odds))
    except Exception as exc:
        logger.error("ChampionBet error: %s", exc)
    return odds


def extract_championbet_1x2(bet_map):
    def pick(keys):
        for key in keys:
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
    return pick([1, 4, 7]), pick([2, 5, 8]), pick([3, 6, 9])


def extract_championbet_ou(bet_map):
    def pick(keys):
        for key in keys:
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


def extract_championbet_extra(bet_map):
    ah = {}
    dc = {}
    btts = {}
    # Asian Handicap: try keys 5,6,7,8; collect odds and line
    for key in [5, 6, 7, 8]:
        market = bet_map.get(str(key), {})
        if not isinstance(market, dict):
            continue
        for item in market.values():
            if not isinstance(item, dict):
                continue
            odd = clean_odd(item.get("ov"))
            if odd is not None:
                line = item.get("P", key)
                if key in [5, 7]:
                    ah.setdefault(line, [None, None])[0] = odd
                elif key in [6, 8]:
                    ah.setdefault(line, [None, None])[1] = odd
    # Double Chance
    for key, name in [(20, "1X"), (22, "12"), (21, "X2")]:
        market = bet_map.get(str(key), {})
        if not isinstance(market, dict):
            continue
        for item in market.values():
            if not isinstance(item, dict):
                continue
            odd = clean_odd(item.get("ov"))
            if odd is not None:
                dc[name] = odd
    # BTTS
    for key, name in [(19, "yes"), (20, "no")]:
        market = bet_map.get(str(key), {})
        if not isinstance(market, dict):
            continue
        for item in market.values():
            if not isinstance(item, dict):
                continue
            odd = clean_odd(item.get("ov"))
            if odd is not None:
                btts[name] = odd
    return ah, dc, btts


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
                row = dict(zip(headers, cells[:len(headers)]))
                home = row.get("Home")
                away = row.get("Away")
                if not home or not away or home == "-" or away == "-":
                    continue
                home_odd = clean_odd(row.get("1"))
                draw_odd = clean_odd(row.get("X"))
                away_odd = clean_odd(row.get("2"))
                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "AbaBet", home_odd, draw_odd, away_odd, competition=row.get("League", "")))
                over_odd = clean_odd(row.get("Over"))
                under_odd = clean_odd(row.get("Under"))
                if over_odd and under_odd:
                    odds.append(build_match_record(home, away, "AbaBet", over_odd, under_odd, None, market_type="Over/Under 2.5"))
        logger.info("AbaBet: %s records", len(odds))
    except Exception as exc:
        logger.error("AbaBet error: %s", exc)
    return odds


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
                competitor_ids = event.get("competitors", [])
                if len(competitor_ids) < 2:
                    continue
                home = competitors.get(str(competitor_ids[0]), {}).get("name", "")
                away = competitors.get(str(competitor_ids[1]), {}).get("name", "")
                if not home or not away:
                    continue
                home_odd = draw_odd = away_odd = None
                over_odd = under_odd = None
                ah_home = ah_away = None
                dc_home = dc_away = None
                btts_yes = btts_no = None
                for market in event_markets.get(str(event_id), []):
                    market_id = market.get("marketId")
                    market_odds = market.get("odds", {})
                    if market_id == 1:
                        odd_list = []
                        for value in market_odds.values():
                            if isinstance(value, dict) and "odds" in value:
                                odd = clean_odd(value.get("odds"))
                                if odd:
                                    odd_list.append((value.get("outcomeId", 0), odd))
                        odd_list.sort(key=lambda x: x[0])
                        if len(odd_list) >= 3:
                            home_odd, draw_odd, away_odd = odd_list[0][1], odd_list[1][1], odd_list[2][1]
                        elif len(odd_list) == 2:
                            home_odd, away_odd = odd_list[0][1], odd_list[1][1]
                    elif market_id == 5:
                        for value in market_odds.values():
                            if isinstance(value, dict):
                                odd = clean_odd(value.get("odds"))
                                if odd and value.get("outcomeId") == 1:
                                    over_odd = odd
                                elif odd and value.get("outcomeId") == 2:
                                    under_odd = odd
                    elif market_id == 2:
                        for value in market_odds.values():
                            if isinstance(value, dict):
                                odd = clean_odd(value.get("odds"))
                                if odd and value.get("outcomeId") == 1:
                                    ah_home = odd
                                elif odd and value.get("outcomeId") == 2:
                                    ah_away = odd
                    elif market_id == 8:
                        for value in market_odds.values():
                            if isinstance(value, dict):
                                odd = clean_odd(value.get("odds"))
                                if odd and value.get("outcomeId") == 1:
                                    dc_home = odd
                                elif odd and value.get("outcomeId") == 3:
                                    dc_away = odd
                    elif market_id == 12:
                        for value in market_odds.values():
                            if isinstance(value, dict):
                                odd = clean_odd(value.get("odds"))
                                if odd and value.get("outcomeId") == 1:
                                    btts_yes = odd
                                elif odd and value.get("outcomeId") == 2:
                                    btts_no = odd
                sport = str(event.get("sportName") or event.get("sport") or "").lower()
                if "basketball" in sport:
                    sport_name = "Basketball"
                elif "tennis" in sport:
                    sport_name = "Tennis"
                else:
                    sport_name = "Football"
                if home_odd and away_odd:
                    odds.append(build_match_record(home, away, "Fortebet", home_odd, draw_odd, away_odd, sport=sport_name))
                if over_odd and under_odd:
                    odds.append(build_match_record(home, away, "Fortebet", over_odd, under_odd, None, market_type="Over/Under 2.5"))
                if ah_home and ah_away:
                    odds.append(build_match_record(home, away, "Fortebet", ah_home, None, ah_away, market_type="Asian Handicap", market_specifier="-0.5"))
                if dc_home and dc_away:
                    odds.append(build_match_record(home, away, "Fortebet", dc_home, None, dc_away, market_type="Double Chance", market_specifier="1X_X2"))
                if btts_yes and btts_no:
                    odds.append(build_match_record(home, away, "Fortebet", btts_yes, None, btts_no, market_type="BTTS"))
            except Exception:
                logger.exception("Fortebet event failed")
                continue
        logger.info("Fortebet: %s records", len(odds))
    except Exception as exc:
        logger.error("Fortebet error: %s", exc)
    return odds


def scrape_shared_1x_like(bookmaker: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    logger.info(f"Fetching {bookmaker}...")
    odds = []
    base_url = config["base_url"]
    partner = config["partner"]
    lng = config.get("lng", "en")
    tz = config.get("tz", 3)
    gr = config.get("gr", 525)
    referer = config.get("referer", base_url)
    headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"}
    try:
        url = f"{base_url}/service-api/LineFeed/Get1x2_VZip?count=1000&lng={lng}&tz={tz}&mode=4&country=191&partner={partner}&getEmpty=true&gr={gr}"
        data = http.get_json(url, headers=headers)
        values = data.get("Value", []) if isinstance(data, dict) else []
        for match in values:
            home = match.get("O1", "")
            away = match.get("O2", "")
            if not home or not away or (home.strip() == "Home" and away.strip() == "Away"):
                continue
            home_odd = draw_odd = away_odd = None
            for outcome in match.get("E", []):
                t = str(outcome.get("T", "")).strip()
                odd = clean_odd(outcome.get("C"))
                if odd is None:
                    continue
                if t == "1":
                    home_odd = odd
                elif t == "2":
                    draw_odd = odd
                elif t == "3":
                    away_odd = odd
            if home_odd and away_odd:
                odds.append(build_match_record(home, away, bookmaker, home_odd, draw_odd, away_odd))
        logger.info(f"{bookmaker}: {len(odds)} records")
    except Exception as exc:
        logger.error(f"{bookmaker} error: {exc}")
    return odds


def scrape_shared_extra_markets() -> List[Dict[str, Any]]:
    all_odds = []
    for bookmaker, config in SHARED_BOOKMAKERS.items():
        base_url = config["base_url"]
        partner = config["partner"]
        lng = config.get("lng", "en")
        tz = config.get("tz", 3)
        gr = config.get("gr", 525)
        referer = config.get("referer", base_url)
        headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"}
        # Over/Under, Asian Handicap, Double Chance, BTTS via Get1x2_VZip (or separate endpoint)
        try:
            url = f"{base_url}/service-api/LineFeed/Get1x2_VZip?count=1000&lng={lng}&tz={tz}&mode=4&country=191&partner={partner}&getEmpty=true&gr={gr}"
            data = http.get_json(url, headers=headers)
            for match in data.get("Value", []):
                home = match.get("O1", "")
                away = match.get("O2", "")
                if not home or not away or (home.strip() == "Home" and away.strip() == "Away"):
                    continue
                ah_home = ah_away = None
                ou_over = ou_under = None
                dc_home = dc_away = None
                btts_yes = btts_no = None
                for outcome in match.get("E", []):
                    t = str(outcome.get("T", "")).strip()
                    odd = clean_odd(outcome.get("C"))
                    if odd is None:
                        continue
                    p = outcome.get("P")
                    if t == "7" and p is not None:
                        ah_home = odd
                        ah_line = p
                    elif t == "8" and p is not None:
                        ah_away = odd
                        ah_line = p
                    elif t == "9" and p == 2.5:
                        ou_over = odd
                    elif t == "10" and p == 2.5:
                        ou_under = odd
                    elif t == "4":
                        dc_home = odd
                    elif t == "6":
                        dc_away = odd
                    elif t == "19":
                        btts_yes = odd
                    elif t == "20":
                        btts_no = odd
                if ah_home and ah_away:
                    all_odds.append(build_match_record(home, away, bookmaker, ah_home, None, ah_away, market_type="Asian Handicap", market_specifier=str(ah_line)))
                if ou_over and ou_under:
                    all_odds.append(build_match_record(home, away, bookmaker, ou_over, ou_under, None, market_type="Over/Under 2.5"))
                if dc_home and dc_away:
                    all_odds.append(build_match_record(home, away, bookmaker, dc_home, None, dc_away, market_type="Double Chance", market_specifier="1X_X2"))
                if btts_yes and btts_no:
                    all_odds.append(build_match_record(home, away, bookmaker, btts_yes, None, btts_no, market_type="BTTS"))
        except Exception as exc:
            logger.error(f"{bookmaker} extra markets error: {exc}")
    return all_odds


def scrape_kbet() -> List[Dict[str, Any]]:
    logger.info("Fetching kbet...")
    odds = []
    try:
        params = {"status": "Scheduled", "sport_id": 1, "limit": 100}
        data = http.get_json(KBET_API_BASE, params=params)
        events = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
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
                odds.append(build_match_record(home, away, "kbet", home_odd, draw_odd, away_odd, sport="Football"))
        logger.info(f"kbet: {len(odds)} records")
    except Exception as exc:
        logger.error(f"kbet error: {exc}")
    return odds


# ============================================================
# Arbitrage finder
# ============================================================

def create_two_outcome_opportunity(
    match: str, sport: str, market_type: str, market_specifier: str,
    first_bm: str, first_outcome: str, first_odd: float,
    second_bm: str, second_outcome: str, second_odd: float,
    stake: int = DEFAULT_STAKE
) -> Optional[Dict[str, Any]]:
    if not first_odd or not second_odd:
        return None
    arb_sum = 1/first_odd + 1/second_odd
    if arb_sum >= 1:
        return None
    profit_percent = round((1 - arb_sum) * 100, 2)
    if not 0.5 <= profit_percent <= 50:
        return None
    first_stake = round(stake * (1/first_odd) / arb_sum)
    second_stake = round(stake * (1/second_odd) / arb_sum)
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
            {"bookmaker": first_bm, "outcome": first_outcome, "odd": first_odd, "stake": first_stake, "win": round(first_stake * first_odd)},
            {"bookmaker": second_bm, "outcome": second_outcome, "odd": second_odd, "stake": second_stake, "win": round(second_stake * second_odd)},
        ],
    }


def create_three_outcome_opportunity(
    match: str, sport: str,
    home_bm: str, home_odd: float,
    draw_bm: str, draw_odd: float,
    away_bm: str, away_odd: float,
    stake: int = DEFAULT_STAKE
) -> Optional[Dict[str, Any]]:
    if not home_odd or not draw_odd or not away_odd:
        return None
    arb_sum = 1/home_odd + 1/draw_odd + 1/away_odd
    if arb_sum >= 1:
        return None
    profit_percent = round((1 - arb_sum) * 100, 2)
    if not 0.5 <= profit_percent <= 50:
        return None
    home_stake = round(stake * (1/home_odd) / arb_sum)
    draw_stake = round(stake * (1/draw_odd) / arb_sum)
    away_stake = round(stake * (1/away_odd) / arb_sum)
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
            {"bookmaker": home_bm, "outcome": "Home", "odd": home_odd, "stake": home_stake, "win": round(home_stake * home_odd)},
            {"bookmaker": draw_bm, "outcome": "Draw", "odd": draw_odd, "stake": draw_stake, "win": round(draw_stake * draw_odd)},
            {"bookmaker": away_bm, "outcome": "Away", "odd": away_odd, "stake": away_stake, "win": round(away_stake * away_odd)},
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
        for i, first_key in enumerate(keys):
            if first_key in processed:
                continue
            group = list(groups[first_key])
            processed.add(first_key)
            for second_key in keys[i+1:]:
                if second_key in processed:
                    continue
                if match_key_similarity(first_key, second_key):
                    group.extend(groups[second_key])
                    processed.add(second_key)
            merged[first_key] = group

        for match_key, records in merged.items():
            if len(records) < 2:
                continue
            first_record = records[0]
            market_type = first_record.get("market_type", "1x2")
            specifier = first_record.get("market_specifier", "")

            # For 1x2 and football/rugby/futsal, look for three-way arbitrage.
            if market_type == "1x2" and sport in {"Football", "Rugby", "Futsal"}:
                bookmakers = {}
                for r in records:
                    bm = r["bookmaker"]
                    if bm not in bookmakers:
                        bookmakers[bm] = {"home": 0, "draw": 0, "away": 0}
                    if r.get("home"):
                        bookmakers[bm]["home"] = max(bookmakers[bm]["home"], r["home"])
                    if r.get("draw"):
                        bookmakers[bm]["draw"] = max(bookmakers[bm]["draw"], r["draw"])
                    if r.get("away"):
                        bookmakers[bm]["away"] = max(bookmakers[bm]["away"], r["away"])
                bms = list(bookmakers.keys())
                for home_bm in bms:
                    for draw_bm in bms:
                        for away_bm in bms:
                            if len({home_bm, draw_bm, away_bm}) < 3:
                                continue
                            home_odd = bookmakers[home_bm]["home"]
                            draw_odd = bookmakers[draw_bm]["draw"]
                            away_odd = bookmakers[away_bm]["away"]
                            if home_odd and draw_odd and away_odd:
                                opp = create_three_outcome_opportunity(
                                    match_key.split("|")[0], sport, home_bm, home_odd,
                                    draw_bm, draw_odd, away_bm, away_odd
                                )
                                if opp:
                                    opportunities.append(opp)
            else:
                # Two-outcome market
                bookmakers = {}
                for r in records:
                    bm = r["bookmaker"]
                    if bm not in bookmakers:
                        bookmakers[bm] = {"home": 0, "away": 0}
                    if r.get("home"):
                        bookmakers[bm]["home"] = max(bookmakers[bm]["home"], r["home"])
                    if r.get("away"):
                        bookmakers[bm]["away"] = max(bookmakers[bm]["away"], r["away"])
                bms = list(bookmakers.keys())
                for i in range(len(bms)):
                    for j in range(i+1, len(bms)):
                        bm1, bm2 = bms[i], bms[j]
                        home1, away2 = bookmakers[bm1]["home"], bookmakers[bm2]["away"]
                        if home1 and away2:
                            opp = create_two_outcome_opportunity(
                                match_key.split("|")[0], sport, market_type, specifier,
                                bm1, "Home", home1, bm2, "Away", away2
                            )
                            if opp:
                                opportunities.append(opp)
                        home2, away1 = bookmakers[bm2]["home"], bookmakers[bm1]["away"]
                        if home2 and away1:
                            opp = create_two_outcome_opportunity(
                                match_key.split("|")[0], sport, market_type, specifier,
                                bm2, "Home", home2, bm1, "Away", away1
                            )
                            if opp:
                                opportunities.append(opp)

    # Deduplicate: keep best opportunity per match+market+line
    best = {}
    for opp in opportunities:
        ident = f"{opp['match']}::{opp['market_type']}::{opp['market_specifier']}"
        if ident not in best or opp['profit_percent'] > best[ident]['profit_percent']:
            best[ident] = opp
    return list(best.values())


# ============================================================
# Run scan
# ============================================================

scan_lock = threading.Lock()


def run_scan() -> List[Dict[str, Any]]:
    logger.info("Starting arbitrage scan...")
    all_odds = []
    scrapers = [
        scrape_sportybet,
        scrape_championbet,
        scrape_ababet,
        scrape_fortebet,
        lambda: scrape_shared_1x_like("1xBet", SHARED_BOOKMAKERS["1xBet"]),
        lambda: scrape_shared_1x_like("22Bet", SHARED_BOOKMAKERS["22Bet"]),
        lambda: scrape_shared_1x_like("Melbet", SHARED_BOOKMAKERS["Melbet"]),
        scrape_shared_extra_markets,
        scrape_kbet,
    ]
    for scraper in scrapers:
        try:
            all_odds.extend(scraper())
        except Exception as exc:
            logger.exception(f"Scraper failed: {exc}")

    opportunities = find_arbitrage(all_odds)
    logger.info(f"Found {len(opportunities)} opportunities")

    # Update history and current file
    with history_lock:
        history = load_arbitrage_history()
        timestamp = utc_timestamp()
        update_arbitrage_history(opportunities, history, timestamp)
        save_arbitrage_history(history)

        # Save current valid opportunities
        current_valid = [opp for opp in opportunities]
        atomic_json_write(OPPORTUNITIES_FILE, current_valid)

    # Send Telegram alerts for new high-profit opportunities (optional, simplified)
    # ...

    return opportunities


# ============================================================
# API routes
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": utc_now().isoformat()})


@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    name = str(data.get("name", "")).strip()
    if not email or len(password) < 6:
        return jsonify({"ok": False, "error": "Invalid email or password"}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"ok": False, "error": "Email already registered"}), 409
    user = User(email=email, name=name or None, subscription_status="free")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify({"ok": True, "token": generate_token(user.id), "user": serialize_user(user)}), 201


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401
    return jsonify({"ok": True, "token": generate_token(user.id), "user": serialize_user(user)})


@app.route("/api/me", methods=["GET"])
@token_required
def me():
    user = db.session.get(User, g.user_id)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    return jsonify({"ok": True, "user": serialize_user(user)})


@app.route("/api/arbs", methods=["GET"])
@subscription_required
def get_arbs():
    if not os.path.exists(OPPORTUNITIES_FILE):
        return jsonify({"ok": True, "arbs": [], "count": 0})
    with open(OPPORTUNITIES_FILE, "r", encoding="utf-8") as f:
        arbs = json.load(f)
    return jsonify({"ok": True, "arbs": arbs, "count": len(arbs)})


@app.route("/api/history", methods=["GET"])
@token_required
def get_history():
    history = load_arbitrage_history()
    entries = []
    for key, entry in history.items():
        entries.append({
            "key": key,
            "match": entry.get("match", ""),
            "sport": entry.get("sport", "Football"),
            "market_type": entry.get("market_type", "1x2"),
            "market_specifier": entry.get("market_specifier", ""),
            "first_seen": entry.get("first_seen", ""),
            "last_seen": entry.get("last_seen", ""),
            "valid": entry.get("valid", False),
            "versions": entry.get("versions", []),
        })
    entries.sort(key=lambda x: x["last_seen"], reverse=True)
    return jsonify({"ok": True, "history": entries})


@app.route("/api/scan", methods=["POST"])
@admin_required
def api_scan():
    with scan_lock:
        try:
            opportunities = run_scan()
            return jsonify({"ok": True, "count": len(opportunities)})
        except Exception as exc:
            logger.exception("Manual scan failed")
            return jsonify({"ok": False, "error": "Scan failed"}), 500


@app.route("/api/complete", methods=["POST"])
@token_required
def complete_arb():
    data = request.get_json(silent=True) or {}
    match = data.get("match", "Unknown")
    profit = float(data.get("profit", 0.0))
    if profit < 0 or profit > 1_000_000:
        return jsonify({"ok": False, "error": "Invalid profit"}), 400
    record = CompletedArb(user_id=g.user_id, match=match, profit=profit)
    db.session.add(record)
    db.session.commit()
    return jsonify({"ok": True, "message": "Arbitrage recorded"})


@app.route("/api/payments", methods=["POST"])
@token_required
def submit_payment():
    data = request.get_json(silent=True) or {}
    plan = str(data.get("plan", "")).strip().lower()
    transaction_id = str(data.get("transaction_id", "")).strip().upper()
    if plan not in VALID_PLANS:
        return jsonify({"ok": False, "error": "Invalid plan"}), 400
    if not transaction_id:
        return jsonify({"ok": False, "error": "Transaction ID required"}), 400
    existing = Payment.query.filter_by(transaction_id=transaction_id).first()
    if existing:
        return jsonify({"ok": False, "error": "Transaction ID already used"}), 409
    payment = Payment(user_id=g.user_id, plan=plan, transaction_id=transaction_id, status="pending")
    db.session.add(payment)
    db.session.commit()
    return jsonify({"ok": True, "message": "Payment submitted for review"})


# Admin payment routes
@app.route("/api/admin/payments", methods=["GET"])
@admin_required
def admin_list_payments():
    payments = Payment.query.order_by(Payment.submitted_at.desc()).all()
    result = []
    for p in payments:
        user = db.session.get(User, p.user_id)
        result.append({
            "id": p.id,
            "email": user.email if user else "?",
            "plan": p.plan,
            "transaction_id": p.transaction_id,
            "status": p.status,
            "submitted_at": p.submitted_at.isoformat(),
            "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        })
    return jsonify({"ok": True, "payments": result})


@app.route("/api/admin/payments/<int:payment_id>/approve", methods=["POST"])
@admin_required
def admin_approve_payment(payment_id):
    payment = Payment.query.get(payment_id)
    if not payment:
        return jsonify({"ok": False, "error": "Payment not found"}), 404
    if payment.status != "pending":
        return jsonify({"ok": False, "error": "Already processed"}), 409
    user = db.session.get(User, payment.user_id)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    # Set subscription based on plan
    days = {"day": 1, "monthly": 30, "quarterly": 90}[payment.plan]
    user.subscription_status = payment.plan
    user.subscription_expires_at = utc_now() + timedelta(days=days)
    payment.status = "approved"
    payment.approved_at = utc_now()
    db.session.commit()
    return jsonify({"ok": True, "message": "Payment approved"})


@app.route("/api/admin/payments/<int:payment_id>/reject", methods=["POST"])
@admin_required
def admin_reject_payment(payment_id):
    payment = Payment.query.get(payment_id)
    if not payment:
        return jsonify({"ok": False, "error": "Payment not found"}), 404
    if payment.status != "pending":
        return jsonify({"ok": False, "error": "Already processed"}), 409
    payment.status = "rejected"
    db.session.commit()
    return jsonify({"ok": True, "message": "Payment rejected"})


# ============================================================
# Frontend serving
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

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
    content = "User-agent: *\nAllow: /\nSitemap: https://abrt-scraper-1-51d7.onrender.com/sitemap.xml\n"
    return Response(content, mimetype='text/plain')

@app.route("/", methods=["GET"])
def serve_frontend():
    if not os.path.exists(INDEX_FILE):
        return jsonify({"ok": False, "error": "index.html not found"}), 404
    return send_file(INDEX_FILE)

@app.route("/<path:path>", methods=["GET"])
def frontend_fallback(path):
    if path.startswith("api/"):
        return jsonify({"ok": False, "error": "API route not found"}), 404
    if not os.path.exists(INDEX_FILE):
        return jsonify({"ok": False, "error": "index.html not found"}), 404
    return send_file(INDEX_FILE)

# ============================================================
# Scheduler (disabled by default)
# ============================================================

if ENABLE_SCHEDULER:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: (scan_lock.acquire(blocking=False), run_scan_and_store()),
        trigger=IntervalTrigger(minutes=SCAN_INTERVAL_MINUTES),
        id="arbitrage_scanner",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started (every {SCAN_INTERVAL_MINUTES} minutes)")
else:
    logger.info("Scheduler disabled (set ENABLE_SCHEDULER=true to enable)")


def run_scan_and_store():
    if not scan_lock.acquire(blocking=False):
        return
    try:
        run_scan()
    finally:
        scan_lock.release()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
