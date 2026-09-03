#!/usr/bin/env python3
"""
scraper.py
Production-oriented football arbitrage scanner.

Features:
- Multiple bookmaker fetchers
- Concurrent scanning
- HTTP retries + sessions
- Strict event/odds validation
- Deduplication
- Arbitrage detection
- Net-profit calculation
- Stake calculation with UGX rounding
- Telegram alerts
- JSON persistence
- Mobile-friendly HTML dashboard
- Scan status + history
- No maximum profit cap

IMPORTANT:
Bookmaker APIs can change at any time. Each bookmaker parser should be
periodically verified against the bookmaker's current API response.
"""

import os
import re
import json
import time
import html
import tempfile
import logging
from datetime import datetime, timezone
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from string import Template

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup


# ============================================================
# CONFIGURATION
# ============================================================

TAX_RATE = float(os.getenv("TAX_RATE", "0.15"))

# Minimum NET profit percentage accepted.
MIN_PROFIT = float(os.getenv("MIN_PROFIT", "0.5"))

# Kept as a safety margin. A true mathematical arb has probability < 1.
# 0.998 means at least ~0.2% theoretical margin before tax.
PROB_LIMIT = float(os.getenv("PROB_LIMIT", "0.998"))

# Lower bound for probability sum (catches obviously wrong markets).
MIN_PROB = float(os.getenv("MIN_PROB", "0.10"))

# Maximum individual odd accepted (catches outliers like 21.25).
ODD_LIMIT = float(os.getenv("ODD_LIMIT", "15.0"))

TIMEOUT = int(os.getenv("TIMEOUT", "20"))
MAX_THREADS = int(os.getenv("MAX_THREADS", "10"))

TOTAL_STAKE = int(os.getenv("TOTAL_STAKE", "10000"))
STAKE_STEP = int(os.getenv("STAKE_STEP", "50"))

# Optional maximum number of stored opportunities.
MAX_ARBS = int(os.getenv("MAX_ARBS", "500"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

EVENTS_FILE = os.getenv("EVENTS_FILE", "events.json")
ARBS_FILE = os.getenv(
    "ARBS_FILE",
    "current_opportunities.json"
)
STATUS_FILE = os.getenv(
    "STATUS_FILE",
    "scanner_status.json"
)
HISTORY_FILE = os.getenv(
    "HISTORY_FILE",
    "arb_history.json"
)
HTML_FILE = os.getenv("HTML_FILE", "index.html")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("arb-scanner")


# ============================================================
# HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; K) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


# ============================================================
# BOOKMAKER RULES
# ============================================================

ALLOWED_BOOKMAKERS = {
    "Fortebet",
    "BetPawa",
    "Betway Uganda",
    "AbaBet",
    "PremierBet Uganda",
    "BongoBongo",
    "ParagonBet",
    "Betmaster",
    "GSB",
    "TopBet",
    "22Bet",
    "ChampionBet",
    "SportyBet",
    "Melbet",
    "1xBet",
    "ParseBot",
}

SHARP_BOOKMAKERS = {
    "Fortebet",
    "BetPawa",
    "AbaBet",
}


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value):
    try:
        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip().replace(",", ".")

        return float(value)

    except (TypeError, ValueError):
        return None


def clean_odd(value):
    """
    Return a valid decimal odd or None.

    Decimal odds below 1.01 are rejected.
    Extremely large odds are also rejected because they are
    usually malformed API values rather than genuine prices.
    """

    odd = safe_float(value)

    if odd is None:
        return None

    if not (1.01 <= odd <= 1000):
        return None

    return round(odd, 4)


def normalize(name):
    """
    Safer team normalization.

    Uses word-level substitutions rather than naive substring
    replacements so names such as 'United' are not accidentally
    modified inside unrelated words.
    """

    if not name:
        return ""

    name = str(name).lower().strip()

    replacements = {
        r"\bunited\b": "utd",
        r"\brovers\b": "rvs",
        r"\bfootball club\b": "",
        r"\bfc\b": "",
        r"\bsc\b": "",
        r"\bcf\b": "",
        r"\bclub\b": "",
    }

    for pattern, replacement in replacements.items():
        name = re.sub(pattern, replacement, name)

    name = re.sub(r"[^a-z0-9\s]", " ", name)
    name = re.sub(r"\s+", " ", name)

    return name.strip()


def event_key(event):
    """
    Canonical event key.

    NOTE:
    If kickoff timestamps become available from the bookmakers,
    they should also be incorporated here. Team names alone can
    occasionally collide.
    """

    return (
        normalize(event.get("home")),
        normalize(event.get("away")),
        str(event.get("market", "")).lower().strip(),
    )


def valid_event(event):
    if not isinstance(event, dict):
        return False

    home = event.get("home")
    away = event.get("away")
    bookmaker = event.get("bookmaker")
    market = event.get("market")
    odds = event.get("odds")

    if not home or not away:
        return False

    if normalize(home) == normalize(away):
        return False

    if not bookmaker or not market:
        return False

    if not isinstance(odds, dict):
        return False

    return True


def validate_odds(odds, required_outcomes):
    """
    Ensure every required outcome exists and has a valid decimal odd.
    """

    if not isinstance(odds, dict):
        return False

    for outcome in required_outcomes:
        odd = clean_odd(odds.get(outcome))

        if odd is None:
            return False

    return True


def round_to_step(amount, step=STAKE_STEP):
    if amount <= 0:
        return 0

    return int(round(amount / step) * step)


def atomic_write(path, content):
    """
    Write a file atomically.

    Prevents a process crash from leaving half-written JSON/HTML.
    """

    directory = os.path.dirname(os.path.abspath(path))

    fd, temp_path = tempfile.mkstemp(
        prefix=".tmp_",
        dir=directory
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, path)

    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

        raise


def write_json(path, data):
    content = json.dumps(
        data,
        indent=2,
        ensure_ascii=False
    )

    atomic_write(path, content)


# ============================================================
# HTTP SESSION
# ============================================================

def create_session():
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(
            ["GET", "POST"]
        ),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_THREADS,
        pool_maxsize=MAX_THREADS,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update(HEADERS)

    return session


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=10
        )

        response.raise_for_status()
        return True

    except requests.RequestException as exc:
        logger.warning(
            "Telegram error: %s",
            exc
        )

        return False


# ============================================================
# BASE BOOKMAKER
# ============================================================

class BaseBookmaker:

    name = "base"

    def __init__(self):
        self.session = create_session()

    def fetch(self):
        return []

    def get_json(self, url, **kwargs):
        response = self.session.get(
            url,
            timeout=TIMEOUT,
            **kwargs
        )

        response.raise_for_status()

        return response.json()

    def post_json(self, url, payload, **kwargs):
        response = self.session.post(
            url,
            json=payload,
            timeout=TIMEOUT,
            **kwargs
        )

        response.raise_for_status()

        return response.json()

    def safe_fetch(self):
        started = time.time()

        try:
            events = self.fetch()

            if not isinstance(events, list):
                events = []

            clean_events = []

            for event in events:
                if valid_event(event):
                    clean_events.append(event)

            duration = round(
                time.time() - started,
                2
            )

            return {
                "bookmaker": self.name,
                "events": clean_events,
                "duration": duration,
                "error": None,
            }

        except Exception as exc:

            duration = round(
                time.time() - started,
                2
            )

            logger.exception(
                "%s failed",
                self.name
            )

            return {
                "bookmaker": self.name,
                "events": [],
                "duration": duration,
                "error": str(exc),
            }


