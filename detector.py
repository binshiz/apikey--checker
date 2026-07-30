"""Auto-detect API key provider from key string."""
import json
import re
import unicodedata
from urllib.parse import urlsplit

OPENAI_PATTERNS = [
    re.compile(r"^sk-proj-[A-Za-z0-9_\-]{40,}$"),
    re.compile(r"^sk-svcacct-[A-Za-z0-9_\-]{40,}$"),
    re.compile(r"^sk-admin-[A-Za-z0-9_\-]{40,}$"),
    re.compile(r"^sk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}$"),
    re.compile(r"^sk-[A-Za-z0-9]{40,}$"),
]
OPENROUTER_API_KEY_PATTERN = re.compile(r"^sk-or-v1-[a-f0-9]{64}$")
ANTHROPIC_PATTERNS = [
    re.compile(r"^sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_\-]{60,}$"),
    re.compile(r"^sk-ant-[A-Za-z0-9_\-]{60,}$"),
]
GEMINI_PATTERNS = [
    re.compile(r"^AIza[A-Za-z0-9_\-]{35}$"),
]
AZURE_OPENAI_ENDPOINT_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
    r"(?:openai\.azure\.com|services\.ai\.azure\.com)$",
    re.IGNORECASE,
)
AZURE_OPENAI_ALLOWED_PATHS = {
    "",
    "/openai/v1",
    "/openai/v1/chat/completions",
}
# Azure exposes resource keys in more than one form. Older keys are commonly
# 32 hex characters, while newer Foundry-backed resources can return longer
# opaque values. Microsoft documents the header contract, not a fixed length.
# Keep this opaque and conservative: printable token characters only, no pipe
# or whitespace, with a bounded length to avoid accepting arbitrary pasted text.
AZURE_OPENAI_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{32,512}$")
AWS_ACCESS_KEY_ID_PATTERN = re.compile(r"^AKIA[A-Z0-9]{16}$")
AWS_SECRET_ACCESS_KEY_PATTERN = re.compile(r"^[A-Za-z0-9/+=]{40}$")
AWS_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-\d+$")
# Native Amazon Bedrock API keys are bearer tokens whose current public form
# starts with ``ABSK``. Keep the suffix grammar deliberately future-friendly:
# AWS documents the authentication scheme, but not a fixed token length.
AWS_BEDROCK_API_KEY_PATTERN = re.compile(r"^ABSK[A-Za-z0-9._~+/=-]{20,8188}$")
GCP_SERVICE_ACCOUNT_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,253}"
    r"@[A-Za-z0-9.-]+\.gserviceaccount\.com$",
    re.IGNORECASE,
)
GCP_SERVICE_ACCOUNT_MAX_BYTES = 64 * 1024
GCP_PRIVATE_KEY_MAX_CHARS = 32 * 1024
GCP_REQUIRED_STRING_FIELDS = (
    "project_id",
    "private_key_id",
    "private_key",
    "client_email",
    "client_id",
    "token_uri",
)


def _normalize_azure_openai_endpoint(value: str) -> str | None:
    """Return a safe Azure OpenAI hostname from a base or inference URL."""
    endpoint = unicodedata.normalize("NFKC", value).strip()
    if not endpoint:
        return None
    if "://" not in endpoint:
        endpoint = f"https://{endpoint}"
    parsed = urlsplit(endpoint)
    if parsed.scheme.lower() != "https":
        return None
    try:
        has_port = parsed.port is not None
    except ValueError:
        return None
    if parsed.username or parsed.password or has_port:
        return None
    if parsed.query or parsed.fragment:
        return None
    hostname = (parsed.hostname or "").lower()
    if not AZURE_OPENAI_ENDPOINT_PATTERN.fullmatch(hostname):
        return None
    path = parsed.path.rstrip("/")
    if path not in AZURE_OPENAI_ALLOWED_PATHS:
        return None
    return hostname


def _azure_openai_candidate_parts(key: str) -> tuple[str, str] | None:
    """Split an Azure OpenAI ``[https://]endpoint|api-key`` candidate safely."""
    normalized = unicodedata.normalize("NFKC", key).strip()
    parts = normalized.split("|")
    if len(parts) != 2:
        return None
    endpoint_value, api_key = (part.strip() for part in parts)
    endpoint = _normalize_azure_openai_endpoint(endpoint_value)
    if endpoint is None:
        return None
    return endpoint, api_key


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


def _bedrock_api_key_candidate(key: str) -> str | None:
    """Return a normalized native Bedrock bearer token candidate.

    A valid optional ``|region`` suffix is accepted so exported rows can be
    pasted back into the checker without creating one duplicate per region.
    """
    normalized = unicodedata.normalize("NFKC", key).strip()
    parts = normalized.split("|")
    if len(parts) not in (1, 2):
        return None
    token = parts[0].strip()
    if len(parts) == 2 and not AWS_REGION_PATTERN.fullmatch(parts[1].strip().lower()):
        return None
    if not token.startswith("ABSK"):
        return None
    return token


