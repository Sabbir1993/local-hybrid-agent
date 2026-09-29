MIME_MAP = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
    ".csv":  "text/csv",
    ".pdf":  "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt":  "application/vnd.ms-powerpoint",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".json": "application/json",
    ".xml":  "application/xml",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

DOCUMENT_EXTENSIONS = set(MIME_MAP.keys()) | {".txt", ".md"}

# generated videos (core/media.py): served inline by /agent/raw, not documents
VIDEO_MIME = {".mp4": "video/mp4", ".webm": "video/webm"}

# Default chunking limits
DEFAULT_MAX_CHARS = 12_000    # ~3000 tokens - safe for 4096-token context windows
CHUNK_SIZE = 10_000           # size for read_file_chunk pages
MAX_CHUNK_CHARS = 40_000      # largest page a caller may ask for

_BINARY_DOCS = (".xlsx", ".xls", ".pdf", ".pptx", ".ppt", ".docx", ".doc")
