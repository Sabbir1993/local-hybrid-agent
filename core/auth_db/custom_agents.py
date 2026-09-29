import json
import re
import time
from typing import Optional

from ..sqlite_util import transaction
from .common import db


class CustomAgentSlugTaken(ValueError):
    pass


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
    is_public = 1 if data.get("is_public") else 0

    base_slug = slug
    counter = 1
    while _slug_taken(conn, user_id, slug):
        counter += 1
        slug = f"{base_slug}-{counter}"

    with transaction(conn) as c:
        cursor = c.execute(
            """INSERT INTO user_custom_agents (
                user_id, name, slug, description, icon, system_prompt, tool_allowlist,
                input_template, preferred_lane, reasoning_effort, temperature, is_public,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, slug, description, icon, system_prompt, tool_json,
             input_template, preferred_lane, reasoning_effort, temperature, is_public,
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

    data = {k: v for k, v in data.items() if v is not None}
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
    if "is_public" in data:
        fields.append("is_public = ?")
        args.append(1 if data["is_public"] else 0)

    if not fields:
        return _custom_agent_row(row)

    fields.append("updated_at = ?")
    args.append(now)
    args.append(agent_id)

    with transaction(conn) as c:
        c.execute(f"UPDATE user_custom_agents SET {', '.join(fields)} WHERE id = ?", args)

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


def db_fork_custom_agent(agent_id: int, user_id: int, new_name: Optional[str] = None) -> Optional[dict]:
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
        "is_public": 0,
    }
    return db_create_custom_agent(user_id, data)
