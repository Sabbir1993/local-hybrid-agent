import sys
from pathlib import Path
from typing import Optional
from fastapi.responses import JSONResponse, FileResponse
from core.file_tools import MIME_MAP, VIDEO_MIME
from core.request_context import get_current_user_id
from core.agent_tools import common_workspace, _common_resolve, tool_write_file_common

from .base import router


def _resolve_requested_file(path: str, space: Optional[str] = None) -> Optional[Path]:
    import re as _re
    p = None
    clean_name = Path(path).name
    raw_stem = Path(clean_name).stem
    suffix = Path(clean_name).suffix.lower()
    has_uuid = bool(_re.search(r'[-_][0-9a-fA-F]{8}$', raw_stem))
    clean_stem = _re.sub(r'([-_][0-9a-fA-F]{8})+$', '', raw_stem)

    # 1. If explicit space requested or specific revision uuid requested, try direct resolution first
    #    (generated/ holds images and videos from core/media.py: always an exact path)
    if space == "common" or str(path).replace("\\", "/").lstrip("/").startswith("generated/"):
        try:
            cand = _common_resolve(path)
            if cand.is_file():
                p = cand
        except Exception:
            pass

    # Only the server-side common space is served from local disk. Workspace
    # paths belong to the user's machine -- resolving them locally would serve
    # the server's own file at that path.
    if p is None and has_uuid:
        try:
            cand = _common_resolve(path)
            if cand.is_file():
                p = cand
        except Exception:
            pass

    # 2. Candidate match: if unversioned or not found, find all candidate revisions
    # and sort newest-first by modification time (so freshly generated files always take precedence over stale ones)
    if p is None or not p.is_file():
        try:
            cand_matches = []
            for ws_dir in (common_workspace(),):
                if ws_dir and ws_dir.is_dir():
                    if suffix:
                        cand_matches.extend([f for f in ws_dir.glob(f"{clean_stem}-*{suffix}") if f.is_file()])
                        cand_matches.extend([f for f in ws_dir.glob(f"{clean_stem}_*{suffix}") if f.is_file()])
                        exact_cand = ws_dir / f"{clean_stem}{suffix}"
                        if exact_cand.is_file():
                            cand_matches.append(exact_cand)
                        cand_matches.extend([f for f in ws_dir.glob(f"{clean_stem}*{suffix}") if f.is_file()])
                    cand_matches.extend([f for f in ws_dir.glob(f"{clean_stem}.*") if f.is_file()])
            if cand_matches:
                seen_paths = set()
                unique_cands = []
                for c in sorted(cand_matches, key=lambda f: f.stat().st_mtime, reverse=True):
                    resolved_c = str(c.resolve())
                    if resolved_c not in seen_paths:
                        seen_paths.add(resolved_c)
                        unique_cands.append(c)
                if unique_cands:
                    p = unique_cands[0]
        except Exception:
            pass

    # 3. Fallback recovery: check if the file was created or provided in the
    # requesting user's own recent session messages (never other users')
    uid = get_current_user_id()
    if (p is None or not p.is_file()) and uid is not None:
        try:
            from core.db import _projects_db
            rows = _projects_db.execute(
                "SELECT m.content FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE s.user_id = ? AND (m.content LIKE ? OR m.content LIKE ?) "
                "ORDER BY m.id DESC LIMIT 10",
                (uid, f"%{clean_name}%", f"%[DOWNLOAD: {clean_name}]%")
            ).fetchall()
            for r in rows:
                c_text = r["content"] or ""
                ext = clean_name.split('.')[-1].lower() if '.' in clean_name else ''
                cand_code = None

                if ext in {'xlsx', 'xls'}:
                    # Excel spreadsheet recovery: extract markdown table or code fence
                    tbl_m = _re.search(r'(\|.+?\|\n\|[\s\-:|]+\|\n(?:\|.+?\|\n?)+)', c_text)
                    if tbl_m:
                        cand_code = tbl_m.group(1).strip()
                    if not cand_code:
                        csv_m = _re.search(r'```(?:csv|tsv|excel)?\n([\s\S]+?)\n```', c_text, _re.IGNORECASE)
                        if csv_m:
                            cand_code = csv_m.group(1).strip()
                    if not cand_code:
                        # no tabular data in this message: try an older one. Writing the
                        # whole chat reply into column A would be a fabricated spreadsheet.
                        continue
                    res_str = tool_write_file_common({"path": clean_name, "content": cand_code})
                    if res_str.startswith("error:"):
                        continue
                    m = _re.search(r"\[DOWNLOAD:\s*([^\]]+)\]", res_str or "")
                    real_saved = m.group(1).strip() if m else clean_name
                    q = common_workspace() / real_saved
                    if not q.is_file():
                        # do not hand back a path that was never written (it would 404
                        # later with no record of why)
                        print(f"[download recovery] {clean_name}: expected {real_saved} "
                              f"not on disk", file=sys.stderr)
                        continue
                    p = q
                    break

                if ext:
                    m = _re.search(rf'```{ext}\b[^\n]*\n([\s\S]+?)\n```', c_text, _re.IGNORECASE)
                    if m:
                        cand_code = m.group(1).strip()
                if not cand_code:
                    m = _re.search(r'```[^\n]*\n([\s\S]+?)\n```', c_text)
                    if m:
                        cand_code = m.group(1).strip()
                if not cand_code and ext in {'html', 'htm', 'xml', 'svg'}:
                    m = _re.search(r'(<!DOCTYPE\s+html[\s\S]*?</html>|<html[\s\S]*?</html>|<svg[\s\S]*?</svg>)', c_text, _re.IGNORECASE)
                    if m:
                        cand_code = m.group(1).strip()
                if not cand_code:
                    title_clean = clean_stem.replace('_', ' ').replace('-', ' ').title()
                    # (no invented placeholder rows for CSV or canned HTML dashboards:
                    # a missing file stays a 404 rather than being replaced by fabricated data)
                    if ext == 'md':
                        cand_code = f"# {title_clean}\n\n{c_text}"

                if cand_code:
                    res_str = tool_write_file_common({"path": clean_name, "content": cand_code})
                    if res_str.startswith("error:"):
                        continue
                    m = _re.search(r"\[DOWNLOAD:\s*([^\]]+)\]", res_str or "")
                    real_saved = m.group(1).strip() if m else clean_name
                    p = common_workspace() / real_saved
                    # Also write unversioned copy as alias
                    try:
                        (common_workspace() / clean_name).write_text(cand_code, encoding="utf-8")
                    except OSError as e:
                        print(f"[download recovery] alias write failed for {clean_name}: {e}",
                              file=sys.stderr)
                    break
        except Exception as e:
            # this means the requested file cannot be recovered: surface it instead of
            # silently returning a path that was never written
            print(f"[download recovery] {clean_name}: {type(e).__name__}: {e}", file=sys.stderr)

    return p


