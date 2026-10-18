"""One-off: create the dedicated non-MFA benchmark account described in
scripts/bench_chat_files.py ("a dedicated non-MFA account; never put them on
the command line"). The old companion/.env BENCH_ credentials pointed at the
owner's personal super-admin account with a stale password - wrong by design
and broken in practice (6 denied logins in the audit log).

The account is scoped to exactly what benchmarking needs:
  chat.use          - run agent tasks
  model.local.load  - switch the GGUF the shared llama-server serves
It gets NO other permission and is not a super-admin. The password is random,
written once to companion/.env (never to the console), and rotating it on
every run is safe because it is not a person.

Run:  python scripts/create_eval_user.py
"""
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.auth_db as auth_db
from core.auth_provider import hash_password

USERNAME = "eval"
ENV_FILE = ROOT / "companion" / ".env"
BENCH_KEYS = ("chat.use", "model.local.load")


def main():
    auth_db._init_auth_db()   # ensure schema exists even before the app boots
    con = auth_db.db()

    uid = None
    row = con.execute("SELECT id FROM users WHERE username = ?", (USERNAME,)).fetchone()
    password = "Ev-" + secrets.token_urlsafe(15)
    if row:
        uid = row["id"]
        con.execute("UPDATE users SET password_hash = ?, is_active = 1, "
                    "failed_login_count = 0, locked_until = NULL, must_change_password = 0 "
                    "WHERE id = ?", (hash_password(password), uid))
        print(f"user '{USERNAME}' exists (id {uid}) - password rotated")
    else:
        uid = auth_db.create_user(USERNAME, hash_password(password),
                                  display_name="Benchmark account")
        print(f"user '{USERNAME}' created (id {uid})")

    if not auth_db.get_user_role_names(uid):
        auth_db.assign_role(uid, "user")   # chat.use comes with the default role
        print("assigned builtin role 'user'")

    # a bench-scoped role with exactly the two keys this account needs
    con.execute("INSERT INTO roles (name, description, is_builtin, created_at) "
                "VALUES ('bench', 'Benchmark service account scope', 0, ?) "
                "ON CONFLICT(name) DO NOTHING", (time.time(),))
    con.commit()
    rid = con.execute("SELECT id FROM roles WHERE name = 'bench'").fetchone()["id"]
    for key in BENCH_KEYS:
        con.execute(
            "INSERT INTO role_permissions (role_id, permission_id, granted_at, granted_by) "
            "SELECT ?, id, ?, NULL FROM permissions WHERE key = ? "
            "ON CONFLICT(role_id, permission_id) DO NOTHING", (rid, time.time(), key))
    con.execute(
        "INSERT INTO user_roles (user_id, role_id, assigned_at, assigned_by) "
        "SELECT ?, ?, ?, NULL WHERE NOT EXISTS "
        "(SELECT 1 FROM user_roles WHERE user_id = ? AND role_id = ?)",
        (uid, rid, time.time(), uid, rid))
    con.commit()

    granted = sorted(auth_db.get_user_permission_keys(uid) & set(BENCH_KEYS))
    print(f"bench scope granted: {granted}")

    if not ENV_FILE.exists():
        print(f"{ENV_FILE} missing - create it with A770_SERVER_URL first", file=sys.stderr)
        sys.exit(1)
    lines, seen = [], set()
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        k = line.split("=", 1)[0] if "=" in line else ""
        if k == "BENCH_USER":
            lines.append(f'BENCH_USER="{USERNAME}"'); seen.add("user")
        elif k == "BENCH_PASSWORD":
            lines.append(f'BENCH_PASSWORD="{password}"'); seen.add("pass")
        else:
            lines.append(line)
    if "user" not in seen:
        lines.append(f'BENCH_USER="{USERNAME}"')
    if "pass" not in seen:
        lines.append(f'BENCH_PASSWORD="{password}"')
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{ENV_FILE} updated - BENCH_USER now '{USERNAME}', password rotated (not printed)")


if __name__ == "__main__":
    main()
