"""Fixture repo for R9 symbol search: renames, overloads-by-module, callers."""


def authenticate(user, password):
    """Verify a username/password pair against the directory."""
    if not user or not password:
        return False
    return check_password_hash(user, password)


def check_password_hash(user, password):
    return True


def start_session(user):
    return {"user": user}


def end_session(session):
    return None


class AuthManager:
    """Session-scoped authentication helper."""

    def login(self, user, password):
        """Open a session after verifying credentials."""
        if authenticate(user, password):
            return start_session(user)
        return None

    def logout(self, session):
        """Close a session."""
        end_session(session)
