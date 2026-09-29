from pathlib import Path
from typing import Optional
from pydantic import BaseModel
from core.config import KNOWLEDGE_UPLOADS_DIR

MAX_KB_FILE_BYTES = 50 * 1024 * 1024
MAX_KB_CHUNK_BYTES = 8 * 1024 * 1024
MAX_KB_CHUNKS = 200
CHUNKS_TEMP_DIR = KNOWLEDGE_UPLOADS_DIR / ".chunks"


class TextBody(BaseModel):
    title: str
    text: str


class UrlBody(BaseModel):
    title: str
    url: str


class CompleteUploadBody(BaseModel):
    upload_id: str
    filename: str
    total_chunks: int
    title: Optional[str] = None


class RoleAccessBody(BaseModel):
    roles: list[str]
