from functools import lru_cache
from pathlib import Path
import base64
import json
import os
from typing import Any, Dict

from pydantic import Field

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except Exception:  # pragma: no cover - fallback when optional dependency is missing
    from pydantic import BaseModel

    def _resolve_env_path(env_file: str) -> Path:
        candidate = Path(env_file)
        if candidate.is_absolute():
            return candidate

        cwd_candidate = Path.cwd() / candidate
        if cwd_candidate.exists():
            return cwd_candidate

        backend_root_candidate = Path(__file__).resolve().parents[2] / candidate
        if backend_root_candidate.exists():
            return backend_root_candidate

        return cwd_candidate

    def _parse_env_file(env_file: str, encoding: str = "utf-8") -> dict[str, str]:
        resolved = _resolve_env_path(env_file)
        try:
            lines = resolved.read_text(encoding=encoding).splitlines()
        except OSError:
            return {}

        parsed: dict[str, str] = {}
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            parsed[key] = value
        return parsed

    class BaseSettings(BaseModel):  # type: ignore[no-redef]
        def __init__(self, **values: Any) -> None:
            model_config = getattr(self.__class__, "model_config", {}) or {}
            env_file = str(model_config.get("env_file", ".env"))
            env_encoding = str(model_config.get("env_file_encoding", "utf-8"))
            env_values = _parse_env_file(env_file, env_encoding)

            settings_values: dict[str, Any] = {}
            for field_name, field_info in self.__class__.model_fields.items():  # type: ignore[attr-defined]
                env_key = field_name.upper()
                raw_value = os.environ.get(env_key, env_values.get(env_key))
                if raw_value is None:
                    continue

                annotation = field_info.annotation
                if annotation in (dict, Dict, dict[str, str], Dict[str, str]):
                    try:
                        settings_values[field_name] = json.loads(raw_value)
                    except Exception:
                        settings_values[field_name] = raw_value
                else:
                    settings_values[field_name] = raw_value

            settings_values.update(values)
            super().__init__(**settings_values)

    SettingsConfigDict = dict  # type: ignore[misc,assignment]


class Settings(BaseSettings):
    app_name: str = "SafeBill RAG API"
    environment: str = "dev"
    aws_only_mode: bool = False
    auth_provider: str = "supabase"
    ai_provider: str = "gemini"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/safebill"
    aws_region: str = "ap-south-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    aws_secrets_enabled: bool = False
    aws_secrets_manager_secret_id: str = ""
    aws_ssm_parameter_prefix: str = ""
    aws_ssm_recursive: bool = True
    aws_ssm_with_decryption: bool = True
    cors_allowed_origins: str = "http://localhost:3000"
    cors_allow_credentials: bool = False
    # --- Supabase ---
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""
    supabase_jwt_issuer: str = ""
    supabase_storage_bucket: str = "documents"
    # --- Gemini AI ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    # --- OCR ---
    ocr_enabled: bool = True
    use_unstructured_partition: bool = False
    tesseract_cmd: str = ""
    google_vision_api_key: str = ""
    google_vision_endpoint: str = "https://vision.googleapis.com/v1/images:annotate"
    google_vision_credentials_file: str = ""
    google_vision_scope: str = "https://www.googleapis.com/auth/cloud-platform"
    google_maps_api_key: str = ""
    image_ocr_mode: str = "auto"
    extraction_low_confidence_threshold: float = 0.65
    extraction_review_required_threshold: float = 0.6
    textract_proxy_url: str = ""
    textract_proxy_api_key: str = ""
    service_center_directory_path: str = ""
    service_center_live_lookup_enabled: bool = False
    service_center_google_lookup_enabled: bool = False
    max_chunks_per_document: int = 1500
    max_search_results: int = 25
    email_notifications_enabled: bool = False
    sms_notifications_enabled: bool = False
    sms_provider: str = "sns"
    sns_region: str = ""
    sns_sms_type: str = "Transactional"
    sns_sms_sender_id: str = ""
    push_notifications_enabled: bool = False
    push_provider: str = "sns"
    sns_push_topic_arn: str = ""
    whatsapp_notifications_enabled: bool = False
    whatsapp_provider: str = "sns"
    sns_whatsapp_topic_arn: str = ""
    email_provider: str = "ses"
    ses_region: str = ""
    ses_configuration_set: str = ""
    ses_source_arn: str = ""
    ses_from_arn: str = ""
    ses_reply_to_addresses: str = ""
    email_from: str = ""
    email_from_name: str = "SafeBill"
    notification_default_alert_days: str = "30,7,1"
    notification_claim_alert_days: str = "14,3"
    notification_worker_batch_size: int = 50
    notification_worker_poll_seconds: int = 60
    notification_max_retries: int = 5
    notification_retry_backoff_minutes: int = 15
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    cognito_jwt_issuer: str = ""
    cognito_jwt_audience: str = ""
    bedrock_chat_model: str = "apac.anthropic.claude-3-sonnet-20240229-v1:0"
    bedrock_text_mapping_enabled: bool = True
    bedrock_image_region: str = ""
    bedrock_image_model: str = "amazon.titan-image-generator-v2:0"
    aws_anthropic_key: str = ""
    aws_amazonnova_key: str = ""
    product_image_generation_enabled: bool = False
    product_image_width: int = 768
    product_image_height: int = 768
    async_extraction_enabled: bool = False
    async_extraction_source_prefix: str = "async-extraction"
    async_extraction_backend_callback_url: str = ""
    async_extraction_callback_token: str = ""
    async_extraction_ocr_mode: str = "hybrid"
    local_async_extraction_worker_enabled: bool = True
    local_async_extraction_poll_seconds: int = 3
    local_async_extraction_batch_size: int = 4
    api_rate_limit_window_seconds: int = 60
    api_rate_limit_ask_per_window: int = 30
    api_rate_limit_ingest_per_window: int = 20
    api_rate_limit_notification_per_window: int = 40
    auth_tokens: Dict[str, str] = Field(
        default_factory=lambda: {
            "safebill-admin-token": "admin",
            "safebill-analyst-token": "analyst",
            "safebill-auditor-token": "auditor",
            "safebill-viewer-token": "viewer",
        }
    )
    prompt_injection_blocking: bool = True
    storage_provider: str = "supabase"
    s3_bucket_name: str = ""
    s3_key_prefix: str = "documents"
    s3_presign_ttl_seconds: int = 900
    s3_force_path_style: bool = False
    s3_require_upload_success: bool = False
    dynamodb_mirror_enabled: bool = False
    dynamodb_read_fallback_enabled: bool = False
    dynamodb_documents_table_name: str = ""
    dynamodb_extraction_jobs_table_name: str = ""
    dynamodb_user_created_at_index_name: str = "user_id-created_at-index"
    dynamodb_merchant_created_at_index_name: str = "merchant_user_id-created_at-index"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


