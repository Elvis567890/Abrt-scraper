# run_scanner.py
# This file is called by Railway Cron to scrape odds in the background

from scraper import run_scan

if __name__ == "__main__":
    print("🚀 Starting manual arbitrage scan via Cron...")
    run_scan()
    print("✅ Scan finished.")
