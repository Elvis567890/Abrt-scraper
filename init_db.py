# init_db.py
from scraper import app, db, create_admin_user

with app.app_context():
    db.create_all()
    create_admin_user()