@router.get("/agent/download")
@router.get("/download")
async def agent_download(path: str, space: Optional[str] = None):
    """Serve a file as a download attachment.

    Query param:  ?path=relative/path/to/file.xlsx
    Serves only the server-side common space (generated files and uploads);
    project workspaces live on users' machines. Resolved via _common_resolve.
    """
    p = _resolve_requested_file(path, space)
    if p is None or not p.is_file():
        return JSONResponse({"error": f"file not found: {path}"}, status_code=404)

    suffix = p.suffix.lower()
    mime = MIME_MAP.get(suffix) or VIDEO_MIME.get(suffix) or "application/octet-stream"
    return FileResponse(
        str(p),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
    )


@router.get("/agent/raw")
@router.get("/raw")
async def agent_raw(path: str, space: Optional[str] = None):
    """Serve a file inline for previews (HTML, PDF, text, images, spreadsheets).
    
    Query param: ?path=relative/path/to/file.html
    Content-Disposition is 'inline' so browser can render in iframe/embed.
    """
    p = _resolve_requested_file(path, space)
    if p is None or not p.is_file():
        return JSONResponse({"error": f"file not found: {path}"}, status_code=404)


    suffix = p.suffix.lower()
    mime = MIME_MAP.get(suffix)
    if not mime:
        if suffix in {".html", ".htm"}:
            mime = "text/html; charset=utf-8"
        elif suffix in {".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".md", ".txt", ".json", ".log", ".yaml", ".yml", ".toml", ".ini", ".sh", ".bat", ".ps1", ".sql", ".csv"}:
            mime = "text/plain; charset=utf-8"
        elif suffix == ".svg":
            mime = "image/svg+xml"
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            mime = f"image/{suffix.lstrip('.')}"
        elif suffix in VIDEO_MIME:
            mime = VIDEO_MIME[suffix]
        else:
            mime = "application/octet-stream"

    headers = {
        "Content-Disposition": f'inline; filename="{p.name}"',
        "Cache-Control": "no-cache, must-revalidate",
        "X-Content-Type-Options": "nosniff",
    }
    if suffix in {".html", ".htm", ".svg", ".xml"}:
        # Generated documents run in an opaque origin even when opened directly
        # in a tab: scripts work (charts), but never with this app's cookies/API.
        headers["Content-Security-Policy"] = "sandbox allow-scripts allow-forms allow-popups allow-modals"
    return FileResponse(str(p), media_type=mime, headers=headers)
