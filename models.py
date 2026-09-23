from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    host_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # State tracking
    current_media_type = db.Column(db.String(20), default='youtube') # 'youtube', 'movie'
    current_media_url = db.Column(db.String(500), nullable=True) # YT ID or magnet link
    
    # Room lifecycle — tracks last meaningful activity for auto-expiration
    last_activity = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Privacy — private rooms require a join secret to enter
    is_private = db.Column(db.Boolean, default=False)
    join_secret_hash = db.Column(db.String(256), nullable=True)
    
    # Locking — host can lock the room to prevent new members
    is_locked = db.Column(db.Boolean, default=False)
    
    host = db.relationship('User', backref=db.backref('hosted_rooms', lazy=True))
