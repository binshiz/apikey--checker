"""Auto-detect API key provider from key string."""
import re
import unicodedata

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
AWS_ACCESS_KEY_ID_PATTERN = re.compile(r"^AKIA[A-Z0-9]{16}$")
AWS_SECRET_ACCESS_KEY_PATTERN = re.compile(r"^[A-Za-z0-9/+=]{40}$")
AWS_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-\d+$")


def _aws_candidate_parts(key: str) -> tuple[str, str] | None:
    """Split a likely AWS credential without deciding whether it is supported."""
    normalized = unicodedata.normalize("NFKC", key)
    parts = normalized.split("|")
    if len(parts) not in (2, 3):
        return None
    access_key_id, secret_access_key = (part.strip() for part in parts[:2])
    if len(parts) == 3 and not AWS_REGION_PATTERN.fullmatch(parts[2].strip().lower()):
        return None
    if not access_key_id.upper().startswith(("AKIA", "ASIA")):
        return None
    return access_key_id, secret_access_key


def _aws_display_access_key_id(key: str) -> str | None:
    """Return the non-secret half of any AWS-looking pipe-delimited value."""
    normalized = unicodedata.normalize("NFKC", key)
    if "|" not in normalized:
        return None
    access_key_id = normalized.split("|", 1)[0].strip()
    if not access_key_id.upper().startswith(("AKIA", "ASIA")):
        return None
    return access_key_id


def normalize_key(key: str) -> str:
    """Return the canonical storage form for a pasted API credential.

    Non-AWS credentials are only stripped. AWS credentials additionally
    normalize compatibility characters and whitespace around the separator.
    An optional third region field is discarded because Bedrock checks discover
    regions themselves; this also de-duplicates one credential pasted once per
    region.
    """
    stripped = key.strip()
    parts = _aws_candidate_parts(stripped)
    if parts is None:
        return stripped
    return "|".join(parts)


def parse_bedrock_key(key: str) -> tuple[str, str] | None:
    """Parse a supported long-lived AWS access key pair.

    Temporary ``ASIA`` credentials intentionally remain unsupported because a
    session token is required in addition to the fields accepted here. A valid
    optional third field (``AccessKey|Secret|region``) is treated as a source
    hint and deliberately ignored; the checker scans all configured regions.
    """
    parts = _aws_candidate_parts(key.strip())
    if parts is None:
        return None
    access_key_id, secret_access_key = parts
    if not AWS_ACCESS_KEY_ID_PATTERN.fullmatch(access_key_id):
        return None
    if not AWS_SECRET_ACCESS_KEY_PATTERN.fullmatch(secret_access_key):
        return None
    return access_key_id, secret_access_key


def detect_provider(key: str) -> str | None:
    """Return a supported provider slug, or ``None`` for an unknown format."""
    k = normalize_key(key)
    if not k:
        return None

    if parse_bedrock_key(k) is not None:
        return "aws_bedrock"

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
    k = normalize_key(key)
    access_key_id = _aws_display_access_key_id(k)
    if access_key_id is not None:
        if len(access_key_id) > 12:
            access_key_id = f"{access_key_id[:8]}…{access_key_id[-4:]}"
        return f"{access_key_id}|••••••••"
    if len(k) > 24:
        return f"{k[:12]}…{k[-6:]}"
    return k
