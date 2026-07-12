"""
AI Bookmaker Scraper Manager
Add any bookmaker and AI generates a scraper for it
"""

import os
import json
import re
import time
import requests
from datetime import datetime
from typing import List, Dict, Optional

# ============================================================
# CONFIGURATION MANAGER
# ============================================================

class BookmakerConfig:
    """Manages bookmaker configurations"""
    
    def __init__(self, config_file="bookmakers.json"):
        self.config_file = config_file
        self.config = self.load()
    
    def load(self) -> Dict:
        """Load bookmaker config"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except:
                return {"bookmakers": []}
        return {"bookmakers": []}
    
    def save(self):
        """Save bookmaker config"""
        with open(self.config_file, 'w') as f:
            json.dump(self.config, f, indent=2)
        print(f"✅ Saved to {self.config_file}")
    
    def add(self, name: str, url: str):
        """Add a new bookmaker"""
        # Check if exists
        for b in self.config["bookmakers"]:
            if b["name"].lower() == name.lower():
                print(f"⚠️ {name} already exists")
                return False
        
        # Add new
        self.config["bookmakers"].append({
            "name": name,
            "url": url,
            "enabled": True,
            "added": datetime.now().isoformat()
        })
        self.save()
        print(f"✅ Added {name}")
        return True
    
    def remove(self, name: str):
        """Remove a bookmaker"""
        self.config["bookmakers"] = [
            b for b in self.config["bookmakers"] 
            if b["name"].lower() != name.lower()
        ]
        self.save()
        print(f"✅ Removed {name}")
    
    def get_all(self) -> List[Dict]:
        """Get all bookmakers"""
        return self.config.get("bookmakers", [])
    
    def get_enabled(self) -> List[Dict]:
        """Get enabled bookmakers only"""
        return [b for b in self.config.get("bookmakers", []) if b.get("enabled", True)]


# ============================================================
# AI SCRAPER GENERATOR
# ============================================================

class AIScraperGenerator:
    """Generates scrapers using Gemini AI"""
    
    def __init__(self):
        self.client = None
        self._init_gemini()
    
    def _init_gemini(self):
        """Initialize Gemini client"""
        try:
            from google import genai
            from dotenv import load_dotenv
            load_dotenv()
            
            API_KEY = os.getenv("GEMINI_API_KEY")
            if API_KEY:
                self.client = genai.Client(api_key=API_KEY)
                print("✅ Gemini AI initialized")
            else:
                print("❌ GEMINI_API_KEY not found in .env")
                print("Please create .env file with: GEMINI_API_KEY=your_key")
        except Exception as e:
            print(f"❌ Gemini init error: {e}")
    
    def generate(self, bookmaker: Dict) -> Optional[str]:
        """Generate a scraper for a bookmaker"""
        
        if not self.client:
            print("❌ Gemini not available")
            return None
        
        name = bookmaker["name"]
        url = bookmaker["url"]
        
        print(f"\n🤖 Generating scraper for {name}...")
        
        try:
            # Fetch website
            print(f"  📡 Fetching {url}...")
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code != 200:
                print(f"  ⚠️ Website returned status: {response.status_code}")
            
            html = response.text[:40000]  # Limit size
            
            # AI Prompt
            prompt = f"""
Create a Python scraper for {name} betting website.

Website: {url}

Return ONLY the Python code, no explanations.

Requirements:
1. Function: scrape_{name.lower().replace(' ', '_')}()
2. Returns: List of dicts with keys: home_team, away_team, home_odd, draw_odd, away_odd, competition
3. Use requests library
4. Handle errors with try/except
5. Return empty list if nothing found
6. Skip live matches
7. Only football/soccer matches
8. Skip finished matches
9. Use BeautifulSoup if needed
10. All odds should be decimal format

Example format:
```python
import requests
from bs4 import BeautifulSoup

def scrape_{name.lower().replace(' ', '_')}():
    odds = []
    try:
        # Your scraping code here
        pass
    except Exception as e:
        print(f"Error: {{e}}")
    return odds