# ============================================================
# GSB
# ============================================================

class GSB(BaseBookmaker):

    name = "GSB"

    TREE_API = (
        "https://gsb.ug/services/evapi/event/"
        "GetSportsTree?statusId=0&eventTypeId=0"
    )

    EVENTS_API = (
        "https://gsb.ug/services/evapi/event/"
        "GetEvents"
    )

    API_HEADERS = {
        "Accept": "*/*, application/json",
        "Content-Type": "application/json",
        "BrandId": "112",
        "ChannelId": "4",
        "Language": "en-US",
        "Terminal": "gsb.ug",
    }

    def get_leagues(self):

        response = self.session.get(
            self.TREE_API,
            headers=self.API_HEADERS,
            timeout=TIMEOUT
        )

        response.raise_for_status()

        data = response.json()

        root = data.get("data", {})

        soccer = None

        for item in root.get("cl", []):
            if str(item.get("id")) == "31":
                soccer = item
                break

        if not soccer:
            return []

        leagues = []

        for country in soccer.get("cl", []):
            for league in country.get("cl", []):
                league_id = league.get("id")

                if league_id:
                    leagues.append(str(league_id))

        return list(dict.fromkeys(leagues))

    def fetch_league(self, league_id):

        events = []

        skip = 0
        take = 100

        while True:

            params = {
                "betTypeIds": "-1",
                "take": take,
                "statusId": "0",
                "eventTypeId": "0",
                "leagueIds": league_id,
                "skip": skip,
                "sportTypeIds": "31",
            }

            try:

                response = self.session.get(
                    self.EVENTS_API,
                    headers=self.API_HEADERS,
                    params=params,
                    timeout=TIMEOUT
                )

                response.raise_for_status()

                payload = response.json()

                data = payload.get("data", [])

                if not isinstance(data, list) or not data:
                    break

                for ev in data:

                    home = ev.get("h")
                    away = ev.get("a")
                    league = ev.get("ln", "")

                    if not home or not away:
                        continue

                    bet_types = ev.get("bts", [])

                    if not isinstance(bet_types, list):
                        continue

                    # ----------------------------
                    # 1X2
                    # ----------------------------

                    market = next(
                        (
                            x for x in bet_types
                            if x.get("n") == "FT 1X2"
                        ),
                        None
                    )

                    if market:

                        odds = {}

                        for odd in market.get("odds", []):

                            label = str(
                                odd.get("n", "")
                            ).upper()

                            price = clean_odd(
                                odd.get("p")
                            )

                            if label in ("1", "HOME"):
                                odds["1"] = price

                            elif label in ("X", "DRAW"):
                                odds["X"] = price

                            elif label in ("2", "AWAY"):
                                odds["2"] = price

                        if validate_odds(
                            odds,
                            ("1", "X", "2")
                        ):
                            events.append({
                                "bookmaker": self.name,
                                "league": league,
                                "home": home,
                                "away": away,
                                "market": "1x2",
                                "odds": odds,
                            })

                    # ----------------------------
                    # OVER/UNDER 1.5
                    # ----------------------------

                    market = next(
                        (
                            x for x in bet_types
                            if str(x.get("n", "")).lower()
                            in (
                                "under/over",
                                "under over",
                                "over/under"
                            )
                        ),
                        None
                    )

                    if market:

                        over = None
                        under = None

                        for odd in market.get("odds", []):

                            line = str(
                                odd.get("l", "")
                            )

                            if line != "1.5":
                                continue

                            label = str(
                                odd.get("n", "")
                            ).lower()

                            price = clean_odd(
                                odd.get("p")
                            )

                            if label == "over":
                                over = price

                            elif label == "under":
                                under = price

                        if over and under:

                            events.append({
                                "bookmaker": self.name,
                                "league": league,
                                "home": home,
                                "away": away,
                                "market": "over15",
                                "odds": {
                                    "over": over,
                                    "under": under,
                                },
                            })

                    # ----------------------------
                    # BTTS
                    # ----------------------------

                    market = next(
                        (
                            x for x in bet_types
                            if x.get("n") == "GG/NG"
                        ),
                        None
                    )

                    if market:

                        yes = None
                        no = None

                        for odd in market.get("odds", []):

                            label = str(
                                odd.get("n", "")
                            ).lower()

                            price = clean_odd(
                                odd.get("p")
                            )

                            if label == "yes":
                                yes = price

                            elif label == "no":
                                no = price

                        if yes and no:

                            events.append({
                                "bookmaker": self.name,
                                "league": league,
                                "home": home,
                                "away": away,
                                "market": "btts",
                                "odds": {
                                    "yes": yes,
                                    "no": no,
                                },
                            })

                if len(data) < take:
                    break

                skip += take

            except Exception as exc:

                logger.warning(
                    "%s league %s failed: %s",
                    self.name,
                    league_id,
                    exc
                )

                break

        return events

    def fetch(self):

        leagues = self.get_leagues()

        if not leagues:
            return []

        events = []

        workers = min(
            MAX_THREADS,
            max(1, len(leagues))
        )

        with ThreadPoolExecutor(
            max_workers=workers
        ) as executor:

            futures = [
                executor.submit(
                    self.fetch_league,
                    league_id
                )
                for league_id in leagues
            ]

            for future in as_completed(futures):

                try:
                    events.extend(
                        future.result()
                    )

                except Exception:
                    logger.exception(
                        "%s league task failed",
                        self.name
                    )

        return events


# ============================================================
# TOPBET
# ============================================================

