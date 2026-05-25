"""Auto-detect API key provider from key string."""
import re

OPENAI_PATTERNS = [
    re.compile(r"^sk-proj-[A-Za-z0-9_\-]{40,}$"),
    re.compile(r"^sk-svcacct-[A-Za-z0-9_\-]{40,}$"),
    re.compile(r"^sk-admin-[A-Za-z0-9_\-]{40,}$"),
    re.compile(r"^sk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}$"),
    re.compile(r"^sk-[A-Za-z0-9]{40,}$"),
]
ANTHROPIC_PATTERNS = [
    re.compile(r"^sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_\-]{60,}$"),
    re.compile(r"^sk-ant-[A-Za-z0-9_\-]{60,}$"),
]
GEMINI_PATTERNS = [
    re.compile(r"^AIza[A-Za-z0-9_\-]{35}$"),
]


def detect_provider(key: str) -> str | None:
    """Return 'openai' | 'anthropic' | 'gemini' | None."""
    k = key.strip()
    if not k:
        return None

    for p in ANTHROPIC_PATTERNS:
        if p.match(k):
            return "anthropic"
    for p in GEMINI_PATTERNS:
        if p.match(k):
            return "gemini"
    for p in OPENAI_PATTERNS:
        if p.match(k):
            return "openai"
    return None


def short_key(key: str) -> str:
    if len(key) > 24:
        return f"{key[:12]}…{key[-6:]}"
    return key
