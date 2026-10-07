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


class CloudAccessBody(BaseModel):
    allowed: bool


class CategoryBody(BaseModel):
    name: str
    cloud_ok: bool = False


class SourceCategoryBody(BaseModel):
    category: Optional[str] = None


class RuleBody(BaseModel):
    name: str
    kind: str = "keywords"
    pattern: str


class RuleEnabledBody(BaseModel):
    enabled: bool


class RuleTestBody(BaseModel):
    text: str


class WebPolicyBody(BaseModel):
    gap_fill: Optional[bool] = None
    full_cos: Optional[float] = None
    budget: Optional[dict] = None


class BulkDeleteBody(BaseModel):
    ids: list[int]