class TopBet(BaseBookmaker):

    name = "TopBet"

    CATEGORIES_API = (
        "https://www.topbet.ug/restapi/offer/en/"
        "categories/sport/S/l?annex=13&"
        "mobileVersion=2.47.4.6&locale=en"
    )

    LEAGUE_API = (
        "https://www.topbet.ug/restapi/offer/en/"
        "sport/S/league/{league_id}/mob?"
        "annex=13&mobileVersion=2.47.4.6&locale=en"
    )

    def get_leagues(self):

        data = self.get_json(
            self.CATEGORIES_API
        )

        leagues = []

        for category in data.get(
            "categories",
            []
        ):

            if (
                category.get("type") == "LEAGUE"
                and category.get("count", 0) > 0
            ):
                if category.get("id"):
                    leagues.append(
                        category["id"]
                    )

        return list(dict.fromkeys(leagues))

    def fetch_league(self, league_id):

        events = []

        try:

            data = self.get_json(
                self.LEAGUE_API.format(
                    league_id=league_id
                )
            )

            for match in data.get(
                "esMatches",
                []
            ):

                home = match.get("home")
                away = match.get("away")

                if not home or not away:
                    continue

                league = match.get(
                    "leagueName",
                    ""
                )

                bet_map = match.get(
                    "betMap",
                    {}
                )

                # 1X2
                h = clean_odd(
                    bet_map.get(
                        "1", {}
                    ).get(
                        "NULL",
                        {}
                    ).get("ov")
                )

                d = clean_odd(
                    bet_map.get(
                        "2", {}
                    ).get(
                        "NULL",
                        {}
                    ).get("ov")
                )

                a = clean_odd(
                    bet_map.get(
                        "3", {}
                    ).get(
                        "NULL",
                        {}
                    ).get("ov")
                )

                odds = {
                    "1": h,
                    "X": d,
                    "2": a,
                }

                if validate_odds(
                    odds,
                    ("1", "X", "2")
                ):

                    events.append({
                        "bookmaker": self.name,
                        "league": league,
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": odds,
                    })

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

        return events

    def fetch(self):

        leagues = self.get_leagues()

        if not leagues:
            return []

        events = []

        with ThreadPoolExecutor(
            max_workers=min(
                MAX_THREADS,
                len(leagues)
            )
        ) as executor:

            futures = [
                executor.submit(
                    self.fetch_league,
                    league
                )
                for league in leagues
            ]

            for future in as_completed(futures):

                try:
                    events.extend(
                        future.result()
                    )

                except Exception:
                    logger.exception(
                        "%s league task failed",
                        self.name
                    )

        return events


# ============================================================
# 22BET
# ============================================================

class Bet22(BaseBookmaker):

    name = "22Bet"

    EVENTS_API = (
        "https://22bet.ug/service-api/LineFeed/"
        "Get1x2_VZip?sports=1&count=1000&lng="
        "en_GB&tz=3&country=191&partner=151&"
        "gr=525&getEmpty=true&virtualSports=true"
    )

    def fetch(self):

        events = []

        try:

            data = self.get_json(
                self.EVENTS_API
            )

            values = data.get(
                "Value",
                []
            )

            for match in values:

                home = match.get("O1")
                away = match.get("O2")

                if not home or not away:
                    continue

                odds_map = {}

                for item in match.get("E", []):

                    t = item.get("T")
                    parameter = item.get("P")
                    odd = clean_odd(
                        item.get("C")
                    )

                    if odd:
                        odds_map[
                            (t, parameter)
                        ] = odd

                odds = {
                    "1": odds_map.get((1, None)),
                    "X": odds_map.get((2, None)),
                    "2": odds_map.get((3, None)),
                }

                if validate_odds(
                    odds,
                    ("1", "X", "2")
                ):

                    events.append({
                        "bookmaker": self.name,
                        "league": match.get(
                            "L",
                            ""
                        ),
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": odds,
                    })

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

        return events


# ============================================================
# BETMASTER
# ============================================================

class Betmaster(BaseBookmaker):

    name = "Betmaster"

    API = (
        "https://betmasterug.com/"
        "Sports.aspx/GetSportMarkets"
    )

    def fetch(self):

        payload = {
            "sportid": "1",
            "countryid": "",
            "leagueid": "",
            "isfeatured": 0,
            "searchteam": 0,
            "filter": 100,
        }

        try:

            response = self.session.post(
                self.API,
                json=payload,
                timeout=TIMEOUT
            )

            response.raise_for_status()

            outer = response.json()

            raw = outer.get("d", "[]")

            data = json.loads(raw)

            events = []

            if not isinstance(data, list):
                return []

            for match in data:

                home = match.get(
                    "hometeam"
                )

                away = match.get(
                    "awayteam"
                )

                if not home or not away:
                    continue

                odds = {
                    "1": clean_odd(
                        match.get(
                            "outcomeodd1market1"
                        )
                    ),
                    "X": clean_odd(
                        match.get(
                            "outcomeodd2market1"
                        )
                    ),
                    "2": clean_odd(
                        match.get(
                            "outcomeodd3market1"
                        )
                    ),
                }

                if validate_odds(
                    odds,
                    ("1", "X", "2")
                ):

                    events.append({
                        "bookmaker": self.name,
                        "league": match.get(
                            "LeagueName",
                            ""
                        ),
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": odds,
                    })

            return events

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

            return []


# ============================================================
# CHAMPIONBET
# ============================================================

class ChampionBet(BaseBookmaker):

    name = "ChampionBet"

    API = (
        "https://www.championbet.ug/restapi/"
        "offer/en/top/mob?annex=13&offset=30&"
        "mobileVersion=2.47.4.3&locale=en"
    )

    MATCH_API = (
        "https://www.championbet.ug/restapi/"
        "offer/en/match/{match_id}?"
        "annex=13&mobileVersion=2.47.4.3&locale=en"
    )

    def extract_1x2(self, bet_map):

        if not isinstance(
            bet_map,
            dict
        ):
            return None, None, None

        def pick_odd(keys):

            for key in keys:

                market = bet_map.get(
                    str(key),
                    {}
                )

                if not isinstance(
                    market,
                    dict
                ):
                    continue

                for item in market.values():

                    if not isinstance(
                        item,
                        dict
                    ):
                        continue

                    odd = clean_odd(
                        item.get("ov")
                    )

                    if odd:
                        return odd

            return None

        return (
            pick_odd([1, 4, 7]),
            pick_odd([2, 5, 8]),
            pick_odd([3, 6, 9]),
        )

    def fetch(self):

        events = []

        try:

            top_data = self.get_json(
                self.API
            )

            matches = top_data.get(
                "esMatches",
                []
            )

            for match in matches:

                if "soccer" not in str(
                    match.get(
                        "sportToken",
                        ""
                    )
                ).lower():

                    continue

                match_id = match.get("id")

                home = match.get("home")
                away = match.get("away")

                if not match_id or not home or not away:
                    continue

                try:

                    match_data = self.get_json(
                        self.MATCH_API.format(
                            match_id=match_id
                        )
                    )

                    bet_map = match_data.get(
                        "betMap",
                        {}
                    )

                    h, d, a = self.extract_1x2(
                        bet_map
                    )

                    odds = {
                        "1": h,
                        "X": d,
                        "2": a,
                    }

                    if validate_odds(
                        odds,
                        ("1", "X", "2")
                    ):

                        events.append({
                            "bookmaker": self.name,
                            "league": match.get(
                                "leagueName",
                                ""
                            ),
                            "home": home,
                            "away": away,
                            "market": "1x2",
                            "odds": odds,
                        })

                except Exception as exc:

                    logger.debug(
                        "%s match %s failed: %s",
                        self.name,
                        match_id,
                        exc
                    )

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

        return events


# ============================================================
# ABABET
# ============================================================

