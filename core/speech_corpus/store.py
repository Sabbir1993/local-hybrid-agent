"""corpus.db and the audio folder. The schema below is what the separate training application reads.

recordings.prompt_text is the label (a copy of the line as shown); recordings.instruction is 'read' (standard speaker
read it as written) or 'dialect' (said in their own dialect, label stays standard Bangla); status is 'ok', 'flagged'
or 'deleted'. Names stay in contributors; use speaker_code in training outputs.

Layout under the corpus folder:
  corpus.db                                   this database (WAL)
  audio/<name>_<employee_id>/<recording>.wav  one folder per speaker, one file per accepted clip
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path

from ..sqlite_util import ThreadLocalDB

SCHEMA_VERSION = 1
SESSION_TTL_S = 6 * 3600
UPLOADS_PER_LINE = 3            # a line may be re-recorded, within reason

REGIONS = {
    "standard": "Standard Bangla",
    "ctg_noakhali": "Chattogram / Noakhali",
    "sylhet": "Sylhet",
    "barishal_khulna": "Barishal / Khulna",
    "rangpur_mymensingh_dhaka": "Rangpur / Mymensingh / Dhaka",
}
GENDERS = {"", "female", "male", "other"}
AGE_BANDS = {"", "18-24", "25-34", "35-44", "45-54", "55+"}
DEVICES = {"", "laptop", "headset", "phone", "desktop"}
CONSENT_VERSION = "2026-11-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contributors (
    id INTEGER PRIMARY KEY,
    speaker_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    employee_id TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL UNIQUE,
    region TEXT NOT NULL,
    district TEXT NOT NULL DEFAULT '',
    gender TEXT NOT NULL DEFAULT '',
    age_band TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    consent_version TEXT NOT NULL,
    consented_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    contributor_id INTEGER NOT NULL REFERENCES contributors(id),
    token_sha256 TEXT NOT NULL UNIQUE,
    prompt_ids TEXT NOT NULL,
    uploads INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    contributor_id INTEGER NOT NULL REFERENCES contributors(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    prompt_id INTEGER NOT NULL REFERENCES prompts(id),
    prompt_text TEXT NOT NULL,
    category TEXT NOT NULL,
    instruction TEXT NOT NULL,
    path TEXT NOT NULL,
    seconds REAL NOT NULL,
    rms REAL NOT NULL,
    clipped_ratio REAL NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    UNIQUE (session_id, prompt_id)
);
CREATE INDEX IF NOT EXISTS ix_recordings_prompt ON recordings(prompt_id, status);
CREATE INDEX IF NOT EXISTS ix_recordings_contributor ON recordings(contributor_id);
CREATE INDEX IF NOT EXISTS ix_sessions_contributor ON sessions(contributor_id, started_at);
"""


