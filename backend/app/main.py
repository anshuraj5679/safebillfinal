from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.dependencies import get_services
from app.api.routes import process_pending_async_extraction_jobs, router
from app.core.config import get_settings
from app.core.security import cognito_jwt_runtime_available
from app.core.database import SessionLocal
from app.services.notifications import NotificationService

settings = get_settings()
logger = logging.getLogger(__name__)


def _parse_origins(raw: str) -> list[str]:
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or ["http://localhost:3000"]


def _require_setting(name: str, value: str) -> None:
    if not str(value or "").strip():
        raise RuntimeError(f"Missing required AWS setting: {name}")


def _validate_aws_only_configuration() -> None:
    if not settings.aws_only_mode:
        return

    provider_expectations = {
        "AUTH_PROVIDER": (settings.auth_provider, {"cognito", "aws_cognito"}),
        "AI_PROVIDER": (settings.ai_provider, {"bedrock", "aws_bedrock"}),
        "STORAGE_PROVIDER": (settings.storage_provider, {"s3", "aws_s3"}),
        "EMAIL_PROVIDER": (settings.email_provider, {"ses", "aws_ses"}),
        "SMS_PROVIDER": (settings.sms_provider, {"sns", "aws_sns"}),
        "PUSH_PROVIDER": (settings.push_provider, {"sns", "aws_sns"}),
        "WHATSAPP_PROVIDER": (settings.whatsapp_provider, {"sns", "aws_sns"}),
    }
    for key, (value, allowed) in provider_expectations.items():
        if str(value or "").strip().lower() not in allowed:
            raise RuntimeError(
                f"AWS-only mode requires {key} in {sorted(allowed)}. Current: {value!r}"
            )

    _require_setting("AWS_REGION", settings.aws_region)
    _require_setting("COGNITO_USER_POOL_ID", settings.cognito_user_pool_id)
    _require_setting("COGNITO_APP_CLIENT_ID", settings.cognito_app_client_id)
    _require_setting("S3_BUCKET_NAME", settings.s3_bucket_name)
    _require_setting("BEDROCK_CHAT_MODEL", settings.bedrock_chat_model)

    if settings.email_notifications_enabled:
        _require_setting("SES_REGION", settings.ses_region or settings.aws_region)
        _require_setting("EMAIL_FROM", settings.email_from)

    if settings.sms_notifications_enabled:
        _require_setting("SNS_REGION", settings.sns_region or settings.aws_region)
    if settings.push_notifications_enabled:
        _require_setting("SNS_REGION", settings.sns_region or settings.aws_region)
        _require_setting("SNS_PUSH_TOPIC_ARN", settings.sns_push_topic_arn)
    if settings.whatsapp_notifications_enabled:
        _require_setting("SNS_REGION", settings.sns_region or settings.aws_region)
        _require_setting("SNS_WHATSAPP_TOPIC_ARN", settings.sns_whatsapp_topic_arn)

    proxy_url = str(settings.textract_proxy_url or "").strip().lower()
    if proxy_url and "amazonaws.com" not in proxy_url:
        raise RuntimeError(
            "AWS-only mode requires TEXTRACT_PROXY_URL to be an AWS endpoint (amazonaws.com), "
            "or leave it empty to use direct AWS Textract."
        )

    auth_provider = str(settings.auth_provider or "").strip().lower()
    if auth_provider in {"cognito", "aws_cognito"} and not cognito_jwt_runtime_available():
        raise RuntimeError(
            "Cognito auth requires PyJWT[crypto] in the active Python runtime. "
            "Install backend requirements before starting the API."
        )


_validate_aws_only_configuration()


allowed_origins = _parse_origins(settings.cors_allowed_origins)
allow_credentials = settings.cors_allow_credentials and "*" not in allowed_origins

def _should_start_in_app_notification_worker() -> bool:
    # Render deployment commonly runs only the web process; start a lightweight
    # worker loop in-process when outbound notification channels are enabled.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    if os.getenv("DISABLE_IN_APP_NOTIFICATION_WORKER", "").strip().lower() in {"1", "true", "yes"}:
        return False

    active_channels = (
        settings.email_notifications_enabled
        or settings.sms_notifications_enabled
        or settings.push_notifications_enabled
        or settings.whatsapp_notifications_enabled
    )
    return bool(active_channels)


def _should_start_local_async_extraction_worker() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    if os.getenv("DISABLE_LOCAL_ASYNC_EXTRACTION_WORKER", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return bool(settings.async_extraction_enabled and settings.local_async_extraction_worker_enabled)


@asynccontextmanager
async def _lifespan(app: FastAPI):  # pragma: no cover - background loop
    _ = app
    from app.core.database import Base, engine
    Base.metadata.create_all(bind=engine)
    stop: threading.Event | None = None
    async_stop: threading.Event | None = None

    if _should_start_in_app_notification_worker():
        poll_seconds = max(5, int(settings.notification_worker_poll_seconds))
        stop = threading.Event()
        service = NotificationService()

        def _loop() -> None:
            while not stop.is_set():
                try:
                    with SessionLocal() as db:
                        result = service.process_due_jobs(db)
                    logger.info(
                        "notification_worker processed=%s sent=%s failed=%s deadLettered=%s",
                        result.get("processed"),
                        result.get("sent"),
                        result.get("failed"),
                        result.get("deadLettered"),
                    )
                except Exception:
                    logger.exception("notification_worker tick failed")
                stop.wait(poll_seconds)

        thread = threading.Thread(target=_loop, name="notification-worker", daemon=True)
        thread.start()
        logger.info("notification_worker started poll_seconds=%s", poll_seconds)
    else:
        logger.info("notification_worker disabled (no outbound channels enabled)")

    if _should_start_local_async_extraction_worker():
        async_poll_seconds = max(1, int(settings.local_async_extraction_poll_seconds))
        async_stop = threading.Event()
        services = get_services()

        def _async_loop() -> None:
            while not async_stop.is_set():
                try:
                    with SessionLocal() as db:
                        result = process_pending_async_extraction_jobs(db=db, services=services)
                    if result.get("processed"):
                        logger.info(
                            "async_extraction_worker processed=%s completed=%s failed=%s",
                            result.get("processed"),
                            result.get("completed"),
                            result.get("failed"),
                        )
                except Exception:
                    logger.exception("async_extraction_worker tick failed")
                async_stop.wait(async_poll_seconds)

        async_thread = threading.Thread(target=_async_loop, name="async-extraction-worker", daemon=True)
        async_thread.start()
        logger.info("async_extraction_worker started poll_seconds=%s", async_poll_seconds)
    else:
        logger.info("async_extraction_worker disabled")

    try:
        yield
    finally:
        if stop is not None:
            stop.set()
        if async_stop is not None:
            async_stop.set()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