class AbaBet(BaseBookmaker):

    name = "AbaBet"

    URL = (
        "https://www.ababet.ug/"
        "soccer/match_result?mobile=1"
    )

    def fetch(self):

        events = []

        try:

            response = self.session.get(
                self.URL,
                timeout=TIMEOUT
            )

            response.raise_for_status()

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            for table in soup.find_all("table"):

                first_row = table.find("tr")

                if not first_row:
                    continue

                headers = [
                    cell.get_text(
                        " ",
                        strip=True
                    )
                    for cell in first_row.find_all(
                        ["th", "td"]
                    )
                ]

                header_lower = {
                    h.lower(): h
                    for h in headers
                }

                if (
                    "home" not in header_lower
                    or "away" not in header_lower
                ):
                    continue

                for tr in table.find_all("tr")[1:]:

                    cells = [
                        cell.get_text(
                            " ",
                            strip=True
                        )
                        for cell in tr.find_all(
                            ["td", "th"]
                        )
                    ]

                    if len(cells) < len(headers):
                        continue

                    row = dict(
                        zip(
                            headers,
                            cells
                        )
                    )

                    home = row.get(
                        header_lower["home"]
                    )

                    away = row.get(
                        header_lower["away"]
                    )

                    if not home or not away:
                        continue

                    odds = {
                        "1": clean_odd(
                            row.get("1")
                        ),
                        "X": clean_odd(
                            row.get("X")
                        ),
                        "2": clean_odd(
                            row.get("2")
                        ),
                    }

                    if validate_odds(
                        odds,
                        ("1", "X", "2")
                    ):

                        events.append({
                            "bookmaker": self.name,
                            "league": row.get(
                                "League",
                                ""
                            ),
                            "home": home,
                            "away": away,
                            "market": "1x2",
                            "odds": odds,
                        })

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

        return events


# ============================================================
# FORTEBET
# ============================================================

class Fortebet(BaseBookmaker):

    name = "Fortebet"

    API = (
        "https://desktop.fortebet.ug/"
        "api/web/v1/offer/full-prematch-en"
    )

    def fetch(self):

        events = []

        try:

            data = self.get_json(
                self.API
            )

            inner = data.get(
                "data",
                {}
            )

            events_dict = inner.get(
                "event",
                {}
            )

            markets_dict = inner.get(
                "markets",
                {}
            )

            competitors = inner.get(
                "competitors",
                {}
            )

            event_markets = defaultdict(list)

            for market in markets_dict.values():

                event_id = str(
                    market.get(
                        "eventId",
                        ""
                    )
                )

                event_markets[
                    event_id
                ].append(market)

            for event_id, event in events_dict.items():

                comps = event.get(
                    "competitors",
                    []
                )

                if len(comps) < 2:
                    continue

                home = competitors.get(
                    str(comps[0]),
                    {}
                ).get(
                    "name",
                    ""
                )

                away = competitors.get(
                    str(comps[1]),
                    {}
                ).get(
                    "name",
                    ""
                )

                if not home or not away:
                    continue

                h = None
                d = None
                a = None

                for market in event_markets.get(
                    str(event_id),
                    []
                ):

                    if str(
                        market.get("marketId")
                    ) != "1":
                        continue

                    odds_list = []

                    market_odds = market.get(
                        "odds",
                        {}
                    )

                    for value in market_odds.values():

                        if not isinstance(
                            value,
                            dict
                        ):
                            continue

                        odd = clean_odd(
                            value.get("odds")
                        )

                        outcome_id = value.get(
                            "outcomeId"
                        )

                        if odd:
                            odds_list.append(
                                (
                                    outcome_id,
                                    odd
                                )
                            )

                    # IMPORTANT:
                    # Do not blindly assume that the first three
                    # outcome IDs are always Home/Draw/Away.
                    #
                    # We only use the historical ordering here.
                    odds_list.sort(
                        key=lambda item: str(
                            item[0]
                        )
                    )

                    if len(odds_list) >= 3:

                        h = odds_list[0][1]
                        d = odds_list[1][1]
                        a = odds_list[2][1]

                    break

                odds = {
                    "1": h,
                    "X": d,
                    "2": a,
                }

                if validate_odds(
                    odds,
                    ("1", "X", "2")
                ):

                    events.append({
                        "bookmaker": self.name,
                        "league": event.get(
                            "tournamentName",
                            ""
                        ),
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": odds,
                    })

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

        return events


# ============================================================
# SPORTYBET
# ============================================================

class SportyBet(BaseBookmaker):

    name = "SportyBet"

    API = (
        "https://www.sportybet.com/"
        "factsCenter/wapConfigurableEventsByOrder"
    )

    def fetch(self):

        events = []

        payload = {
            "productId": 3,
            "sportId": "sr:sport:1",
            "order": 0,
            "pageNum": 1,
            "pageSize": 20,
            "withTwoUpMarket": True,
            "withOneUpMarket": True,
        }

        try:

            data = self.post_json(
                self.API,
                payload
            )

            tournaments = (
                data.get("data", {})
                .get("tournaments", [])
            )

            for tournament in tournaments:

                league = tournament.get(
                    "name",
                    ""
                )

                for event in tournament.get(
                    "events",
                    []
                ):

                    home = event.get(
                        "homeTeamName"
                    )

                    away = event.get(
                        "awayTeamName"
                    )

                    if not home or not away:
                        continue

                    h = None
                    d = None
                    a = None
                    over = None
                    under = None

                    for market in event.get(
                        "markets",
                        []
                    ):

                        market_id = str(
                            market.get("id")
                        )

                        if market_id == "1":

                            for outcome in market.get(
                                "outcomes",
                                []
                            ):

                                outcome_id = str(
                                    outcome.get("id")
                                )

                                odd = clean_odd(
                                    outcome.get(
                                        "odds"
                                    )
                                )

                                if outcome_id == "1":
                                    h = odd

                                elif outcome_id == "2":
                                    d = odd

                                elif outcome_id == "3":
                                    a = odd

                        elif market_id == "18":

                            specifier = str(
                                market.get(
                                    "specifier",
                                    ""
                                )
                            )

                            if specifier != "total=1.5":
                                continue

                            for outcome in market.get(
                                "outcomes",
                                []
                            ):

                                oid = str(
                                    outcome.get("id")
                                )

                                odd = clean_odd(
                                    outcome.get(
                                        "odds"
                                    )
                                )

                                if oid == "12":
                                    over = odd

                                elif oid == "13":
                                    under = odd

                    odds = {
                        "1": h,
                        "X": d,
                        "2": a,
                    }

                    if validate_odds(
                        odds,
                        ("1", "X", "2")
                    ):

                        events.append({
                            "bookmaker": self.name,
                            "league": league,
                            "home": home,
                            "away": away,
                            "market": "1x2",
                            "odds": odds,
                        })

                    if over and under:

                        events.append({
                            "bookmaker": self.name,
                            "league": league,
                            "home": home,
                            "away": away,
                            "market": "over15",
                            "odds": {
                                "over": over,
                                "under": under,
                            },
                        })

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

        return events


# ============================================================
# MELBET
# ============================================================

