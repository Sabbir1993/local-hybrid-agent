"""Route handlers: two call sites for get_user, one for fetch_record."""
from db import fetch_record, get_user


def user_profile(user_id):
    user = get_user(user_id)
    return {"profile": user}


def user_settings(user_id):
    user = get_user(user_id)
    return {"settings": user}


def record_view(table, key):
    return fetch_record(table, key)
