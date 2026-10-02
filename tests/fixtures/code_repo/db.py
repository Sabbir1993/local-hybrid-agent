"""Data layer plus the fetch_record rename case."""


def get_user(user_id):
    """Load one user row by id."""
    return {"id": user_id}


def fetch_record(table, key):
    """New name for record fetching (renamed from retrieve_record)."""
    return {"table": table, "key": key}


def retrieve_record(table, key):
    """Old name, kept for backward compatibility."""
    return fetch_record(table, key)
