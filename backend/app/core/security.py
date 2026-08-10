import re
from functools import lru_cache
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Depends, Header, HTTPException, status

from app.core.config import get_settings

try:
    import jwt
    from jwt import PyJWKClient
except Exception:  # pragma: no cover - optional runtime dependency
    jwt = None  # type: ignore[assignment]
    PyJWKClient = None  # type: ignore[assignment]

PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"reveal\s+(the\s+)?system\s+prompt",
    r"developer\s+message",
    r"tool\s+call",
    r"bypass\s+policy",
    r"disable\s+safety",
    r"drop\s+table",
    r"union\s+select",
    r"exec\(",
    r"<script",
]


@dataclass
class Principal:
    token: str
    role: str
    subject: str | None = None
    user_type: str | None = None
    email: str | None = None
    full_name: str | None = None


@lru_cache(maxsize=2)
def _jwks_client(jwks_url: str):
    if PyJWKClient is None:
        return None
    return PyJWKClient(jwks_url)


def cognito_jwt_runtime_available() -> bool:
    return jwt is not None and PyJWKClient is not None


def _extract_user_type(claims: dict[str, Any]) -> str | None:
    # Supabase: check user_metadata
    user_metadata = claims.get("user_metadata")
    if isinstance(user_metadata, dict):
        candidate = str(user_metadata.get("user_type") or "").strip().lower()
        if candidate in {"consumer", "merchant"}:
            return candidate

    # Supabase: check app_metadata
    app_metadata = claims.get("app_metadata")
    if isinstance(app_metadata, dict):
        candidate = str(app_metadata.get("user_type") or "").strip().lower()
        if candidate in {"consumer", "merchant"}:
            return candidate

    # Legacy Cognito: check custom:user_type
    cognito_custom = str(claims.get("custom:user_type") or "").strip().lower()
    if cognito_custom in {"consumer", "merchant"}:
        return cognito_custom

    preferred_username = str(claims.get("preferred_username") or "").strip().upper()
    if preferred_username.startswith("MER-"):
        return "merchant"
    if preferred_username.startswith("CON-"):
        return "consumer"

    cognito_groups = claims.get("cognito:groups")
    if isinstance(cognito_groups, list):
        lowered_groups = {str(group).strip().lower() for group in cognito_groups}
        if "merchant" in lowered_groups:
            return "merchant"
        if "consumer" in lowered_groups:
            return "consumer"

    return None


def _extract_email(claims: dict[str, Any]) -> str | None:
    direct = str(claims.get("email") or "").strip()
    if "@" in direct:
        return direct[:320]

    for key in ("user_metadata", "app_metadata"):
        section = claims.get(key)
        if not isinstance(section, dict):
            continue
        candidate = str(section.get("email") or "").strip()
        if "@" in candidate:
            return candidate[:320]
    return None


def _extract_full_name(claims: dict[str, Any]) -> str | None:
    # Supabase stores name in user_metadata
    user_metadata = claims.get("user_metadata")
    if isinstance(user_metadata, dict):
        for field in ("full_name", "name"):
            candidate = str(user_metadata.get(field) or "").strip()
            if candidate:
                return candidate[:255]

    direct_name = str(claims.get("name") or "").strip()
    if direct_name:
        return direct_name[:255]

    for key in ("app_metadata",):
        section = claims.get(key)
        if not isinstance(section, dict):
            continue
        for field in ("full_name", "name"):
            candidate = str(section.get(field) or "").strip()
            if candidate:
                return candidate[:255]
    return None


def _resolve_supabase_issuer() -> str:
    """Build the Supabase JWT issuer URL from SUPABASE_URL or SUPABASE_JWT_ISSUER."""
    settings = get_settings()

    # Explicit issuer override
    explicit_issuer = getattr(settings, "supabase_jwt_issuer", "").strip()
    if explicit_issuer:
        return explicit_issuer

    # Build from Supabase URL
    supabase_url = getattr(settings, "supabase_url", "").strip()
    if supabase_url:
        return f"{supabase_url.rstrip('/')}/auth/v1"

    # Fallback: legacy Cognito issuer
    return _resolve_cognito_issuer()


