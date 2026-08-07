from __future__ import annotations

import mimetypes
import re
import uuid
from datetime import datetime, timezone
from typing import Any

try:
    import boto3
except Exception:  # pragma: no cover - optional runtime dependency
    boto3 = None  # type: ignore[assignment]

try:
    from botocore.config import Config as BotoConfig
except Exception:  # pragma: no cover - optional runtime dependency
    BotoConfig = None  # type: ignore[assignment]

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from app.core.config import get_settings


def _normalize_filename(filename: str) -> str:
    name = (filename or "document.bin").strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    name = name.strip(".-")
    if not name:
        return "document.bin"
    return name[:180]


class SupabaseObjectStore:
    """Object storage backed by Supabase Storage."""

    def __init__(self) -> None:
        settings = get_settings()
        self.supabase_url = settings.supabase_url.strip().rstrip("/")
        self.service_role_key = settings.supabase_service_role_key.strip()
        self.bucket = settings.supabase_storage_bucket.strip() or "documents"
        self.enabled = bool(self.supabase_url and self.service_role_key)
        self.key_prefix = settings.s3_key_prefix.strip().strip("/") if settings.s3_key_prefix else "documents"

    @staticmethod
    def guess_content_type(filename: str, fallback: str = "application/octet-stream") -> str:
        guessed, _ = mimetypes.guess_type(filename or "")
        return guessed or fallback

    def build_object_key(self, *, filename: str, source: str) -> str:
        safe_filename = _normalize_filename(filename)
        safe_source = re.sub(r"[^A-Za-z0-9/_-]+", "-", (source or "upload").strip().lower()).strip("/")
        now = datetime.now(timezone.utc)
        dated = f"{now.year:04d}/{now.month:02d}/{now.day:02d}"
        random_id = uuid.uuid4().hex
        key_parts = [part for part in [self.key_prefix, safe_source, dated] if part]
        prefix = "/".join(key_parts)
        return f"{prefix}/{random_id}-{safe_filename}" if prefix else f"{random_id}-{safe_filename}"

    def _storage_api_url(self, path: str) -> str:
        return f"{self.supabase_url}/storage/v1{path}"

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.service_role_key}",
            "apikey": self.service_role_key,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def put_bytes(
        self,
        *,
        key: str,
        payload: bytes,
        filename: str,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        if not self.enabled or httpx is None:
            return None

        final_content_type = content_type or self.guess_content_type(filename)
        url = self._storage_api_url(f"/object/{self.bucket}/{key}")
        headers = self._headers(final_content_type)
        headers["x-upsert"] = "true"

        try:
            response = httpx.post(url, content=payload, headers=headers, timeout=60)
            response.raise_for_status()
        except Exception:
            return None

        return {
            "storage_provider": "supabase",
            "storage_bucket": self.bucket,
            "storage_region": "supabase",
            "storage_key": key,
            "storage_content_type": final_content_type,
            "storage_uploaded_at": datetime.now(timezone.utc).isoformat(),
        }

    def generate_download_url(self, *, key: str, expires_in_seconds: int | None = None) -> str | None:
        if not self.enabled or httpx is None or not key:
            return None

        ttl = expires_in_seconds if expires_in_seconds is not None else 900
        ttl = max(60, min(int(ttl), 3600 * 24))

        url = self._storage_api_url(f"/object/sign/{self.bucket}/{key}")
        headers = self._headers("application/json")

        try:
            response = httpx.post(
                url,
                json={"expiresIn": ttl},
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            signed_url = data.get("signedURL") or data.get("signedUrl") or ""
            if signed_url:
                if signed_url.startswith("/"):
                    return f"{self.supabase_url}/storage/v1{signed_url}"
                return signed_url
        except Exception:
            pass
        return None

    def get_bytes(self, *, key: str) -> bytes | None:
        if not self.enabled or httpx is None or not key:
            return None
        try:
            url = self._storage_api_url(f"/object/{self.bucket}/{key}")
            response = httpx.get(url, headers=self._headers(), timeout=60)
            response.raise_for_status()
            return response.content
        except Exception:
            return None

    def delete_object(self, *, key: str) -> bool:
        if not self.enabled or httpx is None or not key:
            return False
        try:
            url = self._storage_api_url(f"/object/{self.bucket}")
            headers = self._headers("application/json")
            response = httpx.delete(url, json={"prefixes": [key]}, headers=headers, timeout=15)
            response.raise_for_status()
            return True
        except Exception:
            return False


class S3ObjectStore:
    def __init__(self) -> None:
        settings = get_settings()
        self.aws_only_mode = settings.aws_only_mode
        self.provider = settings.storage_provider.strip().lower()
        self.required = bool(settings.s3_require_upload_success or self.aws_only_mode)
        self.bucket = settings.s3_bucket_name.strip()
        self.key_prefix = settings.s3_key_prefix.strip().strip("/")
        self.region = settings.aws_region
        self.default_presign_ttl_seconds = max(60, int(settings.s3_presign_ttl_seconds))
        self.client = None
        self.enabled = False

        if self.provider not in {"s3", "aws_s3"}:
            if self.aws_only_mode:
                raise RuntimeError("AWS-only mode: STORAGE_PROVIDER must be s3.")
            return
        if not self.bucket or boto3 is None:
            if self.aws_only_mode:
                raise RuntimeError("AWS-only mode: S3 bucket and boto3 are required.")
            return

        kwargs: dict[str, Any] = {"region_name": self.region}
        if BotoConfig is not None:
            config_kwargs: dict[str, Any] = {"proxies": {}}
            if settings.s3_force_path_style:
                config_kwargs["s3"] = {"addressing_style": "path"}
            kwargs["config"] = BotoConfig(**config_kwargs)

        try:
            self.client = boto3.client("s3", **kwargs)
            self.enabled = True
        except Exception:
            self.client = None
            self.enabled = False
            if self.aws_only_mode:
                raise RuntimeError("AWS-only mode: failed to initialize S3 client.")

    @staticmethod
    def guess_content_type(filename: str, fallback: str = "application/octet-stream") -> str:
        guessed, _ = mimetypes.guess_type(filename or "")
        return guessed or fallback

    def build_object_key(self, *, filename: str, source: str) -> str:
        safe_filename = _normalize_filename(filename)
        safe_source = re.sub(r"[^A-Za-z0-9/_-]+", "-", (source or "upload").strip().lower()).strip("/")
        now = datetime.now(timezone.utc)
        dated = f"{now.year:04d}/{now.month:02d}/{now.day:02d}"
        random_id = uuid.uuid4().hex
        key_parts = [part for part in [self.key_prefix, safe_source, dated] if part]
        prefix = "/".join(key_parts)
        return f"{prefix}/{random_id}-{safe_filename}" if prefix else f"{random_id}-{safe_filename}"

    def put_bytes(
        self,
        *,
        key: str,
        payload: bytes,
        filename: str,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        if not self.enabled or not self.client:
            return None
        safe_metadata = {str(k).lower(): str(v)[:2000] for k, v in (metadata or {}).items() if str(v).strip()}
        final_content_type = content_type or self.guess_content_type(filename)
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=payload,
            ContentType=final_content_type,
            Metadata=safe_metadata,
        )
        return {
            "storage_provider": "s3",
            "storage_bucket": self.bucket,
            "storage_region": self.region,
            "storage_key": key,
            "storage_content_type": final_content_type,
            "storage_uploaded_at": datetime.now(timezone.utc).isoformat(),
            "storage_etag": str(response.get("ETag") or "").strip('"'),
        }

    def generate_download_url(self, *, key: str, expires_in_seconds: int | None = None) -> str | None:
        if not self.enabled or not self.client or not key:
            return None
        ttl = expires_in_seconds if expires_in_seconds is not None else self.default_presign_ttl_seconds
        ttl = max(60, min(int(ttl), 3600 * 24))
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl,
        )

    def get_bytes(self, *, key: str) -> bytes | None:
        if not self.enabled or not self.client or not key:
            return None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response.get("Body")
            if body is None:
                return None
            return body.read()
        except Exception:
            return None

    def delete_object(self, *, key: str) -> bool:
        if not self.enabled or not self.client or not key:
            return False
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False


def get_object_store():
    """Factory: returns the appropriate object store based on STORAGE_PROVIDER."""
    settings = get_settings()
    provider = settings.storage_provider.strip().lower()
    if provider in {"supabase", "supabase_storage"}:
        return SupabaseObjectStore()
    return S3ObjectStore()
