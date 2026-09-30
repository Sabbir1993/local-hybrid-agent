import json
import re
import time
from typing import Optional

from ..sqlite_util import transaction
from .common import db


class CustomAgentSlugTaken(ValueError):
    pass


# folders a Personal Agent may never be pointed at: a whole drive, the OS, installed programs
_BAD_DIR_PARTS = tuple("\\" + n for n in ("windows", "program files", "programdata", "system32", "appdata"))


def clean_work_dir(value) -> str:
    """The agent's work folder on the user's machine: '' (none) or a normalised absolute path.
    A drive root, a system folder or a relative path is refused (ValueError)."""
    import ntpath
    raw = str(value or "").strip().strip('"')
    if not raw:
        return ""
    if not (ntpath.isabs(raw) or raw.startswith("/")) or ".." in raw.replace("/", "\\").split("\\"):
        raise ValueError("work folder must be a full path, for example D:\\reports\\weekly")
    path = ntpath.normpath(raw) if ntpath.splitdrive(raw)[0] else raw.rstrip("/") or "/"
    if path in ("/", "") or ntpath.splitdrive(path)[1] in ("\\", ""):
        raise ValueError("pick a folder, not a whole drive")
    low = path.lower()
    if any(part in low for part in _BAD_DIR_PARTS):
        raise ValueError("that is a system or program folder; pick a folder of your own")
    return path


def _custom_agent_row(r) -> dict:
    if not r:
        return {}
    d = dict(r)
    try:
        if d.get("tool_allowlist"):
            d["tool_allowlist"] = json.loads(d["tool_allowlist"])
        else:
            d["tool_allowlist"] = []
    except Exception:
        d["tool_allowlist"] = []
    d["is_public"] = bool(d.get("is_public", 0))
    return d


def db_list_custom_agents(user_id: Optional[int] = None, include_public: bool = True) -> list[dict]:
    """List custom agents accessible to user: user's own agents + system templates / public agents."""
    conn = db()
    if user_id is not None:
        if include_public:
            rows = conn.execute(
                """SELECT * FROM user_custom_agents
                   WHERE user_id = ? OR user_id IS NULL OR is_public = 1
                   ORDER BY (CASE WHEN user_id = ? THEN 0 WHEN user_id IS NULL THEN 1 ELSE 2 END), name COLLATE NOCASE""",
                (user_id, user_id)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM user_custom_agents WHERE user_id = ? ORDER BY name COLLATE NOCASE",
                (user_id,)
            ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM user_custom_agents WHERE user_id IS NULL OR is_public = 1 ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_custom_agent_row(r) for r in rows]


