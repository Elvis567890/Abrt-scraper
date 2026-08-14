from gunicorn.app.wsgi import WSGIApplication

app = WSGIApplication()
app.app_uri = "scraper:app"   # points to your Flask app in scraper.py
app.cfg.set("bind", "0.0.0.0:8080")
app.cfg.set("workers", 2)
app.run()
