from scrapper import app, db, create_admin_user  # double 'p'
with app.app_context():
    db.create_all()
    create_admin_user()
