import asyncio
from pathlib import Path
from typing import Optional
from fastapi import UploadFile, File as FastAPIFile
from fastapi.responses import JSONResponse
from core.file_tools import extract_file_content
from core.agent_tools import common_workspace

from .base import router
from .constants import MAX_UPLOAD_BYTES, MAX_UPLOAD_FILES


@router.post("/agent/upload")
async def agent_upload(files: list[UploadFile] = FastAPIFile(...), space: Optional[str] = None):
    """Upload one or more document files.
    
    Always saved to the server-side common upload space. The project workspace
    lives on the user's machine; writing active_workspace() here would write to
    that path on the SERVER's disk. (`space` is accepted for compatibility.)
    """
    if len(files) > MAX_UPLOAD_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_UPLOAD_FILES} per upload)"}, status_code=400)
    target_dir = common_workspace()
    target_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for uf in files:
        fname = uf.filename or "upload"
        # Sanitize filename
        safe_name = Path(fname.replace("\\", "/")).name or "upload"
        dest = _unique_dest(target_dir, safe_name)
        safe_name = dest.name
        try:
            data = await uf.read(MAX_UPLOAD_BYTES + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError(f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
            dest.write_bytes(data)
        except Exception as e:
            results.append({
                "name": safe_name,
                "path": safe_name,
                "size": 0,
                "preview": f"(upload error: {e})",
                "truncated": False,
                "error": str(e),
            })
            continue
        size = len(data)
        # Extract text content for context injection
        try:
            preview, truncated = await asyncio.get_event_loop().run_in_executor(
                None, extract_file_content, dest)
        except Exception as e:
            preview = f"(extraction error: {e})"
            truncated = False
        results.append({
            "name": safe_name,
            "path": safe_name,
            "size": size,
            "preview": preview,
            "truncated": truncated,
        })
    return {"files": results}


def _unique_dest(folder: Path, name: str) -> Path:
    """folder/name, or name-2.ext, name-3.ext ... when taken (never overwrite an upload)."""
    dest = folder / name
    stem, suf = Path(name).stem, Path(name).suffix
    n = 2
    while dest.exists():
        dest = folder / f"{stem}-{n}{suf}"
        n += 1
    return dest
