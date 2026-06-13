import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime
import cloudscraper
import time

BOOKMAKERS = [
    {'name': 'BetPawa', 'url': 'https://www.betpawa.ug'},
    {'name': 'Fortebet', 'url': 'https://www.fortebet.ug'},
    {'name': 'Betway', 'url': 'https://www.betway.co.ug'},
    {'name': 'Gal Sports', 'url': 'https://www.galsportsbetting.com'},
    {'name': 'Elitebet', 'url': 'https://www.elitebet.ug'},
]

def scrape_site(bookmaker):
    odds = []
    try:
        scraper = cloudscraper.create_scraper()
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = scraper.get(
            bookmaker['url'],
            headers=headers,
            timeout=30
        )
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Log what we got
        print(f"{bookmaker['name']}: status {response.status_code}, {len(response.text)} bytes")
        
        # Save raw HTML for debugging
        with open(f"debug_{bookmaker['name'].replace(' ','_')}.html", 'w') as f:
            f.write(response.text[:5000])
            
    except Exception as e:
        print(f"{bookmaker['name']} error: {e}")
    return odds

def main():
    print("Scraper started:", datetime.utcnow())
    all_odds = []
    scraped = []

    for bm in BOOKMAKERS:
        print(f"Trying {bm['name']}...")
        odds = scrape_site(bm)
        all_odds.extend(odds)
        scraped.append(bm['name'])
        time.sleep(2)

    output = {
        'last_updated': datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
        'total_matches': len(all_odds),
        'bookmakers_scraped': scraped,
        'raw_odds': all_odds
    }

    with open('odds.json', 'w') as f:
        json.dump(output, f, indent=2)

    pprint("Done! Saved odds.json!")