def _to_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_secret_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        key_text = str(key or "").strip().upper()
        if not key_text:
            continue
        if isinstance(value, (dict, list)):
            normalized[key_text] = json.dumps(value)
        elif value is None:
            normalized[key_text] = ""
        else:
            normalized[key_text] = str(value)
    return normalized


def _parse_secret_string(secret_text: str) -> dict[str, str]:
    text = (secret_text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        return _normalize_secret_map(parsed)

    parsed_env: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        parsed_env[key] = value
    return parsed_env


def _normalize_ssm_parameter_key(parameter_name: str) -> str:
    leaf = str(parameter_name or "").strip().split("/")[-1]
    key = leaf.replace("-", "_").replace(".", "_").strip().upper()
    return key


@lru_cache(maxsize=1)
def _load_aws_runtime_overrides() -> dict[str, str]:
    if not _to_bool(os.environ.get("AWS_SECRETS_ENABLED"), default=False):
        return {}

    try:
        import boto3  # type: ignore
    except Exception:
        return {}

    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("NEXT_PUBLIC_COGNITO_REGION")
        or "ap-south-1"
    )
    overrides: dict[str, str] = {}
    secret_id = str(os.environ.get("AWS_SECRETS_MANAGER_SECRET_ID") or "").strip()
    ssm_prefix = str(os.environ.get("AWS_SSM_PARAMETER_PREFIX") or "").strip()
    ssm_recursive = _to_bool(os.environ.get("AWS_SSM_RECURSIVE"), default=True)
    ssm_with_decryption = _to_bool(os.environ.get("AWS_SSM_WITH_DECRYPTION"), default=True)

    session = boto3.session.Session(region_name=region)

    if secret_id:
        try:
            client = session.client("secretsmanager")
            response = client.get_secret_value(SecretId=secret_id)
            secret_text = str(response.get("SecretString") or "").strip()
            if not secret_text and response.get("SecretBinary"):
                encoded = response["SecretBinary"]
                if isinstance(encoded, str):
                    secret_text = base64.b64decode(encoded).decode("utf-8", errors="ignore")
                else:
                    secret_text = base64.b64decode(bytes(encoded)).decode("utf-8", errors="ignore")
            overrides.update(_parse_secret_string(secret_text))
        except Exception:
            pass

    if ssm_prefix:
        try:
            ssm = session.client("ssm")
            paginator = ssm.get_paginator("get_parameters_by_path")
            for page in paginator.paginate(
                Path=ssm_prefix,
                Recursive=ssm_recursive,
                WithDecryption=ssm_with_decryption,
            ):
                for parameter in page.get("Parameters", []):
                    if not isinstance(parameter, dict):
                        continue
                    name = _normalize_ssm_parameter_key(str(parameter.get("Name") or ""))
                    value = str(parameter.get("Value") or "")
                    if name:
                        overrides[name] = value
        except Exception:
            pass

    return overrides


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    runtime_overrides = _load_aws_runtime_overrides()
    for key, value in runtime_overrides.items():
        if key and key not in os.environ:
            os.environ[key] = value
    settings = Settings()

    # Make AWS SDK (boto3) credentials available when provided via .env/.secrets.
    if settings.aws_access_key_id and "AWS_ACCESS_KEY_ID" not in os.environ:
        os.environ["AWS_ACCESS_KEY_ID"] = settings.aws_access_key_id
    if settings.aws_secret_access_key and "AWS_SECRET_ACCESS_KEY" not in os.environ:
        os.environ["AWS_SECRET_ACCESS_KEY"] = settings.aws_secret_access_key
    if settings.aws_session_token and "AWS_SESSION_TOKEN" not in os.environ:
        os.environ["AWS_SESSION_TOKEN"] = settings.aws_session_token

    return settings
