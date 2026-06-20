import json
from datetime import datetime
from playwright.sync_api import sync_playwright
import re
import urllib.request

def scrape_betpawa():
    odds = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-blink-features=AutomationControlled'])
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Linux; Android 12; Samsung Galaxy) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
                viewport={'width': 390, 'height': 844},
                locale='en-UG'
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            print("Opening BetPawa...")
            page.goto('https://www.betpawa.ug/events?categoryId=2&marketId=1X2', timeout=60000)
            page.wait_for_timeout(8000)
            html = page.content()
            print(f"BetPawa loaded: {len(html)} bytes")
            links = page.query_selector_all('a[href*="/event/"], a[href*="/match/"]')
            print(f"BetPawa: found {len(links)} links")
            skip = ['pm','am','Sat','Sun','Mon','Tue','Wed','Thu','Fri','Full Time','Half','1UP','2UP','1X2','Double','Both','Over','Under','Total','Score','Chance','Teams','Interval','minutes','First']
            for link in links[:60]:
                try:
                    text = link.inner_text()
                    parts = [p.strip() for p in text.split('\n') if p.strip()]
                    teams = []
                    odd_values = []
                    competition = ''
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
                        odds.append({'match': f"{teams[0]} vs {teams[1]}",'home_team': teams[0],'away_team': teams[1],'bookmaker': 'BetPawa','competition': competition,'home': odd_values[0],'draw': odd_values[1],'away': odd_values[2],'sport': 'Football'})
                except:
                    continue
            browser.close()
            print(f"BetPawa: {len(odds)} matches extracted")
    except Exception as e:
        print(f"BetPawa error: {e}")
    return odds

def scrape_fortebet():
    odds = []
    try:
        print("Fetching Fortebet API...")
        url = 'https://desktop.fortebet.ug/api/web/v1/offer/full-prematch-en'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Referer': 'https://desktop.fortebet.ug/prematch/landing'
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        inner = data.get('data', {})
        print(f"Fortebet data keys: {list(inner.keys()) if isinstance(inner, dict) else type(inner)}")
        if isinstance(inner, dict):
            for key, val in inner.items():
                print(f"  Key: {key} -> type: {type(val)}, len: {len(val) if hasattr(val,'__len__') else 'N/A'}")
        print(f"Fortebet: {len(odds)} matches extracted")
    except Exception as e:
        print(f"Fortebet error: {e}")
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
        if len(bookmakers) < 2:
            continue
        best_home = max(bookmakers, key=lambda x: x.get('home') or 0)
        best_draw = max(bookmakers, key=lambda x: x.get('draw') or 0)
        best_away = max(bookmakers, key=lambda x: x.get('away') or 0)
        h = best_home.get('home', 0)
        d = best_draw.get('draw', 0)
        a = best_away.get('away', 0)
        if not h or not a:
            continue
        arb2 = (1/h)+(1/a)
        if arb2 < 1:
            profit = round((1-arb2)*100, 2)
            opportunities.append({'match': match_name,'type': '2-way','profit_percent': profit,'bets': [{'bookmaker': best_home['bookmaker'],'outcome':'Home','odd': h,'stake': round(STAKE*(1/h)/arb2)},{'bookmaker': best_away['bookmaker'],'outcome':'Away','odd': a,'stake': round(STAKE*(1/a)/arb2)}]})
        if d:
            arb3 = (1/h)+(1/d)+(1/a)
            if arb3 < 1:
                profit = round((1-arb3)*100, 2)
                opportunities.append({'match': match_name,'type': '3-way','profit_percent': profit,'bets': [{'bookmaker': best_home['bookmaker'],'outcome':'Home','odd': h,'stake': round(STAKE*(1/h)/arb3)},{'bookmaker': best_draw['bookmaker'],'outcome':'Draw','odd': d,'stake': round(STAKE*(1/d)/arb3)},{'bookmaker': best_away['bookmaker'],'outcome':'Away','odd': a,'stake': round(STAKE*(1/a)/arb3)}]})
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

def main():
    print(f"Scraper started: {datetime.utcnow()}")
    all_odds = []
    scraped = []
    print("Scraping BetPawa...")
    bp = scrape_betpawa()
    all_odds.extend(bp)
    if bp:
        scraped.append('BetPawa')
    print("Scraping Fortebet...")
    fb = scrape_fortebet()
    all_odds.extend(fb)
    if fb:
        scraped.append('Fortebet')
    opportunities = find_arbitrage(all_odds)
    print(f"Found {len(opportunities)} arbitrage opportunities")
    output = {'last_updated': datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),'total_matches': len(all_odds),'bookmakers_scraped': scraped,'opportunities': opportunities,'raw_odds': all_odds}
    with open('odds.json', 'w') as f:
        json.dump(output, f, indent=2)
    print(f"Done! {len(all_odds)} matches saved")

if __name__ == '__main__':
    main()
