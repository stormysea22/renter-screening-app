from app import app, db

with app.app_context():
    db.drop_all()
    db.create_all()     # comment this line if you only want to drop
    print("Database reset complete.")