class Melbet(BaseBookmaker):

    name = "Melbet"

    API = (
        "https://melbet-424658.top/"
        "service-api/LineFeed/GetTopGamesStatZip"
    )

    def fetch(self):

        events = []

        params = {
            "lng": "en",
            "antisports": "66",
            "partner": "8",
        }

        try:

            data = self.get_json(
                self.API,
                params=params
            )

            for event in data.get(
                "Value",
                []
            ):

                if event.get("SI") != 1:
                    continue

                home = event.get(
                    "O1",
                    ""
                )

                away = event.get(
                    "O2",
                    ""
                )

                if not home or not away:
                    continue

                odds_map = {}

                for item in event.get(
                    "E",
                    []
                ):

                    odd = clean_odd(
                        item.get("C")
                    )

                    if odd:
                        odds_map[
                            (
                                item.get("T"),
                                item.get("P")
                            )
                        ] = odd

                odds = {
                    "1": odds_map.get(
                        (1, None)
                    ),
                    "X": odds_map.get(
                        (2, None)
                    ),
                    "2": odds_map.get(
                        (3, None)
                    ),
                }

                if validate_odds(
                    odds,
                    ("1", "X", "2")
                ):

                    events.append({
                        "bookmaker": self.name,
                        "league": event.get(
                            "L",
                            ""
                        ),
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": odds,
                    })

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

        return events


# ============================================================
# 1XBET
# ============================================================

class OneXBet(BaseBookmaker):

    name = "1xBet"

    API = (
        "https://1x-bet.mobi/"
        "service-api/main-live-feed/v3/"
        "games1x2"
    )

    def fetch(self):

        events = []

        params = {
            "cfView": "3",
            "count": "50",
            "fcountry": "191",
            "gr": "455",
            "grMode": "4",
            "lng": "en",
            "ref": "1",
        }

        try:

            data = self.get_json(
                self.API,
                params=params
            )

            if not isinstance(
                data,
                list
            ):
                return []

            for event in data:

                sport = event.get(
                    "sport",
                    {}
                )

                if sport.get("id") != 1:
                    continue

                home = (
                    event.get(
                        "opponent1",
                        {}
                    ).get(
                        "fullName",
                        ""
                    )
                )

                away = (
                    event.get(
                        "opponent2",
                        {}
                    ).get(
                        "fullName",
                        ""
                    )
                )

                if not home or not away:
                    continue

                odds_map = {}

                for group in event.get(
                    "eventGroups",
                    []
                ):

                    group_id = group.get(
                        "groupId"
                    )

                    for event_list in group.get(
                        "events",
                        []
                    ):

                        if not isinstance(
                            event_list,
                            list
                        ):
                            continue

                        for item in event_list:

                            odd = clean_odd(
                                item.get("cf")
                            )

                            if odd:

                                odds_map[
                                    (
                                        group_id,
                                        item.get("type"),
                                        item.get("parameter")
                                    )
                                ] = odd

                odds = {
                    "1": odds_map.get(
                        (1, 1, None)
                    ),
                    "X": odds_map.get(
                        (1, 2, None)
                    ),
                    "2": odds_map.get(
                        (1, 3, None)
                    ),
                }

                if validate_odds(
                    odds,
                    ("1", "X", "2")
                ):

                    events.append({
                        "bookmaker": self.name,
                        "league": event.get(
                            "liga",
                            {}
                        ).get(
                            "name",
                            ""
                        ),
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": odds,
                    })

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

        return events


# ============================================================
# PARSEBOT
# ============================================================

class ParseBot(BaseBookmaker):

    name = "ParseBot"

    API = (
        "https://api.parse.bot/"
        "scraper/8ffd9f0c-6174-43af-80dc-4898f47f074b/"
        "get_upcoming_events"
    )

    def fetch(self):

        events = []

        params = {
            "page": 1,
            "sport": "football",
            "page_size": 50,
            "market_ids": "1,18",
        }

        try:

            data = self.get_json(
                self.API,
                params=params
            )

            if isinstance(
                data,
                list
            ):
                event_list = data

            elif isinstance(
                data,
                dict
            ):
                event_list = data.get(
                    "data",
                    []
                )

            else:
                event_list = []

            for event in event_list:

                home = (
                    event.get("home_team")
                    or event.get("home")
                    or event.get("homeTeam")
                )

                away = (
                    event.get("away_team")
                    or event.get("away")
                    or event.get("awayTeam")
                )

                league = (
                    event.get("league")
                    or event.get("competition")
                    or ""
                )

                if not home or not away:
                    continue

                odds = {}

                for market in event.get(
                    "markets",
                    []
                ):

                    market_id = market.get(
                        "id"
                    )

                    for outcome in market.get(
                        "outcomes",
                        []
                    ):

                        name = str(
                            outcome.get(
                                "name",
                                ""
                            )
                        ).upper()

                        odd = clean_odd(
                            outcome.get(
                                "odds"
                            )
                        )

                        line = str(
                            outcome.get(
                                "line",
                                ""
                            )
                        )

                        if market_id == 1:

                            if name == "1" and odd:
                                odds["1"] = odd

                            elif name == "X" and odd:
                                odds["X"] = odd

                            elif name == "2" and odd:
                                odds["2"] = odd

                        elif market_id == 18:

                            if (
                                name == "OVER"
                                and line == "1.5"
                                and odd
                            ):
                                odds["over"] = odd

                            elif (
                                name == "UNDER"
                                and line == "1.5"
                                and odd
                            ):
                                odds["under"] = odd

                if validate_odds(
                    odds,
                    ("1", "X", "2")
                ):

                    events.append({
                        "bookmaker": self.name,
                        "league": league,
                        "home": home,
                        "away": away,
                        "market": "1x2",
                        "odds": {
                            "1": odds["1"],
                            "X": odds["X"],
                            "2": odds["2"],
                        },
                    })

                if (
                    "over" in odds
                    and "under" in odds
                ):

                    events.append({
                        "bookmaker": self.name,
                        "league": league,
                        "home": home,
                        "away": away,
                        "market": "over15",
                        "odds": {
                            "over": odds["over"],
                            "under": odds["under"],
                        },
                    })

        except Exception as exc:

            logger.warning(
                "%s error: %s",
                self.name,
                exc
            )

        return events


# ============================================================
# PLACEHOLDERS
# ============================================================

class BongoBongo(BaseBookmaker):

    name = "BongoBongo"

    def fetch(self):
        return []


class BetPawa(BaseBookmaker):

    name = "BetPawa"

    def fetch(self):
        return []


# ============================================================
# EVENT DEDUPLICATION
# ============================================================

def deduplicate_events(events):

    unique = {}

    for event in events:

        if not valid_event(event):
            continue

        market = event.get(
            "market"
        )

        required = {
            "1x2": ("1", "X", "2"),
            "over15": ("over", "under"),
            "btts": ("yes", "no"),
            "dc": ("1X", "12", "X2"),
        }.get(market)

        if not required:
            continue

        if not validate_odds(
            event.get("odds"),
            required
        ):
            continue

        key = (
            event_key(event),
            event.get("bookmaker")
        )

        unique[key] = event

    return list(
        unique.values()
    )


# ============================================================
# ARBITRAGE HELPERS
# ============================================================

def probability_sum(best_odds):

    total = 0.0

    for odd, _ in best_odds.values():

        odd = clean_odd(odd)

        if odd is None:
            return None

        total += 1.0 / odd

    return total