def _resolve_cognito_issuer() -> str:
    settings = get_settings()
    explicit_issuer = settings.cognito_jwt_issuer.strip() if settings.cognito_jwt_issuer else ""
    if explicit_issuer:
        return explicit_issuer

    region = settings.aws_region.strip()
    user_pool_id = settings.cognito_user_pool_id.strip()
    if not region or not user_pool_id:
        return ""
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def _verify_supabase_jwt(token: str) -> dict[str, Any] | None:
    """Verify a Supabase JWT using the JWT secret (HS256) or JWKS (RS256)."""
    settings = get_settings()
    if not token or jwt is None:
        return None

    # Method 1: Verify with Supabase JWT secret (HS256) — simpler, no JWKS
    jwt_secret = getattr(settings, "supabase_jwt_secret", "").strip()
    if jwt_secret:
        import base64
        keys_to_try = [jwt_secret, jwt_secret.encode()]
        try:
            padded = jwt_secret + "=" * (4 - len(jwt_secret) % 4)
            keys_to_try.append(base64.b64decode(padded))
        except Exception:
            pass

        for key in keys_to_try:
            try:
                claims = jwt.decode(
                    token,
                    key,
                    algorithms=["HS256"],
                    audience="authenticated",
                    options={"require": ["exp", "sub"]},
                )
                if isinstance(claims, dict):
                    return claims
            except Exception:
                pass

    # Method 2: Verify with JWKS endpoint (RS256)
    supabase_url = getattr(settings, "supabase_url", "").strip()
    if supabase_url:
        jwks_url = f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        jwks_client = _jwks_client(jwks_url)
        if jwks_client:
            try:
                signing_key = jwks_client.get_signing_key_from_jwt(token)
                claims = jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=["RS256"],
                    audience="authenticated",
                    options={"require": ["exp", "sub"]},
                )
                if isinstance(claims, dict):
                    return claims
            except Exception:
                pass

    return None


def _verify_cognito_jwt(token: str) -> dict[str, Any] | None:
    settings = get_settings()
    if not token or jwt is None:
        return None

    issuer = _resolve_cognito_issuer()
    if not issuer:
        return None

    jwks_client = _jwks_client(f"{issuer.rstrip('/')}/.well-known/jwks.json")
    if jwks_client is None:
        return None

    audience = settings.cognito_jwt_audience.strip() if settings.cognito_jwt_audience else ""
    if not audience:
        audience = settings.cognito_app_client_id.strip()

    decode_kwargs: dict[str, Any] = {
        "algorithms": ["RS256"],
        "issuer": issuer,
        "options": {"require": ["exp", "iat", "sub"], "verify_aud": False},
    }

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(token, signing_key.key, **decode_kwargs)
    except Exception:
        return None

    if not isinstance(claims, dict):
        return None

    if audience:
        aud_claim = claims.get("aud")
        client_id_claim = claims.get("client_id")
        aud_match = isinstance(aud_claim, str) and aud_claim.strip() == audience
        client_id_match = isinstance(client_id_claim, str) and client_id_claim.strip() == audience
        if not (aud_match or client_id_match):
            return None

    return claims


def sanitize_user_query(text: str) -> str:
    stripped = text.replace("\x00", " ")
    stripped = re.sub(r"[\r\n\t]+", " ", stripped)
    return re.sub(r"\s{2,}", " ", stripped).strip()


def detect_prompt_injection(text: str) -> list[str]:
    query = text.lower()
    hits: list[str] = []
    for pattern in PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, query):
            hits.append(pattern)
    return hits


def enforce_safe_query(text: str) -> str:
    settings = get_settings()
    sanitized = sanitize_user_query(text)
    if settings.prompt_injection_blocking:
        hits = detect_prompt_injection(sanitized)
        if hits:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "Query blocked by prompt-injection defenses.", "signals": hits},
            )
    return sanitized


def get_current_principal(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> Principal:
    settings = get_settings()

    token = x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    if token in settings.auth_tokens:
        return Principal(token=token, role=settings.auth_tokens[token])

    provider = settings.auth_provider.strip().lower()

    # Try Supabase JWT first
    if provider in {"supabase", "supabase_auth"}:
        claims = _verify_supabase_jwt(token)
        if claims:
            subject = str(claims.get("sub") or "").strip()
            if not subject:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

            user_type = _extract_user_type(claims) or "consumer"
            role = "merchant" if user_type == "merchant" else "consumer"
            return Principal(
                token=token,
                role=role,
                subject=subject,
                user_type=user_type,
                email=_extract_email(claims),
                full_name=_extract_full_name(claims),
            )

    # Try Cognito JWT
    if provider in {"cognito", "aws_cognito"}:
        claims = _verify_cognito_jwt(token)
        if claims:
            subject = str(claims.get("sub") or "").strip()
            if not subject:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

            user_type = _extract_user_type(claims) or "consumer"
            role = "merchant" if user_type == "merchant" else "consumer"
            return Principal(
                token=token,
                role=role,
                subject=subject,
                user_type=user_type,
                email=_extract_email(claims),
                full_name=_extract_full_name(claims),
            )

    # Auto-detect: try Supabase first, then Cognito
    if provider not in {"cognito", "aws_cognito", "supabase", "supabase_auth"}:
        for verify_fn in (_verify_supabase_jwt, _verify_cognito_jwt):
            claims = verify_fn(token)
            if claims:
                subject = str(claims.get("sub") or "").strip()
                if subject:
                    user_type = _extract_user_type(claims) or "consumer"
                    role = "merchant" if user_type == "merchant" else "consumer"
                    return Principal(
                        token=token,
                        role=role,
                        subject=subject,
                        user_type=user_type,
                        email=_extract_email(claims),
                        full_name=_extract_full_name(claims),
                    )

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def require_roles(*allowed_roles: str) -> Callable:
    def dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if principal.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return principal

    return dependency