def db_get_custom_agent(agent_id: int, user_id: Optional[int] = None) -> Optional[dict]:
    """Get agent by id, checking access (owner, public, or system template)."""
    conn = db()
    row = conn.execute("SELECT * FROM user_custom_agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return None
    agent = _custom_agent_row(row)
    if user_id is not None:
        if agent.get("user_id") not in (user_id, None) and not agent.get("is_public"):
            return None
    return agent


def db_get_custom_agent_by_slug(slug: str, user_id: Optional[int] = None) -> Optional[dict]:
    """Find a custom agent by slug: the user's own first, then system templates."""
    conn = db()
    s = (slug or "").strip().lower()
    if user_id is not None:
        row = conn.execute(
            "SELECT * FROM user_custom_agents WHERE user_id = ? AND LOWER(slug) = ?",
            (user_id, s)
        ).fetchone()
        if row:
            return _custom_agent_row(row)
    row = conn.execute(
        "SELECT * FROM user_custom_agents WHERE user_id IS NULL AND LOWER(slug) = ?", (s,)
    ).fetchone()
    return _custom_agent_row(row) if row else None


def slugify_custom_agent(text) -> str:
    s = re.sub(r"[^a-z0-9_-]", "-", str(text or "").strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-")


def _slug_taken(conn, user_id: Optional[int], slug: str, exclude_id: Optional[int] = None) -> bool:
    row = conn.execute(
        "SELECT id FROM user_custom_agents WHERE COALESCE(user_id, 0) = ? AND LOWER(slug) = ? AND id != ?",
        (user_id or 0, slug.lower(), exclude_id or 0)).fetchone()
    return row is not None


def _tools_json(tools) -> str:
    return json.dumps([str(t) for t in tools]) if isinstance(tools, list) else "[]"


def db_create_custom_agent(user_id: Optional[int], data: dict) -> dict:
    """Create a new custom agent for a user."""
    conn = db()
    now = time.time()
    name = (data.get("name") or "Custom Agent").strip()
    slug = slugify_custom_agent(data.get("slug") or name) or "agent"
    description = (data.get("description") or "").strip()
    icon = (data.get("icon") or "🤖").strip()
    system_prompt = (data.get("system_prompt") or "").strip()
    tool_json = _tools_json(data.get("tool_allowlist") or [])
    input_template = (data.get("input_template") or "").strip()
    preferred_lane = (data.get("preferred_lane") or "auto").strip()
    reasoning_effort = (data.get("reasoning_effort") or "medium").strip()
    temp = data.get("temperature")
    temperature = float(temp) if temp is not None else 0.4
    # is_public = visible to everyone = approved; a request from someone who cannot approve waits as 'pending'
    share_status = str(data.get("share_status") or "")
    is_public = 1 if share_status == "approved" else 0
    work_dir = clean_work_dir(data.get("work_dir"))

    base_slug = slug
    counter = 1
    while _slug_taken(conn, user_id, slug):
        counter += 1
        slug = f"{base_slug}-{counter}"

    with transaction(conn) as c:
        cursor = c.execute(
            """INSERT INTO user_custom_agents (
                user_id, name, slug, description, icon, system_prompt, tool_allowlist,
                input_template, preferred_lane, reasoning_effort, temperature, is_public, work_dir, share_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, slug, description, icon, system_prompt, tool_json,
             input_template, preferred_lane, reasoning_effort, temperature, is_public, work_dir, share_status,
             now, now)
        )
        new_id = cursor.lastrowid
    return db_get_custom_agent(new_id)


def db_update_custom_agent(agent_id: int, user_id: int, data: dict) -> Optional[dict]:
    """Update an agent owned by user (or admin updating any agent)."""
    conn = db()
    row = conn.execute("SELECT * FROM user_custom_agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return None
    if row["user_id"] != user_id:
        user_row = conn.execute("SELECT is_super_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not (user_row and user_row["is_super_admin"]):
            return None

    share = data.get("share")                       # owner's checkbox: True / False / not sent
    can_approve = bool(data.get("can_approve"))
    data = {k: v for k, v in data.items() if v is not None and k not in ("share", "can_approve", "is_public")}
    if "work_dir" in data:
        data["work_dir"] = clean_work_dir(data["work_dir"])
    now = time.time()
    fields = []
    args = []

    for k in ("name", "description", "icon", "system_prompt", "input_template", "preferred_lane", "reasoning_effort"):
        if k in data:
            fields.append(f"{k} = ?")
            args.append(str(data[k]).strip())
    if "slug" in data:
        slug = slugify_custom_agent(data["slug"])
        if slug and slug != row["slug"]:
            if _slug_taken(conn, row["user_id"], slug, exclude_id=agent_id):
                raise CustomAgentSlugTaken(slug)
            fields.append("slug = ?")
            args.append(slug)
    if "tool_allowlist" in data:
        fields.append("tool_allowlist = ?")
        args.append(_tools_json(data["tool_allowlist"]))
    if "temperature" in data:
        fields.append("temperature = ?")
        args.append(float(data["temperature"]))
    # Sharing: a change to what an approved agent says or does needs approval again, so a reviewed
    # agent cannot be swapped for another after the fact. Editing only the work folder never does.
    changed = any(k in data and str(data[k]).strip() != str(row[k] or "").strip()
                  for k in ("name", "description", "icon", "system_prompt", "input_template",
                            "preferred_lane", "reasoning_effort", "temperature")) or         ("tool_allowlist" in data and _tools_json(data["tool_allowlist"]) != (row["tool_allowlist"] or "[]"))
    if row["user_id"] is not None:
        if share is False:
            new_status = ""
        elif share is True or row["is_public"]:
            keep = bool(row["is_public"]) and not changed
            new_status = "approved" if (can_approve or keep) else "pending"
        else:
            new_status = row["share_status"] or ""
        if new_status != (row["share_status"] or "") or (new_status == "approved") != bool(row["is_public"]):
            fields.append("share_status = ?")
            args.append(new_status)
            fields.append("is_public = ?")
            args.append(1 if new_status == "approved" else 0)
    if "work_dir" in data:
        fields.append("work_dir = ?")
        args.append(data["work_dir"])

    if not fields:
        return _custom_agent_row(row)

    fields.append("updated_at = ?")
    args.append(now)
    args.append(agent_id)

    with transaction(conn) as c:
        c.execute(f"UPDATE user_custom_agents SET {', '.join(fields)} WHERE id = ?", args)

    return db_get_custom_agent(agent_id)


def db_list_pending_agents() -> list[dict]:
    """Agents waiting for someone with the publish permission to approve sharing them, with the owner's name."""
    rows = db().execute(
        """SELECT a.*, u.username AS owner_name FROM user_custom_agents a
           LEFT JOIN users u ON u.id = a.user_id
           WHERE a.share_status = 'pending' AND a.user_id IS NOT NULL ORDER BY a.updated_at""").fetchall()
    return [_custom_agent_row(r) for r in rows]


def db_set_share_status(agent_id: int, approved: bool) -> Optional[dict]:
    """Approve (visible to all) or reject (back to private, marked rejected) a pending agent."""
    conn = db()
    row = conn.execute("SELECT * FROM user_custom_agents WHERE id = ? AND share_status = 'pending'",
                       (agent_id,)).fetchone()
    if not row:
        return None
    with transaction(conn) as c:
        c.execute("UPDATE user_custom_agents SET share_status = ?, is_public = ?, updated_at = updated_at WHERE id = ?",
                  ("approved" if approved else "rejected", 1 if approved else 0, agent_id))
    return db_get_custom_agent(agent_id)


def db_delete_custom_agent(agent_id: int, user_id: int) -> bool:
    """Delete an agent owned by user (or by super-admin)."""
    conn = db()
    row = conn.execute("SELECT * FROM user_custom_agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return False
    if row["user_id"] != user_id:
        user_row = conn.execute("SELECT is_super_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not (user_row and user_row["is_super_admin"]):
            return False

    with transaction(conn) as c:
        c.execute("DELETE FROM user_custom_agents WHERE id = ?", (agent_id,))
    return True


def db_fork_custom_agent(agent_id: int, user_id: int, new_name: Optional[str] = None,
                         work_dir: str = "") -> Optional[dict]:
    """Clone an existing agent / template into user's own collection."""
    source = db_get_custom_agent(agent_id, user_id)
    if not source:
        return None
    base_name = new_name or f"{source['name']} (Fork)"
    base_slug = f"{source['slug']}-copy"
    slug = base_slug
    counter = 1
    conn = db()
    while _slug_taken(conn, user_id, slug):
        counter += 1
        slug = f"{base_slug}-{counter}"

    data = {
        "name": base_name,
        "slug": slug,
        "description": source["description"],
        "icon": source["icon"],
        "system_prompt": source["system_prompt"],
        "tool_allowlist": source["tool_allowlist"],
        "input_template": source["input_template"],
        "preferred_lane": source["preferred_lane"],
        "reasoning_effort": source["reasoning_effort"],
        "temperature": source["temperature"],
        "work_dir": work_dir,          # never copied from the source: a path on someone else's machine
    }
    return db_create_custom_agent(user_id, data)