def gross_profit_percent(probability):

    return (
        (1.0 - probability)
        * 100.0
    )


def net_profit(raw_profit):

    return round(
        raw_profit
        * (1.0 - TAX_RATE),
        2
    )


def is_valid_arb(
    best_odds,
    profit_net
):

    if not best_odds:
        return False, "empty odds"

    if profit_net < MIN_PROFIT:
        return False, "profit too low"

    bookmakers = []

    for odd, bookmaker in best_odds.values():

        odd = clean_odd(odd)

        if odd is None:
            return False, "invalid odd"

        # Reject unreasonably high odds (likely API errors).
        if odd > ODD_LIMIT:
            return False, f"odd too high {odd}"

        if not bookmaker:
            return False, "missing bookmaker"

        bookmakers.append(
            bookmaker
        )

    bookmaker_set = set(
        bookmakers
    )

    if len(bookmaker_set) < 2:
        return False, "single bookmaker"

    # Every bookmaker must be explicitly allowed.
    for bookmaker in bookmaker_set:

        if bookmaker not in ALLOWED_BOOKMAKERS:
            return False, (
                f"{bookmaker} not whitelisted"
            )

    # Reject any duplicate bookmaker (no need for sharp-specific rule).
    counts = Counter(
        bookmakers
    )

    if any(c >= 2 for c in counts.values()):
        return False, f"duplicate bookie {counts}"

    probability = probability_sum(
        best_odds
    )

    if probability is None:
        return False, "invalid probability"

    if probability >= PROB_LIMIT:
        return False, "probability sum too high"

    # Reject extremely high profits (probability too low) –
    # likely wrong market or fake odds.
    if probability < MIN_PROB:
        return False, f"probability too low {probability}"

    return True, "OK"


# ============================================================
# STAKE CALCULATION
# ============================================================

def calculate_stakes(best_odds):

    probability = probability_sum(
        best_odds
    )

    if probability is None or probability <= 0:
        return {}

    raw_stakes = {}

    for outcome, (
        odd,
        bookmaker
    ) in best_odds.items():

        raw_stakes[outcome] = (
            TOTAL_STAKE
            * (1.0 / odd)
            / probability
        )

    # Round to UGX 50.
    stakes = {
        outcome: round_to_step(
            amount
        )
        for outcome, amount
        in raw_stakes.items()
    }

    # Correct rounding drift.
    difference = (
        TOTAL_STAKE
        - sum(stakes.values())
    )

    if difference != 0:

        # Adjust the largest stake.
        largest = max(
            stakes,
            key=stakes.get
        )

        adjusted = (
            stakes[largest]
            + difference
        )

        if adjusted >= 0:
            stakes[largest] = adjusted

    return stakes


def calculate_payouts(
    best_odds,
    stakes
):

    payouts = {}

    for outcome, (
        odd,
        bookmaker
    ) in best_odds.items():

        stake = stakes.get(
            outcome,
            0
        )

        payouts[outcome] = round(
            stake * odd,
            2
        )

    return payouts


# ============================================================
# ARBITRAGE DETECTION
# ============================================================

MARKET_CONFIG = {

    "1x2": {
        "outcomes": (
            "1",
            "X",
            "2"
        ),
        "label": "1X2",
    },

    "over15": {
        "outcomes": (
            "over",
            "under"
        ),
        "label": "Over 1.5",
    },

    "btts": {
        "outcomes": (
            "yes",
            "no"
        ),
        "label": "BTTS",
    },

    "dc": {
        "outcomes": (
            "1X",
            "12",
            "X2"
        ),
        "label": "Double Chance",
    },
}


def find_arbs(events):

    events = deduplicate_events(
        events
    )

    grouped = defaultdict(list)

    for event in events:

        key = event_key(event)

        grouped[key].append(
            event
        )

    arbs = []
    rejected = []

    for key, group in grouped.items():

        market = key[2]

        config = MARKET_CONFIG.get(
            market
        )

        if not config:
            continue

        outcomes = config[
            "outcomes"
        ]

        if len(group) < 2:
            continue

        # Best price for every outcome.
        best = {}

        for outcome in outcomes:

            candidates = []

            for event in group:

                odd = clean_odd(
                    event.get(
                        "odds",
                        {}
                    ).get(outcome)
                )

                bookmaker = event.get(
                    "bookmaker"
                )

                if odd and bookmaker:
                    candidates.append(
                        (
                            odd,
                            bookmaker
                        )
                    )

            if not candidates:
                break

            # Highest odd wins.
            best[outcome] = max(
                candidates,
                key=lambda x: x[0]
            )

        if len(best) != len(outcomes):
            continue

        probability = probability_sum(
            best
        )

        if probability is None:
            continue

        if probability >= 1.0:
            continue

        raw_profit = gross_profit_percent(
            probability
        )

        profit = net_profit(
            raw_profit
        )

        valid, reason = is_valid_arb(
            best,
            profit
        )

        if not valid:

            rejected.append({
                "key": key,
                "reason": reason,
            })

            continue

        stakes = calculate_stakes(
            best
        )

        payouts = calculate_payouts(
            best,
            stakes
        )

        actual_total_stake = sum(
            stakes.values()
        )

        min_payout = (
            min(payouts.values())
            if payouts
            else 0
        )

        actual_profit = round(
            (
                min_payout
                - actual_total_stake
            )
            / actual_total_stake
            * 100,
            2
        ) if actual_total_stake else 0

        arbs.append({

            "id": (
                f"{key[0]}_"
                f"{key[1]}_"
                f"{market}"
            ),

            "match": (
                f"{group[0].get('home', '')}"
                f" vs "
                f"{group[0].get('away', '')}"
            ),

            "home": group[0].get(
                "home",
                ""
            ),

            "away": group[0].get(
                "away",
                ""
            ),

            "league": group[0].get(
                "league",
                ""
            ),

            "market": config[
                "label"
            ],

            "market_code": market,

            "profit": profit,

            "raw_profit": round(
                raw_profit,
                2
            ),

            "probability": round(
                probability,
                6
            ),

            "best_odds": best,

            "stakes": stakes,

            "payouts": payouts,

            "total_stake": actual_total_stake,

            "minimum_payout": min_payout,

            "actual_profit_after_rounding": (
                actual_profit
            ),

            "bookmakers": sorted(
                set(
                    bookmaker
                    for _, bookmaker
                    in best.values()
                )
            ),

            "detected_at": utc_now(),
        })

    # Highest profit first.
    arbs.sort(
        key=lambda x: (
            x["profit"],
            x["raw_profit"]
        ),
        reverse=True
    )

    return (
        arbs[:MAX_ARBS],
        rejected
    )


# ============================================================
# HTML DASHBOARD
# ============================================================