class CorpusError(Exception):
    """A request the store refuses; `status` is the HTTP code the route should answer with."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _clean_text(s, limit: int) -> str:
    s = unicodedata.normalize("NFC", str(s or ""))
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")      # no control / format characters
    return re.sub(r"\s+", " ", s).strip()[:limit]


def clean_employee_id(s) -> str:
    v = str(s or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,31}", v):
        raise CorpusError("Enter your employee ID (letters, digits, - or _, up to 32 characters).")
    return v


def _slug(s: str) -> str:
    """Letters (any script, with their vowel signs), digits and '-' only."""
    out = []
    for ch in unicodedata.normalize("NFC", s):
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "M", "N"):
            out.append(ch)
        else:                                  # spaces, dots, slashes, anything else: a separator, never a path part
            out.append("-")
    return re.sub(r"-{2,}", "-", "".join(out)).strip("-")


def speaker_folder(name: str, employee_id: str) -> str:
    """'<name>_<employee_id>', safe as a single path part on Linux and Windows, at most 60 characters."""
    emp = _slug(employee_id)[:32] or "ID"
    nm = _slug(name)[: max(1, 59 - len(emp))].strip("-") or "speaker"
    return f"{nm}_{emp}"


class CorpusStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.audio = self.root / "audio"
        self.audio.mkdir(mode=0o700, exist_ok=True)
        self.db = ThreadLocalDB(self.root / "corpus.db", row_factory=sqlite3.Row)
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_SCHEMA)
            self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (str(time.time()),))
            self.db.commit()

    # ---- contributors and sessions ----
    def upsert_contributor(self, *, name, employee_id, region, district="", gender="", age_band="", device="") -> sqlite3.Row:
        nm = _clean_text(name, 80)
        if len(nm) < 2:
            raise CorpusError("Enter your name.")
        emp = clean_employee_id(employee_id)
        if region not in REGIONS:
            raise CorpusError("Choose how you speak at home.")
        gender = gender if gender in GENDERS else ""
        age_band = age_band if age_band in AGE_BANDS else ""
        device = device if device in DEVICES else ""
        district = _clean_text(district, 40)
        now = time.time()
        with self._lock:
            row = self.db.execute("SELECT * FROM contributors WHERE employee_id = ?", (emp,)).fetchone()
            if row:
                # the folder from the first session is kept, even if the name is typed differently now
                self.db.execute(
                    "UPDATE contributors SET region=?, district=?, gender=?, age_band=?, device=?, consent_version=?, "
                    "consented_at=? WHERE id=?",
                    (region, district, gender, age_band, device, CONSENT_VERSION, now, row["id"]))
            else:
                # a counter in meta, never MAX(id): a deleted speaker's code is never handed out again
                got = self.db.execute("SELECT value FROM meta WHERE key = 'next_speaker'").fetchone()
                n = int(got["value"]) if got else 1
                self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('next_speaker', ?)", (str(n + 1),))
                code = f"S{n:04d}"
                folder = speaker_folder(nm, emp)
                if self.db.execute("SELECT 1 FROM contributors WHERE folder = ?", (folder,)).fetchone():
                    folder = f"{folder[:54]}_{code}"
                self.db.execute(
                    "INSERT INTO contributors (speaker_code, name, employee_id, folder, region, district, gender, age_band, "
                    "device, consent_version, consented_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, nm, emp, folder, region, district, gender, age_band, device, CONSENT_VERSION, now, now))
            self.db.commit()
            return self.db.execute("SELECT * FROM contributors WHERE employee_id = ?", (emp,)).fetchone()

    def sessions_today(self, contributor_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM sessions WHERE contributor_id = ? AND started_at > ?",
                               (contributor_id, time.time() - 86400)).fetchone()[0]

    def seen_prompt_ids(self, contributor_id: int) -> set:
        seen = set()
        for r in self.db.execute("SELECT prompt_ids FROM sessions WHERE contributor_id = ?", (contributor_id,)):
            try:
                seen.update(int(x) for x in json.loads(r["prompt_ids"]))
            except (ValueError, TypeError):
                pass
        return seen

    def open_session(self, contributor_id: int, prompt_ids: list) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self.db.execute("INSERT INTO sessions (contributor_id, token_sha256, prompt_ids, started_at) VALUES (?,?,?,?)",
                            (contributor_id, _token_hash(token), json.dumps(prompt_ids), time.time()))
            self.db.commit()
        return token

    def session_for(self, token: str) -> sqlite3.Row:
        if not token or len(token) > 100:
            raise CorpusError("Your session has ended. Reload the page to start again.", 401)
        row = self.db.execute(
            "SELECT s.*, c.folder, c.region FROM sessions s JOIN contributors c ON c.id = s.contributor_id "
            "WHERE s.token_sha256 = ?", (_token_hash(token),)).fetchone()
        if not row or row["finished_at"] or time.time() - row["started_at"] > SESSION_TTL_S:
            raise CorpusError("Your session has ended. Reload the page to start again.", 401)
        return row

    def finish_session(self, token: str) -> dict:
        s = self.session_for(token)
        with self._lock:
            self.db.execute("UPDATE sessions SET finished_at = ? WHERE id = ?", (time.time(), s["id"]))
            self.db.commit()
        done = self.db.execute("SELECT COUNT(*) FROM recordings WHERE session_id = ? AND status = 'ok'",
                               (s["id"],)).fetchone()[0]
        return {"recorded": done}

    # ---- recordings ----
    def save_recording(self, token: str, prompt_id: int, wav: bytes, info: dict) -> dict:
        s = self.session_for(token)
        allowed = [int(x) for x in json.loads(s["prompt_ids"])]
        if prompt_id not in allowed:
            raise CorpusError("That line is not part of your session.")
        if s["uploads"] >= len(allowed) * UPLOADS_PER_LINE:
            raise CorpusError("Too many attempts in this session. Reload the page to start a new one.", 429)
        p = self.db.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        if not p:
            raise CorpusError("That line no longer exists. Reload the page.")
        folder = (self.audio / s["folder"]).resolve()
        if not folder.is_relative_to(self.audio.resolve()) or folder == self.audio.resolve():
            raise CorpusError("Invalid speaker folder.", 500)
        folder.mkdir(mode=0o700, exist_ok=True)
        instruction = "read" if s["region"] == "standard" else "dialect"
        now = time.time()
        with self._lock:
            self.db.execute("UPDATE sessions SET uploads = uploads + 1 WHERE id = ?", (s["id"],))
            old = self.db.execute("SELECT id, path FROM recordings WHERE session_id = ? AND prompt_id = ?",
                                  (s["id"], prompt_id)).fetchone()
            if old:
                rec_id = old["id"]
            else:
                cur = self.db.execute(
                    "INSERT INTO recordings (contributor_id, session_id, prompt_id, prompt_text, category, instruction, "
                    "path, seconds, rms, clipped_ratio, created_at) VALUES (?,?,?,?,?,?,'',0,0,0,?)",
                    (s["contributor_id"], s["id"], prompt_id, p["text"], p["category"], instruction, now))
                rec_id = cur.lastrowid
            rel = f"audio/{s['folder']}/{rec_id:07d}.wav"
            target = self.root / rel
            tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            try:
                tmp.write_bytes(wav)
                os.replace(tmp, target)
            except OSError:
                self.db.rollback()
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise CorpusError("The server could not save the recording. Please try again.", 500)
            self.db.execute(
                "UPDATE recordings SET path=?, seconds=?, rms=?, clipped_ratio=?, created_at=?, status='ok', "
                "prompt_text=?, category=?, instruction=? WHERE id=?",
                (rel, info["seconds"], info["rms"], info["clipped_ratio"], now, p["text"], p["category"], instruction, rec_id))
            self.db.commit()
        return {"recording_id": rec_id, "seconds": info["seconds"]}

    # ---- admin ----
    def delete_contributor(self, employee_id: str) -> dict:
        emp = clean_employee_id(employee_id)
        row = self.db.execute("SELECT * FROM contributors WHERE employee_id = ?", (emp,)).fetchone()
        if not row:
            raise CorpusError("No contributor with that employee ID.", 404)
        folder = (self.audio / row["folder"]).resolve()
        with self._lock:
            n = self.db.execute("DELETE FROM recordings WHERE contributor_id = ?", (row["id"],)).rowcount
            self.db.execute("DELETE FROM sessions WHERE contributor_id = ?", (row["id"],))
            self.db.execute("DELETE FROM contributors WHERE id = ?", (row["id"],))
            self.db.commit()
        if folder.is_relative_to(self.audio.resolve()) and folder != self.audio.resolve() and folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        return {"speaker_code": row["speaker_code"], "recordings_deleted": n}

    def stats(self) -> dict:
        q = self.db.execute
        by_region = [dict(r) for r in q(
            "SELECT c.region AS region, COUNT(DISTINCT c.id) AS speakers, COUNT(r.id) AS clips, "
            "ROUND(COALESCE(SUM(r.seconds), 0) / 3600.0, 2) AS hours FROM contributors c "
            "LEFT JOIN recordings r ON r.contributor_id = c.id AND r.status = 'ok' GROUP BY c.region ORDER BY c.region")]
        by_category = [dict(r) for r in q(
            "SELECT category, COUNT(*) AS clips, ROUND(SUM(seconds) / 3600.0, 2) AS hours FROM recordings "
            "WHERE status = 'ok' GROUP BY category ORDER BY category")]
        for r in by_region:
            r["label"] = REGIONS.get(r["region"], r["region"])
        return {
            "speakers": q("SELECT COUNT(*) FROM contributors").fetchone()[0],
            "clips": q("SELECT COUNT(*) FROM recordings WHERE status = 'ok'").fetchone()[0],
            "hours": round((q("SELECT COALESCE(SUM(seconds), 0) FROM recordings WHERE status = 'ok'").fetchone()[0]) / 3600.0, 2),
            "flagged": q("SELECT COUNT(*) FROM recordings WHERE status = 'flagged'").fetchone()[0],
            "prompts_active": q("SELECT COUNT(*) FROM prompts WHERE active = 1").fetchone()[0],
            "prompts_unrecorded": q(
                "SELECT COUNT(*) FROM prompts p WHERE p.active = 1 AND NOT EXISTS "
                "(SELECT 1 FROM recordings r WHERE r.prompt_id = p.id AND r.status = 'ok')").fetchone()[0],
            "by_region": by_region,
            "by_category": by_category,
        }

    # ---- review (admin report) ----
    REVIEW_STATUSES = ("ok", "flagged")
    PAGE_MAX = 100

    @staticmethod
    def _like(q: str) -> str:
        return "%" + str(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def list_contributors(self, q: str = "", region: str = "", offset: int = 0, limit: int = 50) -> dict:
        where, args = ["1=1"], []
        if q:
            where.append("(c.name LIKE ? ESCAPE '\\' OR c.employee_id LIKE ? ESCAPE '\\' OR c.speaker_code LIKE ? ESCAPE '\\')")
            args += [self._like(q)] * 3
        if region in REGIONS:
            where.append("c.region = ?")
            args.append(region)
        w = " AND ".join(where)
        total = self.db.execute(f"SELECT COUNT(*) FROM contributors c WHERE {w}", args).fetchone()[0]
        rows = self.db.execute(
            "SELECT c.id, c.speaker_code, c.name, c.employee_id, c.region, c.district, c.gender, c.age_band, c.device, "
            "c.created_at, "
            "SUM(CASE WHEN r.status = 'ok' THEN 1 ELSE 0 END) AS clips, "
            "SUM(CASE WHEN r.status = 'flagged' THEN 1 ELSE 0 END) AS flagged, "
            "ROUND(COALESCE(SUM(CASE WHEN r.status = 'ok' THEN r.seconds END), 0) / 60.0, 1) AS minutes, "
            "MAX(r.created_at) AS last_upload "
            f"FROM contributors c LEFT JOIN recordings r ON r.contributor_id = c.id WHERE {w} "
            "GROUP BY c.id ORDER BY COALESCE(MAX(r.created_at), c.created_at) DESC LIMIT ? OFFSET ?",
            args + [max(1, min(self.PAGE_MAX, int(limit))), max(0, int(offset))]).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def list_recordings(self, *, contributor_id: int = 0, q: str = "", region: str = "", category: str = "",
                        status: str = "", offset: int = 0, limit: int = 25) -> dict:
        where, args = ["r.status != 'deleted'"], []
        if contributor_id:
            where.append("r.contributor_id = ?")
            args.append(int(contributor_id))
        if q:
            where.append("(r.prompt_text LIKE ? ESCAPE '\\' OR c.name LIKE ? ESCAPE '\\' OR c.employee_id LIKE ? ESCAPE '\\')")
            args += [self._like(q)] * 3
        if region in REGIONS:
            where.append("c.region = ?")
            args.append(region)
        if category:
            where.append("r.category = ?")
            args.append(category)
        if status in self.REVIEW_STATUSES:
            where.append("r.status = ?")
            args.append(status)
        w = " AND ".join(where)
        base = f"FROM recordings r JOIN contributors c ON c.id = r.contributor_id WHERE {w}"
        total = self.db.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
        rows = self.db.execute(
            "SELECT r.id, r.prompt_text, r.category, r.instruction, r.seconds, r.rms, r.clipped_ratio, r.created_at, "
            f"r.status, c.speaker_code, c.name, c.employee_id, c.region {base} "
            "ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
            args + [max(1, min(self.PAGE_MAX, int(limit))), max(0, int(offset))]).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def recording_file(self, rec_id: int) -> Path:
        """The WAV of one recording, checked to be inside this corpus' audio folder."""
        row = self.db.execute("SELECT path, status FROM recordings WHERE id = ?", (int(rec_id),)).fetchone()
        if not row or row["status"] == "deleted" or not row["path"]:
            raise CorpusError("No such recording.", 404)
        p = (self.root / row["path"]).resolve()
        if not p.is_relative_to(self.audio.resolve()) or not p.is_file():
            raise CorpusError("The audio file is missing.", 404)
        return p

    def set_recording_status(self, rec_id: int, status: str) -> dict:
        if status not in self.REVIEW_STATUSES:
            raise CorpusError("Status must be 'ok' or 'flagged'.")
        with self._lock:
            n = self.db.execute("UPDATE recordings SET status = ? WHERE id = ? AND status != 'deleted'",
                                (status, int(rec_id))).rowcount
            self.db.commit()
        if not n:
            raise CorpusError("No such recording.", 404)
        return {"id": int(rec_id), "status": status}

    def close(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass


_stores: dict = {}
_stores_lock = threading.Lock()


def get_store(root: Path) -> CorpusStore:
    key = str(Path(root).resolve())
    with _stores_lock:
        st = _stores.get(key)
        if st is None:
            st = _stores[key] = CorpusStore(Path(key))
        return st


def reset_stores() -> None:
    """Tests: drop cached stores (and close their connections)."""
    with _stores_lock:
        for st in _stores.values():
            st.close()
        _stores.clear()
