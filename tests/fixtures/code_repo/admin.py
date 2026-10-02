"""Admin panel: third call site for get_user."""
from db import get_user


def admin_lookup(user_id):
    return get_user(user_id)
