"""Database client and collection handles exported from db_service."""

from app.services.db_service import client, db, initialize_database, ping_database

__all__ = ["client", "db", "initialize_database", "ping_database"]
