import re

JOBS = {
    "agent.reason": {
        "kind": "chat",
        "default": "main",
        "label": "Thinking & planning",
        "hint": "Agent steps that need the strongest model: planning, hard reasoning, recovering when stuck.",
    },
    "agent.tool_step": {
        "kind": "chat",
        "default": "executor",
        "label": "Routine tool calls",
        "hint": "Quick agent steps like reading files, searching and small edits.",
    },
    "summarize": {
        "kind": "chat",
        "default": "executor",
        "label": "Summarizing chats",
        "hint": "Writes the summary when a long chat is compacted (/compact).",
    },
    "commit_msg": {
        "kind": "chat",
        "default": "executor",
        "label": "Commit & PR messages",
        "hint": "Writes git commit messages and pull-request descriptions from a diff.",
    },
    "subagent": {
        "kind": "chat",
        "default": "executor",
        "label": "Sub-agents",
        "hint": "Helper agents the main agent starts for a focused sub-task (when no role picks a model).",
    },
    "verify": {
        "kind": "chat",
        "default": "main",
        "label": "Checking answers",
        "hint": "Double-checks an answer against the question and sources before or after you see it.",
    },
    "search_rewrite": {
        "kind": "chat",
        "default": "executor",
        "label": "Web search queries",
        "hint": "Turns your question into a short web search. Only used when that model is already running.",
        "local_only": True,
    },
    "input_guard": {
        "kind": "chat",
        "default": "executor",
        "label": "Policy checks",
        "hint": "Checks messages against your organization's policy rules. Always on this PC.",
        "local_only": True,
    },
    "vision": {
        "kind": "vision",
        "default": "vision",
        "label": "Reading images",
        "hint": "Describes pictures and screenshots you attach.",
    },
    "embed": {
        "kind": "embed",
        "default": "embedder",
        "label": "Search memory",
        "hint": "Turns documents into vectors for memory and knowledge search. Always on this PC; switching models needs a re-index.",
        "local_only": True,
    },
    "image_gen": {
        "kind": "image_gen",
        "default": None,
        "label": "Making images",
        "media": True,
        "hint": "Creates pictures from a description (/image in chat, or the agent's generate_image tool).",
    },
    "video_gen": {
        "kind": "video_gen",
        "default": None,
        "label": "Making videos",
        "media": True,
        "hint": "Creates short video clips from a description (/video in chat, or the agent).",
    },
    "transcribe": {
        "kind": "stt",
        "default": None,
        "label": "Speech to text",
        "media": True,
        "hint": "Turns your voice (the mic button) and audio files into text. Stays on this PC unless an admin allows cloud speech.",
        "local_first": True,
    },
}

BUILTIN_LANES = {
    "main": {"kind": "chat", "label": "Main brain"},
    "executor": {"kind": "chat", "label": "Fast helper"},
    "vision": {"kind": "vision", "label": "Image reader"},
    "embedder": {"kind": "embed", "label": "Search memory"},
}

_KIND_OK = {
    "chat": ("chat", "vision"),
    "vision": ("vision",),
    "embed": ("embed",),
    "image_gen": ("image_gen",),
    "video_gen": ("video_gen",),
    "stt": ("stt",),
}

MEDIA_KINDS = ("image_gen", "video_gen", "stt")
SHARED_MEDIA_JOBS = ("image_gen", "video_gen")

KIND_NEED = {
    "chat": "a text model",
    "vision": "an image model",
    "embed": "an embedding model",
    "image_gen": "an image maker",
    "video_gen": "a video maker",
    "stt": "a speech-to-text model",
}

LANE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
SD_FILE_KEYS = ("diffusion_model", "llm", "vae", "llm_vision")
SD_SAMPLER_RE = re.compile(r"^[a-z0-9_+]{2,24}$")


def kind_ok(job_kind: str, lane_kind: str) -> bool:
    return lane_kind in _KIND_OK.get(job_kind, (job_kind,))