def _validated_gcp_service_account(value: str | dict) -> dict:
    """Return a validated GCP service-account info object.

    Validation intentionally covers only downloaded service-account key files.
    Workload identity, authorized-user, and external-account JSON documents use
    different authentication flows and must not be mistaken for private keys.
    """
    if isinstance(value, str):
        if len(value.encode("utf-8")) > GCP_SERVICE_ACCOUNT_MAX_BYTES:
            raise ValueError("GCP service account JSON exceeds 64 KiB")
        try:
            info = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid GCP service account JSON") from exc
    elif isinstance(value, dict):
        info = dict(value)
    else:
        raise ValueError("GCP service account credential must be a JSON object")

    if info.get("type") != "service_account":
        raise ValueError("JSON credential type must be service_account")

    for field in GCP_REQUIRED_STRING_FIELDS:
        field_value = info.get(field)
        if not isinstance(field_value, str) or not field_value.strip():
            raise ValueError(f"GCP service account field {field} is required")

    if len(info["project_id"]) > 256:
        raise ValueError("GCP project_id is too long")
    if len(info["private_key_id"]) > 256:
        raise ValueError("GCP private_key_id is too long")
    if len(info["client_id"]) > 256:
        raise ValueError("GCP client_id is too long")
    if not GCP_SERVICE_ACCOUNT_EMAIL_PATTERN.fullmatch(info["client_email"]):
        raise ValueError("invalid GCP service account client_email")

    private_key = info["private_key"]
    if (
        len(private_key) > GCP_PRIVATE_KEY_MAX_CHARS
        or not private_key.strip().startswith("-----BEGIN PRIVATE KEY-----")
        or not private_key.strip().endswith("-----END PRIVATE KEY-----")
    ):
        raise ValueError("invalid GCP service account private_key")

    try:
        canonical = json.dumps(
            info,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("GCP service account JSON contains unsupported values") from exc
    if len(canonical.encode("utf-8")) > GCP_SERVICE_ACCOUNT_MAX_BYTES:
        raise ValueError("GCP service account JSON exceeds 64 KiB")
    return info


def canonicalize_gcp_service_account(value: str | dict) -> str:
    """Return the stable, single-line storage/export representation."""
    info = _validated_gcp_service_account(value)
    return json.dumps(
        info,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_gcp_service_account(key: str) -> dict | None:
    """Parse a canonical or pretty-printed GCP service-account key document."""
    try:
        return _validated_gcp_service_account(key)
    except (TypeError, ValueError):
        return None


def normalize_key(key: str) -> str:
    """Return the canonical storage form for a pasted API credential.

    Pipe-delimited credentials normalize compatibility characters and separator
    whitespace. Azure OpenAI endpoints are lower-cased. An optional AWS region
    field is discarded because Bedrock checks discover regions themselves; this
    also de-duplicates one credential pasted once per region.
    """
    stripped = key.strip()
    gcp_service_account = parse_gcp_service_account(stripped)
    if gcp_service_account is not None:
        return canonicalize_gcp_service_account(gcp_service_account)
    azure_openai = parse_azure_openai_key(stripped)
    if azure_openai is not None:
        return "|".join(azure_openai)
    bedrock_api_key = parse_bedrock_api_key(stripped)
    if bedrock_api_key is not None:
        return bedrock_api_key
    parts = _aws_candidate_parts(stripped)
    if parts is None:
        return stripped
    return "|".join(parts)


def parse_azure_openai_key(key: str) -> tuple[str, str] | None:
    """Parse Azure OpenAI ``[https://]resource-host|resource-key`` input."""
    parts = _azure_openai_candidate_parts(key)
    if parts is None:
        return None
    endpoint, api_key = parts
    if not AZURE_OPENAI_API_KEY_PATTERN.fullmatch(api_key):
        return None
    return endpoint, api_key


def parse_openrouter_key(key: str) -> str | None:
    """Parse the documented OpenRouter ``sk-or-v1-`` API key format."""
    token = key.strip()
    if not OPENROUTER_API_KEY_PATTERN.fullmatch(token):
        return None
    return token


def azure_openai_chat_completions_url(endpoint: str) -> str | None:
    """Build the canonical v1 Chat Completions URL for a safe endpoint."""
    hostname = _normalize_azure_openai_endpoint(endpoint)
    if hostname is None:
        return None
    return f"https://{hostname}/openai/v1/chat/completions"


def parse_bedrock_api_key(key: str) -> str | None:
    """Parse a native Amazon Bedrock ``ABSK...`` bearer API key."""
    token = _bedrock_api_key_candidate(key)
    if token is None or not AWS_BEDROCK_API_KEY_PATTERN.fullmatch(token):
        return None
    return token


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

    if parse_azure_openai_key(k) is not None:
        return "azure_openai"
    if parse_bedrock_api_key(k) is not None or parse_bedrock_key(k) is not None:
        return "aws_bedrock"
    if parse_gcp_service_account(k) is not None:
        return "gcp_service_account"
    if parse_openrouter_key(k) is not None:
        return "openrouter"

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
    gcp_service_account = parse_gcp_service_account(k)
    if gcp_service_account is not None:
        project_id = gcp_service_account["project_id"]
        client_email = gcp_service_account["client_email"]
        key_id_suffix = gcp_service_account["private_key_id"][-8:]
        return f"{project_id}|{client_email}|…{key_id_suffix}"
    azure_openai = _azure_openai_candidate_parts(k)
    if azure_openai is not None:
        return f"{azure_openai[0]}|••••••••"
    bedrock_api_key = _bedrock_api_key_candidate(k)
    if bedrock_api_key is not None:
        if len(bedrock_api_key) > 18:
            return f"{bedrock_api_key[:8]}…{bedrock_api_key[-5:]}"
        return "ABSK••••••••"
    openrouter_api_key = parse_openrouter_key(k)
    if openrouter_api_key is not None:
        return f"sk-or-v1-…{openrouter_api_key[-6:]}"
    access_key_id = _aws_display_access_key_id(k)
    if access_key_id is not None:
        if len(access_key_id) > 12:
            access_key_id = f"{access_key_id[:8]}…{access_key_id[-4:]}"
        return f"{access_key_id}|••••••••"
    if len(k) > 24:
        return f"{k[:12]}…{k[-6:]}"
    return k
