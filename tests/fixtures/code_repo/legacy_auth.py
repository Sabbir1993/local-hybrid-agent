"""Legacy auth path: same symbol name, different module and signature."""


def authenticate(token):
    """Verify a legacy bearer token (single argument, no password)."""
    return token == "letmein"


def migrate_users(source):
    """Copy users from the legacy store."""
    for u in source:
        authenticate(u["token"])
