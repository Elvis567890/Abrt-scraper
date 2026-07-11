# test_telegram.py
import requests

# Your bot token
TOKEN = "8288004781:AAH6EAKt7mPajvALW1KHKWAnSCvADkGQMyo"
CHAT_ID = "768354055"  # Your chat ID from earlier

def test_bot():
    print("🚀 Testing Telegram Bot...")
    
    # Send a test message
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": "✅ **Test Message!** \n\nYour Telegram bot is working correctly! 🎉",
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()
        
        if result.get('ok'):
            print("✅ SUCCESS!")
            print("📱 Check your Telegram: @afrinodebot")
            print(f"📨 Message sent: {result['result']['message_id']}")
        else:
            print("❌ Failed:", result)
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    test_bot()