def generate_html_dashboard(
    arbs,
    scan_info=None
):

    cards = []

    for arb in arbs:

        odds_rows = []

        for outcome, (
            odd,
            bookmaker
        ) in arb["best_odds"].items():

            stake = arb[
                "stakes"
            ].get(
                outcome,
                0
            )

            payout = arb[
                "payouts"
            ].get(
                outcome,
                0
            )

            odds_rows.append(
                f"""
                <div class="odd-row">
                    <span class="outcome">
                        {html.escape(str(outcome))}
                    </span>

                    <span>
                        <b>{odd:.2f}</b>
                        <small>
                            @ {html.escape(bookmaker)}
                        </small>
                    </span>

                    <span>
                        UGX {stake:,}
                    </span>

                    <span>
                        UGX {payout:,.0f}
                    </span>
                </div>
                """
            )

        cards.append(
            f"""
            <article class="arb-card">

                <div class="top-line">

                    <div>
                        <h2>
                            {html.escape(arb["match"])}
                        </h2>

                        <div class="league">
                            {html.escape(
                                str(arb["league"])
                            )}
                        </div>
                    </div>

                    <div class="profit">
                        +{arb["profit"]:.2f}%
                    </div>

                </div>

                <div class="market">
                    {html.escape(
                        arb["market"]
                    )}
                </div>

                <div class="stats">

                    <div>
                        <small>Total Stake</small>
                        <strong>
                            UGX {arb["total_stake"]:,}
                        </strong>
                    </div>

                    <div>
                        <small>Min Payout</small>
                        <strong>
                            UGX {arb["minimum_payout"]:,.0f}
                        </strong>
                    </div>

                    <div>
                        <small>Arb Probability</small>
                        <strong>
                            {arb["probability"]:.4f}
                        </strong>
                    </div>

                </div>

                <div class="odds-header">
                    <span>Outcome</span>
                    <span>Odd / Bookmaker</span>
                    <span>Stake</span>
                    <span>Payout</span>
                </div>

                {"".join(odds_rows)}

            </article>
            """
        )

    generated = utc_now()

    total_events = (
        scan_info.get(
            "total_events",
            0
        )
        if scan_info
        else 0
    )

    total_books = (
        scan_info.get(
            "bookmakers",
            0
        )
        if scan_info
        else 0
    )

    template = Template(
        """
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<meta
    name="theme-color"
    content="#0b0f14"
>

<title>
    Uganda Arbitrage Scanner
</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    padding: 16px;
    background: #0b0f14;
    color: #e7edf5;
    font-family:
        Arial,
        Helvetica,
        sans-serif;
}

.container {
    width: 100%;
    max-width: 900px;
    margin: auto;
}

header {
    padding: 20px 0;
}

h1 {
    margin: 0 0 8px;
    font-size: 26px;
}

.subtitle {
    color: #8d9aaa;
}

.scan-info {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(130px, 1fr));
    gap: 10px;
    margin: 15px 0 20px;
}

.info {
    background: #111821;
    border: 1px solid #202b37;
    border-radius: 12px;
    padding: 14px;
}

.info small {
    display: block;
    color: #7f8b99;
    margin-bottom: 5px;
}

.info strong {
    font-size: 18px;
}

.arb-card {
    background: #111821;
    border: 1px solid #24303d;
    border-radius: 16px;
    padding: 16px;
    margin-bottom: 14px;
    box-shadow:
        0 8px 30px
        rgba(0,0,0,.18);
}

.top-line {
    display: flex;
    gap: 15px;
    justify-content: space-between;
    align-items: flex-start;
}

h2 {
    margin: 0 0 6px;
    font-size: 18px;
}

.league {
    color: #7f8b99;
    font-size: 13px;
}

.profit {
    color: #42e695;
    font-size: 21px;
    font-weight: 800;
    white-space: nowrap;
}

.market {
    display: inline-block;
    margin: 10px 0;
    padding: 5px 9px;
    border-radius: 7px;
    background: #1b2632;
    color: #b7c3d0;
    font-size: 12px;
}

.stats {
    display: grid;
    grid-template-columns:
        repeat(3, 1fr);
    gap: 8px;
    margin-bottom: 15px;
}

.stats > div {
    background: #0d141c;
    border-radius: 9px;
    padding: 10px;
}

.stats small {
    display: block;
    color: #718091;
    font-size: 10px;
    margin-bottom: 5px;
}

.stats strong {
    font-size: 12px;
}

.odds-header,
.odd-row {
    display: grid;
    grid-template-columns:
        .7fr 1.5fr 1fr 1fr;
    gap: 8px;
    align-items: center;
}

.odds-header {
    color: #667485;
    font-size: 10px;
    padding: 7px 8px;
}

.odd-row {
    padding: 10px 8px;
    border-top: 1px solid #202a35;
    font-size: 12px;
}

.odd-row small {
    display: block;
    color: #758394;
    margin-top: 2px;
}

.outcome {
    font-weight: 700;
}

.empty {
    padding: 40px 20px;
    text-align: center;
    color: #768394;
    background: #111821;
    border-radius: 14px;
}

footer {
    color: #596676;
    font-size: 11px;
    padding: 20px 0;
    text-align: center;
}

@media(max-width:600px) {

    body {
        padding: 10px;
    }

    h1 {
        font-size: 22px;
    }

    .stats {
        grid-template-columns: 1fr;
    }

    .odds-header,
    .odd-row {
        grid-template-columns:
            .7fr 1.4fr 1fr 1fr;
        font-size: 11px;
    }

    .arb-card {
        padding: 13px;
    }

}

</style>

</head>

<body>

<div class="container">

<header>

<h1>
    Uganda Arbitrage Scanner
</h1>

<div class="subtitle">
    Last scan: $generated
</div>

</header>

<div class="scan-info">

<div class="info">
    <small>Opportunities</small>
    <strong>$total_arbs</strong>
</div>

<div class="info">
    <small>Events</small>
    <strong>$total_events</strong>
</div>

<div class="info">
    <small>Bookmakers</small>
    <strong>$total_books</strong>
</div>

</div>

<div>

$arbs

</div>

<footer>

Arbitrage calculations are theoretical.
Odds can change before placement.

</footer>

</div>

</body>

</html>
"""
    )

    if not cards:

        cards_html = """
        <div class="empty">
            No valid arbitrage opportunities found
            in this scan.
        </div>
        """

    else:

        cards_html = "\n".join(
            cards
        )

    output = template.substitute(
        generated=html.escape(
            generated
        ),
        total_arbs=len(arbs),
        total_events=total_events,
        total_books=total_books,
        arbs=cards_html,
    )

    atomic_write(
        HTML_FILE,
        output
    )


# ============================================================
# HISTORY
# ============================================================

def load_json_file(
    path,
    default
):

    try:

        if not os.path.exists(path):
            return default

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        return data

    except Exception as exc:

        logger.warning(
            "Could not load %s: %s",
            path,
            exc
        )

        return default


def save_history(arbs):

    history = load_json_file(
        HISTORY_FILE,
        []
    )

    if not isinstance(
        history,
        list
    ):
        history = []

    history.append({
        "timestamp": utc_now(),
        "count": len(arbs),
        "arbitrage": arbs,
    })

    # Keep last 100 scans.
    history = history[-100:]

    write_json(
        HISTORY_FILE,
        history
    )


# ============================================================
# STATUS
# ============================================================

def save_status(status):

    write_json(
        STATUS_FILE,
        status
    )


