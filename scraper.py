import json
from datetime import datetime
from playwright.sync_api import sync_playwright
import re

def scrape_site(name, url):
    odds = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent='Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36'
            )
            print(f"Opening {name}...")
            page.goto(url, timeout=60000)
            page.wait_for_timeout(6000)
            html = page.content()
            print(f"{name} page loaded: {len(html)} bytes")
            links = page.query_selector_all('a[href*="/event/"]')
            print(f"{name}: found {len(links)} event links")
            for link in links[:60]:
                try:
                    text = link.inner_text()
                    parts = [p.strip() for p in text.split('\n') if p.strip()]
                    teams = []
                    odd_values = []
                    competition = ''
                    skip = ['pm','am','Sat','Sun','Mon','Tue','Wed','Thu','Fri',
                            'Full Time','Half','1UP','2UP','1X2','Double','Both',
                            'Over','Under','Total','Score','Chance','Teams',
                            'Interval','minutes','First']
                    for part in parts:
                        if re.match(r'^\d+\.\d+$', part):
                            odd_values.append(float(part))
                        elif any(s in part for s in ['Football','Soccer']):
                            competition = part
                        elif part in ['1','X','2','1X','X2','12']:
                            continue
                        elif any(s in part for s in skip):
                            continue
                        elif re.match(r'^\d+:\d+', part):
                            continue
                        elif re.match(r'^\d+/\d+', part):
                            continue
                        elif len(part) > 2:
                            teams.append(part)
                    if len(teams) >= 2 and len(odd_values) >= 3:
                        odds.append({
                            'match': f"{teams[0]} vs {teams[1]}",
                            'home_team': teams[0],
                            'away_team': teams[1],
                            'bookmaker': name,
                            'competition': competition,
                            'home': odd_values[0],
                            'draw': odd_values[1],
                            'away': odd_values[2],
                            'sport': 'Football'
                        })
                except:
                    continue
            browser.close()
            print(f"{name}: {len(odds)} matches extracted")
    except Exception as e:
        print(f"{name} error: {e}")
    return odds

def find_arbitrage(all_odds):
    opportunities = []
    STAKE = 100000
    matches = {}
    for odd in all_odds:
        key = odd['match'].lower().strip()
        if key not in matches:
            matches[key] = []
        matches[key].append(odd)
    for match_name, bookmakers in matches.items():
