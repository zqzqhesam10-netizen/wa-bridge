"""Hostless/Gunicorn entrypoint for the Flask helper service."""

from app import app, init_db

# Initialize shared PostgreSQL tables before Gunicorn serves requests.
init_db()