# ============================================================
# TELEGRAM FORMAT
# ============================================================

def format_telegram_message(
    arb
):

    lines = [
        "🚨 ARBITRAGE FOUND",
        "",
        f"{arb['match']}",
        f"Market: {arb['market']}",
        f"League: {arb['league']}",
        "",
        f"Net Profit: {arb['profit']:.2f}%",
        f"Stake: UGX {arb['total_stake']:,}",
        "",
    ]

    for outcome, (
        odd,
        bookmaker
    ) in arb[
        "best_odds"
    ].items():

        stake = arb[
            "stakes"
        ].get(
            outcome,
            0
        )

        lines.append(
            f"{outcome}: "
            f"{odd:.2f} @ {bookmaker} "
            f"| Stake UGX {stake:,}"
        )

    return "\n".join(
        lines
    )


# ============================================================
# BOOKMAKERS
# ============================================================

BOOKMAKERS = [

    GSB(),

    TopBet(),

    Bet22(),

    Betmaster(),

    ChampionBet(),

    AbaBet(),

    Fortebet(),

    SportyBet(),

    Melbet(),

    OneXBet(),

    ParseBot(),

    # These currently contain no parser.
    BongoBongo(),

    BetPawa(),
]


# ============================================================
# MAIN SCAN
# ============================================================

def scan_once():

    started_at = utc_now()
    start_time = time.time()

    print()
    print("=" * 70)
    print("UGANDA ARBITRAGE SCANNER")
    print("=" * 70)
    print()

    all_events = []
    bookmaker_results = []

    save_status({
        "status": "running",
        "started_at": started_at,
        "finished_at": None,
        "total_events": 0,
        "arbs": 0,
        "errors": [],
    })

    # --------------------------------------------------------
    # Fetch bookmakers concurrently
    # --------------------------------------------------------

    workers = min(
        MAX_THREADS,
        max(
            1,
            len(BOOKMAKERS)
        )
    )

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:

        futures = {
            executor.submit(
                bookmaker.safe_fetch
            ): bookmaker.name

            for bookmaker
            in BOOKMAKERS
        }

        for future in as_completed(
            futures
        ):

            name = futures[
                future
            ]

            try:

                result = future.result()

            except Exception as exc:

                result = {
                    "bookmaker": name,
                    "events": [],
                    "duration": 0,
                    "error": str(exc),
                }

            bookmaker_results.append(
                result
            )

            count = len(
                result.get(
                    "events",
                    []
                )
            )

            error = result.get(
                "error"
            )

            if error:

                logger.error(
                    "%s: FAILED | %s",
                    name,
                    error
                )

            else:

                logger.info(
                    "%s: %d events | %.2fs",
                    name,
                    count,
                    result.get(
                        "duration",
                        0
                    )
                )

            all_events.extend(
                result.get(
                    "events",
                    []
                )
            )

    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    raw_event_count = len(
        all_events
    )

    all_events = deduplicate_events(
        all_events
    )

    logger.info(
        "Raw events: %d",
        raw_event_count
    )

    logger.info(
        "Valid unique events: %d",
        len(all_events)
    )

    # --------------------------------------------------------
    # Find arbitrage
    # --------------------------------------------------------

    arbs, rejected = find_arbs(
        all_events
    )

    logger.info(
        "Valid arbitrage opportunities: %d",
        len(arbs)
    )

    logger.info(
        "Rejected candidates: %d",
        len(rejected)
    )

    # --------------------------------------------------------
    # Save events
    # --------------------------------------------------------

    write_json(
        EVENTS_FILE,
        all_events
    )

    write_json(
        ARBS_FILE,
        arbs
    )

    # --------------------------------------------------------
    # Scan information
    # --------------------------------------------------------

    errors = [
        {
            "bookmaker": result[
                "bookmaker"
            ],
            "error": result[
                "error"
            ],
        }

        for result
        in bookmaker_results

        if result.get("error")
    ]

    elapsed = round(
        time.time() - start_time,
        2
    )

    status = {
        "status": "complete",

        "started_at": started_at,

        "finished_at": utc_now(),

        "duration_seconds": elapsed,

        "bookmakers": len(
            BOOKMAKERS
        ),

        "successful_bookmakers": (
            len(BOOKMAKERS) - len(errors)
        ),

        "failed_bookmakers": len(
            errors
        ),

        "raw_events": raw_event_count,

        "total_events": len(
            all_events
        ),

        "arbs": len(
            arbs
        ),

        "rejected": len(
            rejected
        ),

        "errors": errors,
    }

    save_status(
        status
    )

    # --------------------------------------------------------
    # Dashboard
    # --------------------------------------------------------

    generate_html_dashboard(
        arbs,
        status
    )

    # --------------------------------------------------------
    # History
    # --------------------------------------------------------

    save_history(
        arbs
    )

    # --------------------------------------------------------
    # Print opportunities
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print(
        f"ARBS FOUND: {len(arbs)}"
    )
    print("=" * 70)

    for arb in arbs:

        print()
        print("-" * 70)

        print(
            f"{arb['match']} "
            f"[{arb['market']}]"
        )

        print(
            f"League: {arb['league']}"
        )

        print(
            f"NET PROFIT: "
            f"{arb['profit']:.2f}%"
        )

        print(
            f"Probability: "
            f"{arb['probability']:.6f}"
        )

        print(
            f"Total stake: "
            f"UGX {arb['total_stake']:,}"
        )

        for outcome, (
            odd,
            bookmaker
        ) in arb[
            "best_odds"
        ].items():

            stake = arb[
                "stakes"
            ].get(
                outcome,
                0
            )

            payout = arb[
                "payouts"
            ].get(
                outcome,
                0
            )

            print(
                f"  {outcome}: "
                f"{odd:.2f} @ {bookmaker} "
                f"| Stake UGX {stake:,} "
                f"| Payout UGX {payout:,.0f}"
            )

        # Telegram
        send_telegram(
            format_telegram_message(
                arb
            )
        )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("SCAN COMPLETE")
    print("=" * 70)

    print(
        f"Raw events:       {raw_event_count}"
    )

    print(
        f"Valid events:     {len(all_events)}"
    )

    print(
        f"Opportunities:    {len(arbs)}"
    )

    print(
        f"Rejected:         {len(rejected)}"
    )

    print(
        f"Duration:         {elapsed}s"
    )

    print()
    print(
        f"Saved: {EVENTS_FILE}"
    )

    print(
        f"Saved: {ARBS_FILE}"
    )

    print(
        f"Saved: {STATUS_FILE}"
    )

    print(
        f"Saved: {HISTORY_FILE}"
    )

    print(
        f"Saved: {HTML_FILE}"
    )

    print()

    return {
        "events": all_events,
        "arbs": arbs,
        "rejected": rejected,
        "status": status,
    }


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        scan_once()

    except KeyboardInterrupt:

        logger.warning(
            "Scanner interrupted by user."
        )

    except Exception:

        logger.exception(
            "FATAL SCANNER ERROR"
        )

        save_status({
            "status": "failed",
            "finished_at": utc_now(),
        })

        raise
