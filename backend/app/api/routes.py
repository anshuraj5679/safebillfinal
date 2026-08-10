from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from datetime import date
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, Response, UploadFile
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

try:
    import boto3
except Exception:  # pragma: no cover - optional runtime dependency
    boto3 = None  # type: ignore[assignment]

try:
    import httpx
except Exception:  # pragma: no cover - optional runtime dependency
    httpx = None  # type: ignore[assignment]

try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account
except Exception:  # pragma: no cover - optional runtime dependency
    GoogleAuthRequest = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]

from app.api.dependencies import ServiceRegistry, get_services
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import Principal, enforce_safe_query, require_roles
from app.models import (
    Chunk,
    Document,
    ExtractionJob,
    ExtractionReview,
    MerchantAssignmentAudit,
    NotificationDelivery,
    NotificationJob,
    SecurityAuditLog,
)
from app.parsers.pdf_parser import extract_invoice_metadata, parse_pdf_document
from app.services.bedrock_client import configure_bedrock_api_key
from app.services.gemini_client import gemini_extract_image_metadata, gemini_classify_image
from app.schemas import (
    AskRequest,
    AskResponse,
    AsyncExtractionCallbackRequest,
    AsyncExtractionJobCreateResponse,
    AsyncExtractionJobStatusResponse,
    BharatAIAskRequest,
    BharatAIAskResponse,
    BharatAIEnrichRequest,
    BharatAIEnrichResponse,
    BharatAITranslateBatchRequest,
    BharatAITranslateBatchResponse,
    BharatAITranslateRequest,
    BharatAITranslateResponse,
    CalendarLinkResponse,
    ClaimAssistantResponse,
    ClaimPacketResponse,
    Citation,
    DocumentProductImageGenerateRequest,
    DocumentProductImageUrlResponse,
    DocumentProductImageView,
    DocumentShareMemberView,
    DocumentShareRequest,
    DocumentShareResponse,
    DocumentsResponse,
    FraudCheckResponse,
    FraudSignalView,
    DocumentView,
    ExtractionReviewConfirmRequest,
    ExtractionReviewQueueResponse,
    ExtractionReviewView,
    ExtractionTraceStep,
    IngestPDFResponse,
    IngestVendorTableResponse,
    MerchantAssignmentAcceptRequest,
    MerchantAssignmentAuditResponse,
    MerchantAssignmentAuditView,
    MerchantActivityItem,
    MerchantActivityResponse,
    MerchantAssignRequest,
    MerchantIssueBillResponse,
    MerchantManualBillRequest,
    NotificationAnalyticsResponse,
    NotificationDeliverabilityDashboardResponse,
    NotificationItem,
    NotificationPreferenceUpdateRequest,
    NotificationPreferenceView,
    NotificationProviderEventIngestRequest,
    NotificationProcessResult,
    NotificationsResponse,
    PlannerOutput,
    PlannerStep,
    RemindersResponse,
    ReminderView,
    RenewalProviderWebhookRequest,
    RenewalPurchaseIntentResponse,
    RenewalPurchaseRequest,
    RenewalQuoteResponse,
    RenewalOptionsResponse,
    RenewalOptionView,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SharedVaultResponse,
    ServiceCentersRecommendationResponse,
    ServiceCenterView,
    WhatsAppClaimDraftResponse,
    WarrantyItemView,
)
from app.services.date_utils import add_months
from app.services.extraction_pipeline import (
    build_review_fields,
    compute_field_confidences,
    ensure_strict_extraction,
    estimate_claim_readiness,
    estimate_text_quality,
    extraction_fingerprint,
    merge_engine_results,
    prefer_grounded_ocr_fields,
    sanitize_merchandise_name,
)
from app.services.gst_compliance import validate_invoice_compliance
from app.services.notifications import NotificationService
from app.services.qa_logging import create_qa_log
from app.services.rate_limiter import rate_limiter
from app.services.service_centers import ServiceCenterCandidate

try:
    import pytesseract
except Exception:  # pragma: no cover - optional runtime dependency
    pytesseract = None

try:
    from PIL import Image
except Exception:  # pragma: no cover - optional runtime dependency
    Image = None  # type: ignore[assignment]

router = APIRouter(prefix="/api/v1", tags=["safebill-rag"])
_notification_service = NotificationService()
logger = logging.getLogger(__name__)


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Unsupported JSON value: {value!r}")


def _response_model_payload(model: object) -> dict[str, object]:
    if hasattr(model, "model_dump"):
        raw = model.model_dump(mode="json")  # type: ignore[attr-defined]
    elif hasattr(model, "dict"):
        raw = model.dict()  # type: ignore[attr-defined]
    else:
        raw = model
    normalized = json.loads(json.dumps(raw, default=_json_default, ensure_ascii=True))
    return normalized if isinstance(normalized, dict) else {}


def _store_source_blob(
    *,
    services: ServiceRegistry,
    payload: bytes,
    filename: str,
    source: str,
    principal: Principal | None,
    user_id: str | None,
    merchant_user_id: str | None,
) -> dict[str, object]:
    store = getattr(services, "object_store", None)
    if store is None or not getattr(store, "enabled", False):
        return {}
    key = store.build_object_key(filename=filename, source=source)
    content_type = store.guess_content_type(filename)
    metadata = {
        "source": source,
        "filename": filename,
        "principal_role": str(principal.role if principal else ""),
        "principal_subject": str(principal.subject if principal and principal.subject else ""),
        "user_id": str(user_id or ""),
        "merchant_user_id": str(merchant_user_id or ""),
    }
    try:
        uploaded = store.put_bytes(
            key=key,
            payload=payload,
            filename=filename,
            content_type=content_type,
            metadata=metadata,
        )
    except Exception:
        logger.exception("S3 upload failed for source=%s filename=%s", source, filename)
        if getattr(store, "required", False):
            raise HTTPException(status_code=500, detail="S3 upload is required but failed.")
        return {}
    if uploaded:
        return uploaded
    if getattr(store, "required", False):
        raise HTTPException(status_code=500, detail="S3 upload is required but failed.")
    return {}


def _store_ocr_text_snapshot(
    *,
    services: ServiceRegistry,
    extracted_text: str,
    filename: str,
    source: str,
    document_user_id: str | None,
    merchant_user_id: str | None,
) -> dict[str, object]:
    text = str(extracted_text or "").strip()
    if not text:
        return {}
    store = getattr(services, "object_store", None)
    if store is None or not getattr(store, "enabled", False):
        return {}
    stem = Path(filename or "invoice").stem or "invoice"
    snapshot_name = f"{stem}-ocr.txt"
    key = store.build_object_key(filename=snapshot_name, source=f"{source}-ocr")
    try:
        uploaded = store.put_bytes(
            key=key,
            payload=text.encode("utf-8"),
            filename=snapshot_name,
            content_type="text/plain; charset=utf-8",
            metadata={
                "source": source,
                "filename": snapshot_name,
                "user_id": str(document_user_id or ""),
                "merchant_user_id": str(merchant_user_id or ""),
            },
        )
    except Exception:
        logger.exception("Failed to store OCR text snapshot for filename=%s", filename)
        return {}
    if not uploaded:
        return {}
    return {
        "ocr_text_storage_key": str(uploaded.get("storage_key") or ""),
        "ocr_text_storage_bucket": str(uploaded.get("storage_bucket") or ""),
        "ocr_text_storage_region": str(uploaded.get("storage_region") or ""),
        "ocr_text_storage_content_type": str(uploaded.get("storage_content_type") or "text/plain"),
    }


def _load_ocr_text_from_snapshot(
    *,
    services: ServiceRegistry,
    snapshot_references: dict[str, object] | None,
    fallback_text: str,
) -> tuple[str, str]:
    store = getattr(services, "object_store", None)
    key = str((snapshot_references or {}).get("ocr_text_storage_key") or "").strip()
    if store is not None and getattr(store, "enabled", False) and key:
        try:
            payload = store.get_bytes(key=key)
        except Exception:
            logger.exception("Failed to load OCR text snapshot key=%s", key)
            payload = None
        if payload:
            try:
                return payload.decode("utf-8", errors="ignore").strip(), "s3"
            except Exception:
                logger.exception("Failed to decode OCR text snapshot key=%s", key)
    return str(fallback_text or "").strip(), "fallback"


def _classify_document_from_snapshot(
    *,
    services: ServiceRegistry,
    filename: str,
    snapshot_references: dict[str, object] | None,
    fallback_text: str,
) -> tuple[dict[str, object], str, str]:
    ocr_text, text_source = _load_ocr_text_from_snapshot(
        services=services,
        snapshot_references=snapshot_references,
        fallback_text=fallback_text,
    )
    if not ocr_text:
        return {}, text_source, ""
    classification = _classify_document_with_bedrock(ocr_text, filename)
    return classification, text_source, ocr_text


def _enforce_document_text_classification(
    *,
    services: ServiceRegistry,
    filename: str,
    snapshot_references: dict[str, object] | None,
    fallback_text: str,
) -> dict[str, object]:
    classification, text_source, ocr_text = _classify_document_from_snapshot(
        services=services,
        filename=filename,
        snapshot_references=snapshot_references,
        fallback_text=fallback_text,
    )
    if _looks_like_safebill_ui(ocr_text) or _looks_like_ui_screenshot(ocr_text):
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )
    if not classification:
        heuristic_is_invoice, heuristic_confidence = _heuristic_is_invoice_document(ocr_text)
        if not heuristic_is_invoice and heuristic_confidence >= 0.8:
            raise HTTPException(
                status_code=422,
                detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
            )
        return {"document_type_source": text_source}

    doc_is_invoice = classification.get("is_invoice")
    doc_confidence = _coerce_float(classification.get("confidence"), default=0.0) or 0.0
    doc_type = str(classification.get("document_type") or "").strip().lower()
    allowed_types = {"invoice", "receipt", "warranty_card", "guarantee_card"}
    is_allowed_type = doc_type in allowed_types

    if doc_is_invoice is True or is_allowed_type:
        return {
            "document_type": doc_type or None,
            "document_type_confidence": doc_confidence,
            "document_type_reason": classification.get("reason"),
            "document_type_source": text_source,
        }

    if doc_is_invoice is False and doc_confidence >= 0.6:
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )

    if doc_type == "other" and doc_confidence >= 0.6:
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )

    heuristic_is_invoice, heuristic_confidence = _heuristic_is_invoice_document(ocr_text)
    if not heuristic_is_invoice and heuristic_confidence >= 0.8:
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )

    return {
        "document_type": doc_type or None,
        "document_type_confidence": doc_confidence,
        "document_type_reason": classification.get("reason"),
        "document_type_source": text_source,
    }


def _async_extraction_enabled() -> bool:
    settings = get_settings()
    return bool(settings.async_extraction_enabled)


def _local_async_extraction_worker_enabled() -> bool:
    settings = get_settings()
    return bool(settings.async_extraction_enabled and settings.local_async_extraction_worker_enabled)


def _require_async_callback(request: Request) -> None:
    settings = get_settings()
    expected = str(settings.async_extraction_callback_token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Async extraction callback token is not configured.")
    presented = str(request.headers.get("X-Async-Extraction-Token") or "").strip()
    if not presented or presented != expected:
        raise HTTPException(status_code=401, detail="Invalid async extraction callback token.")


def _serialize_async_extraction_job(job: ExtractionJob) -> AsyncExtractionJobStatusResponse:
    engines = job.engines_used if isinstance(job.engines_used, list) else []
    return AsyncExtractionJobStatusResponse(
        jobId=job.id,
        status=str(job.status or "queued"),
        filename=str(job.filename or "uploaded-image"),
        documentId=job.document_id,
        error=(str(job.error_message) if job.error_message else None),
        enginesUsed=[str(engine) for engine in engines],
        createdAt=job.created_at,
        updatedAt=(job.updated_at or job.created_at),
        completedAt=job.completed_at,
    )


def _async_job_in_scope(job: ExtractionJob, *, user_id: str | None, merchant_user_id: str | None) -> bool:
    if user_id and str(job.user_id or "") != user_id:
        return False
    if merchant_user_id and str(job.merchant_user_id or "") != merchant_user_id:
        return False
    return True


def _load_async_job_image_bytes(job: ExtractionJob, *, services: ServiceRegistry) -> bytes | None:
    request_metadata = job.request_metadata if isinstance(job.request_metadata, dict) else {}
    inline_payload = str(request_metadata.get("inline_image_base64") or "").strip()
    if inline_payload:
        try:
            return base64.b64decode(inline_payload.encode("ascii"))
        except Exception:
            logger.exception("Failed to decode inline async extraction payload for job_id=%s", str(job.id))

    store = getattr(services, "object_store", None)
    if store is not None and getattr(store, "enabled", False):
        key = str(job.source_object_key or "").strip()
        if key:
            payload = store.get_bytes(key=key)
            if payload:
                return payload
    return None


def _mark_async_job_failed(
    db: Session,
    job: ExtractionJob,
    *,
    error_message: str,
    engines_used: list[str] | None = None,
    services: ServiceRegistry | None = None,
) -> None:
    job.status = "failed"
    job.error_message = (error_message or "Async extraction failed")[:2000]
    if engines_used is not None:
        job.engines_used = [str(engine) for engine in engines_used]
    job.completed_at = datetime.now(timezone.utc)
    db.add(job)
    db.commit()
    if services is not None:
        _sync_async_extraction_job_mirror(services, job)


def _finalize_async_extraction_job(
    *,
    db: Session,
    services: ServiceRegistry,
    job: ExtractionJob,
    extracted_text: str,
    extracted_metadata: dict[str, object],
    field_confidences: dict[str, float] | None,
    field_sources: dict[str, str] | None,
    low_confidence_fields: list[str] | None,
    engines_used: list[str] | None,
) -> Document:
    request_metadata = job.request_metadata if isinstance(job.request_metadata, dict) else {}
    additional_references: dict[str, object] = {
        "async_extraction_job_id": str(job.id),
        "metadata_source": "async_s3_lambda" if job.source_object_key else "async_local_worker",
    }
    if job.source_bucket:
        additional_references["storage_provider"] = "s3"
        additional_references["storage_bucket"] = str(job.source_bucket)
    if job.source_region:
        additional_references["storage_region"] = str(job.source_region)
    if job.source_object_key:
        additional_references["storage_key"] = str(job.source_object_key)
    if request_metadata.get("merchant_name"):
        additional_references["merchant_name"] = str(request_metadata.get("merchant_name"))
    if request_metadata.get("merchant_custom_id"):
        additional_references["merchant_custom_id"] = str(request_metadata.get("merchant_custom_id"))
    if job.merchant_user_id:
        additional_references["merchant_user_id"] = job.merchant_user_id
    if job.user_id:
        additional_references["user_id"] = job.user_id

    document, _chunk_count = _persist_structured_document(
        db=db,
        services=services,
        filename=job.filename,
        source="image_ocr_async",
        user_id=job.user_id,
        extracted_text=extracted_text,
        extracted_metadata=extracted_metadata,
        bill_id=str(request_metadata.get("bill_id") or "").strip() or None,
        vendor=str(request_metadata.get("vendor") or "").strip() or None,
        document_date=_coerce_date(request_metadata.get("document_date")),
        total_amount=_coerce_float(request_metadata.get("total_amount")),
        field_confidences=dict(field_confidences or {}),
        field_sources={str(key): str(value) for key, value in dict(field_sources or {}).items()},
        low_confidence_fields=[str(field) for field in list(low_confidence_fields or [])],
        extraction_engines=[str(engine) for engine in list(engines_used or [])],
        additional_references=additional_references,
    )
    _schedule_document_notifications(
        db,
        document,
        consumer_user_id=job.user_id,
        consumer_email=str(request_metadata.get("consumer_email") or "").strip() or None,
        consumer_name=str(request_metadata.get("consumer_name") or "").strip() or None,
    )

    job.status = "completed"
    job.document_id = document.id
    job.result_metadata = dict(extracted_metadata or {})
    job.result_text = extracted_text
    job.error_message = None
    job.engines_used = [str(engine) for engine in list(engines_used or [])]
    job.completed_at = datetime.now(timezone.utc)
    db.add(job)
    db.commit()
    db.refresh(job)
    _sync_async_extraction_job_mirror(services, job)
    return document


def process_pending_async_extraction_jobs(
    *,
    db: Session,
    services: ServiceRegistry,
    limit: int | None = None,
) -> dict[str, int]:
    settings = get_settings()
    batch_size = max(1, int(limit or settings.local_async_extraction_batch_size))
    stmt = (
        select(ExtractionJob)
        .where(ExtractionJob.status.in_(("queued", "processing")))
        .order_by(ExtractionJob.created_at.asc())
        .limit(batch_size)
    )
    jobs = db.execute(stmt).scalars().all()
    processed = 0
    completed = 0
    failed = 0

    for job in jobs:
        if str(job.status or "").lower() == "completed":
            continue
        processed += 1
        if str(job.status or "").lower() != "processing":
            job.status = "processing"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            db.add(job)
            db.commit()
            _sync_async_extraction_job_mirror(services, job)

        payload = _load_async_job_image_bytes(job, services=services)
        if not payload:
            failed += 1
            _mark_async_job_failed(
                db,
                job,
                error_message="Async extraction source payload is unavailable.",
                services=services,
            )
            continue
        if _looks_like_person_photo(payload):
            failed += 1
            _mark_async_job_failed(
                db,
                job,
                error_message="Not a bill/invoice. Please upload a valid invoice or warranty card.",
                services=services,
            )
            continue

        request_metadata = job.request_metadata if isinstance(job.request_metadata, dict) else {}
        try:
            routed = _run_image_extraction_router(
                image_bytes=payload,
                filename=job.filename,
                supplied_ocr_text="",
                ocr_mode_override=str(request_metadata.get("ocr_mode") or settings.async_extraction_ocr_mode or "hybrid"),
                bill_id=str(request_metadata.get("bill_id") or "").strip() or None,
                vendor=str(request_metadata.get("vendor") or "").strip() or None,
                document_date=_coerce_date(request_metadata.get("document_date")),
                total_amount=_coerce_float(request_metadata.get("total_amount")),
            )
            extracted_metadata = routed.get("metadata") if isinstance(routed.get("metadata"), dict) else {}
            extracted_text = str(routed.get("resolved_text") or "").strip() or _metadata_to_canonical_text(extracted_metadata)
            _finalize_async_extraction_job(
                db=db,
                services=services,
                job=job,
                extracted_text=extracted_text,
                extracted_metadata=extracted_metadata,
                field_confidences=(routed.get("field_confidences") if isinstance(routed.get("field_confidences"), dict) else {}),
                field_sources=(routed.get("field_sources") if isinstance(routed.get("field_sources"), dict) else {}),
                low_confidence_fields=(routed.get("low_confidence_fields") if isinstance(routed.get("low_confidence_fields"), list) else []),
                engines_used=(routed.get("engines_used") if isinstance(routed.get("engines_used"), list) else []),
            )
            completed += 1
        except Exception as exc:
            logger.exception("Local async extraction failed for job_id=%s", str(job.id))
            failed += 1
            _mark_async_job_failed(
                db,
                job,
                error_message=str(exc),
                engines_used=["local_async_worker"],
                services=services,
            )

    return {
        "processed": processed,
        "completed": completed,
        "failed": failed,
    }


def _cognito_attr_map(user: dict[str, object]) -> dict[str, str]:
    attrs = user.get("Attributes")
    if not isinstance(attrs, list):
        return {}
    mapped: dict[str, str] = {}
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        key = str(attr.get("Name") or "").strip()
        value = str(attr.get("Value") or "").strip()
        if key:
            mapped[key] = value
    return mapped


def _list_cognito_users_by_custom_id(
    *,
    client: object,
    user_pool_id: str,
    custom_id: str,
) -> list[dict[str, object]]:
    escaped = custom_id.replace('"', '\\"')
    filter_expressions = [
        f'custom:custom_id = "{escaped}"',
        f'preferred_username = "{escaped}"',
    ]
    matched_by_filter: list[dict[str, object]] = []
    seen_usernames: set[str] = set()
    for filter_expression in filter_expressions:
        try:
            response = client.list_users(UserPoolId=user_pool_id, Filter=filter_expression, Limit=20)
            users = response.get("Users", [])
            if not isinstance(users, list):
                continue
            for user in users:
                if not isinstance(user, dict):
                    continue
                username = str(user.get("Username") or "").strip()
                if username and username not in seen_usernames:
                    seen_usernames.add(username)
                    matched_by_filter.append(user)
        except Exception:
            continue

    if matched_by_filter:
        return matched_by_filter

    # Fallback for pools where custom-attribute filtering is unavailable.
    matched: list[dict[str, object]] = []
    pagination_token: str | None = None
    for _ in range(20):
        params: dict[str, object] = {"UserPoolId": user_pool_id, "Limit": 60}
        if pagination_token:
            params["PaginationToken"] = pagination_token
        response = client.list_users(**params)
        users = response.get("Users", [])
        if isinstance(users, list):
            for user in users:
                if not isinstance(user, dict):
                    continue
                attrs = _cognito_attr_map(user)
                stored_custom_id = str(
                    attrs.get("custom:custom_id") or attrs.get("preferred_username") or ""
                ).strip().upper()
                if stored_custom_id == custom_id.upper():
                    matched.append(user)
        token_value = response.get("PaginationToken")
        pagination_token = str(token_value).strip() if token_value else None
        if not pagination_token:
            break
    return matched


def _list_cognito_users_by_attribute(
    *,
    client: object,
    user_pool_id: str,
    attribute_name: str,
    attribute_value: str,
    limit: int = 20,
) -> list[dict[str, object]]:
    escaped = str(attribute_value or "").replace('"', '\\"')
    if not escaped:
        return []
    try:
        response = client.list_users(
            UserPoolId=user_pool_id,
            Filter=f'{attribute_name} = "{escaped}"',
            Limit=max(1, min(limit, 60)),
        )
    except Exception:
        return []

    users = response.get("Users", [])
    if not isinstance(users, list):
        return []
    return [user for user in users if isinstance(user, dict)]


def _user_type_from_custom_id(custom_id: str) -> str | None:
    normalized = str(custom_id or "").strip().upper()
    if normalized.startswith("MER-"):
        return "merchant"
    if normalized.startswith("CON-"):
        return "consumer"
    return None


def _should_try_next_cognito_password_flow(exc: Exception) -> bool:
    message = str(exc).strip().lower()
    if not message:
        return False
    retry_markers = (
        "flow not enabled",
        "not enabled for this client",
        "invalid choice for authflow",
        "unknown operation exception",
        "accessdenied",
        "access denied",
        "admininitiateauth",
    )
    return any(marker in message for marker in retry_markers)


def _cognito_password_login_response(
    *,
    client: object,
    user_pool_id: str,
    client_id: str,
    username: str,
    password: str,
    secret_hash: str | None = None,
) -> dict[str, object]:
    auth_parameters = {
        "USERNAME": username,
        "PASSWORD": password,
    }
    if secret_hash:
        auth_parameters["SECRET_HASH"] = secret_hash

    flow_attempts: list[tuple[str, dict[str, object]]] = [
        (
            "admin_initiate_auth",
            {
                "UserPoolId": user_pool_id,
                "ClientId": client_id,
                "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
                "AuthParameters": auth_parameters,
            },
        ),
        (
            "admin_initiate_auth",
            {
                "UserPoolId": user_pool_id,
                "ClientId": client_id,
                "AuthFlow": "ADMIN_NO_SRP_AUTH",
                "AuthParameters": auth_parameters,
            },
        ),
        (
            "initiate_auth",
            {
                "ClientId": client_id,
                "AuthFlow": "USER_PASSWORD_AUTH",
                "AuthParameters": auth_parameters,
            },
        ),
    ]

    last_error: Exception | None = None
    for index, (method_name, params) in enumerate(flow_attempts):
        method = getattr(client, method_name, None)
        if not callable(method):
            continue
        try:
            response = method(**params)
        except Exception as exc:
            last_error = exc
            should_retry = index < len(flow_attempts) - 1 and _should_try_next_cognito_password_flow(exc)
            if should_retry:
                continue
            raise
        if isinstance(response, dict):
            return response

    if last_error is not None:
        raise last_error
    raise RuntimeError("Cognito password login is unavailable")


def _safe_session_commit(db: Session) -> None:
    if hasattr(db, "commit"):
        db.commit()


def _resolve_client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip()[:128]
    if request.client and request.client.host:
        return str(request.client.host)[:128]
    return None


def _log_security_event(
    db: Session,
    *,
    event_type: str,
    principal: Principal | None,
    resource: str | None,
    request: Request | None,
    metadata: dict[str, object] | None = None,
) -> None:
    safe_metadata = metadata or {}
    try:
        entry = SecurityAuditLog(
            event_type=event_type[:64],
            actor_role=(principal.role[:64] if principal else None),
            user_id=(principal.subject[:128] if principal and principal.subject else None),
            resource=(resource[:255] if resource else None),
            client_ip=_resolve_client_ip(request),
            event_metadata=safe_metadata,
        )
        db.add(entry)
        _safe_session_commit(db)
    except Exception:
        logger.exception("Failed to write security audit event=%s", event_type)
        if hasattr(db, "rollback"):
            try:
                db.rollback()
            except Exception:
                pass


def _rate_limit_or_429(
    *,
    request: Request | None,
    principal: Principal,
    bucket: str,
    limit: int,
) -> None:
    settings = get_settings()
    window_seconds = max(1, int(settings.api_rate_limit_window_seconds))
    identity = principal.subject or _resolve_client_ip(request) or "anonymous"
    allowed, retry_after = rate_limiter.allow(
        bucket=bucket,
        key=identity,
        limit=max(1, limit),
        window_seconds=window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Too many requests. Please retry shortly.",
                "bucket": bucket,
                "retry_after_seconds": retry_after,
            },
        )


@router.post("/auth/lookup-id")
def lookup_user_by_custom_id(payload: dict[str, object]) -> dict[str, object]:
    settings = get_settings()
    custom_id = str(payload.get("customId") or "").strip().upper()
    requested_type = str(payload.get("userType") or "").strip().lower()
    if not custom_id or requested_type not in {"consumer", "merchant"}:
        raise HTTPException(status_code=400, detail="customId and valid userType are required")

    if not settings.cognito_user_pool_id:
        raise HTTPException(status_code=500, detail="COGNITO_USER_POOL_ID is not configured")
    if boto3 is None:
        raise HTTPException(status_code=500, detail="boto3 dependency is unavailable")

    try:
        client = boto3.client("cognito-idp", region_name=settings.aws_region)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to initialize Cognito client: {exc}") from exc

    try:
        users = _list_cognito_users_by_custom_id(
            client=client,
            user_pool_id=settings.cognito_user_pool_id,
            custom_id=custom_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cognito lookup failed: {exc}") from exc

    for user in users:
        attrs = _cognito_attr_map(user)
        resolved_custom_id = str(
            attrs.get("custom:custom_id") or attrs.get("preferred_username") or custom_id
        ).strip().upper()
        discovered_type = str(attrs.get("custom:user_type") or "").strip().lower()
        if discovered_type not in {"consumer", "merchant"}:
            discovered_type = _user_type_from_custom_id(resolved_custom_id) or ""
        if discovered_type and discovered_type != requested_type:
            continue

        email = str(attrs.get("email") or "").strip().lower()
        phone = str(attrs.get("phone_number") or "").strip()
        username = str(user.get("Username") or "").strip()
        if not username:
            continue
        full_name = str(attrs.get("name") or attrs.get("given_name") or "").strip()
        user_sub = str(attrs.get("sub") or "").strip()
        resolved_type = discovered_type if discovered_type in {"consumer", "merchant"} else requested_type

        return {
            "userId": user_sub,
            "username": username,
            "email": email,
            "phone": phone,
            "fullName": full_name,
            "userType": resolved_type,
            "customId": resolved_custom_id,
        }

    raise HTTPException(status_code=404, detail="Account not found")


@router.post("/auth/recover-id")
def recover_custom_id_via_email(payload: dict[str, object]) -> dict[str, object]:
    settings = get_settings()
    email = str(payload.get("email") or payload.get("identifier") or "").strip().lower()
    requested_type = str(payload.get("userType") or "").strip().lower()
    account_label = "Merchant ID" if requested_type == "merchant" else "Consumer ID"

    if "@" not in email or requested_type not in {"consumer", "merchant"}:
        raise HTTPException(status_code=400, detail="email and valid userType are required")

    generic_response = {
        "ok": True,
        "deliveryDestination": email,
        "deliveryMedium": "EMAIL",
        "message": f"If an account exists, your {account_label} has been emailed.",
    }

    if not settings.cognito_user_pool_id:
        raise HTTPException(status_code=500, detail="COGNITO_USER_POOL_ID is not configured")
    if boto3 is None:
        raise HTTPException(status_code=500, detail="boto3 dependency is unavailable")

    try:
        client = boto3.client("cognito-idp", region_name=settings.aws_region)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to initialize Cognito client: {exc}") from exc

    try:
        users = _list_cognito_users_by_attribute(
            client=client,
            user_pool_id=settings.cognito_user_pool_id,
            attribute_name="email",
            attribute_value=email,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cognito lookup failed: {exc}") from exc

    matched_user: dict[str, object] | None = None
    resolved_custom_id = ""
    full_name = ""

    for user in users:
        attrs = _cognito_attr_map(user)
        resolved_email = str(attrs.get("email") or "").strip().lower()
        resolved_custom_id = str(
            attrs.get("custom:custom_id") or attrs.get("preferred_username") or ""
        ).strip().upper()
        if not resolved_email or resolved_email != email or not resolved_custom_id:
            continue

        discovered_type = str(attrs.get("custom:user_type") or "").strip().lower()
        if discovered_type not in {"consumer", "merchant"}:
            discovered_type = _user_type_from_custom_id(resolved_custom_id) or ""
        if discovered_type != requested_type:
            continue

        matched_user = user
        full_name = str(attrs.get("name") or attrs.get("given_name") or "").strip()
        break

    if matched_user is None or not resolved_custom_id:
        return generic_response

    salutation = full_name or ("Merchant" if requested_type == "merchant" else "Consumer")
    subject = f"Your SafeBill {account_label}"
    body = "\n".join(
        [
            f"Hello {salutation},",
            "",
            f"Your SafeBill {account_label} is: {resolved_custom_id}",
            "",
            "Use this ID with your password to sign in to SafeBill.",
            "If you did not request this email, you can ignore it.",
            "",
            "SafeBill",
        ]
    )

    try:
        _notification_service._send_email(
            recipient_email=email,
            subject=subject,
            body=body,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to send recovery email: {exc}") from exc

    return generic_response


@router.post("/auth/cognito/login")
def cognito_password_login(payload: dict[str, object]) -> dict[str, object]:
    settings = get_settings()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    secret_hash = str(payload.get("secretHash") or "").strip() or None

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")
    if not settings.cognito_user_pool_id:
        raise HTTPException(status_code=500, detail="COGNITO_USER_POOL_ID is not configured")
    if not settings.cognito_app_client_id:
        raise HTTPException(status_code=500, detail="COGNITO_APP_CLIENT_ID is not configured")
    if boto3 is None:
        raise HTTPException(status_code=500, detail="boto3 dependency is unavailable")

    try:
        client = boto3.client("cognito-idp", region_name=settings.aws_region)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to initialize Cognito client: {exc}") from exc

    try:
        response = _cognito_password_login_response(
            client=client,
            user_pool_id=settings.cognito_user_pool_id,
            client_id=settings.cognito_app_client_id,
            username=username,
            password=password,
            secret_hash=secret_hash,
        )
    except Exception as exc:
        detail = str(exc).strip() or "Cognito login failed"
        lowered = detail.lower()
        if "not authorized" in lowered or "incorrect username or password" in lowered:
            raise HTTPException(status_code=401, detail=detail) from exc
        if "user is not confirmed" in lowered or "usernotconfirmedexception" in lowered:
            raise HTTPException(status_code=400, detail=detail) from exc
        if "password reset required" in lowered:
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=500, detail=detail) from exc

    auth_result = response.get("AuthenticationResult")
    challenge_name = str(response.get("ChallengeName") or "").strip() or None
    session = str(response.get("Session") or "").strip() or None
    auth_result_map = auth_result if isinstance(auth_result, dict) else {}

    return {
        "accessToken": str(auth_result_map.get("AccessToken") or "").strip() or None,
        "idToken": str(auth_result_map.get("IdToken") or "").strip() or None,
        "refreshToken": str(auth_result_map.get("RefreshToken") or "").strip() or None,
        "expiresIn": auth_result_map.get("ExpiresIn"),
        "challengeName": challenge_name,
        "session": session,
    }


def _coerce_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _coerce_int(value: object, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return bool(value)


def _looks_like_non_merchandise_item_name(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    compact = re.sub(r"\s+", " ", text)
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,62}\.[a-z]{2,6}", compact):
        return True
    blocked_tokens = (
        "customer number",
        "customer no",
        "customer id",
        "document number",
        "invoice number",
        "tax invoice number",
        "order number",
        "po number",
        "purchase order number",
        "ship to",
        "bill to",
        "place of supply",
        "gstin",
        "pan",
        "address",
        "pincode",
        "postal code",
        "phone",
        "email",
        "tax rate",
        "item number",
        "hsn",
        "amount in words",
        "total amount",
        "tax amount",
        "taxable amount",
        "gst amount",
        "igst",
        "cgst",
        "sgst",
        "total",
        "subtotal",
        "discount",
        "shipping charges",
        "delivery charges",
        "handling charges",
        "packaging charges",
    )
    if any(token in compact for token in blocked_tokens):
        return True
    return bool(
        re.fullmatch(r"(?:customer|invoice|document|order|po|gst|pan|hsn|item)\s*(?:no|number|id|code)", compact)
    )


def _first_meaningful_line(text: str, fallback: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) < 4:
            continue
        if len(line) > 120:
            continue
        if re.fullmatch(r"[\W_]+", line):
            continue
        if _looks_like_non_merchandise_item_name(line):
            continue
        return line
    return fallback


def _ocr_image_bytes(image_bytes: bytes) -> str:
    if not image_bytes:
        return ""

    google_text = _extract_text_with_google_vision(image_bytes)
    if google_text:
        return google_text

    textract_text = _extract_text_with_textract(image_bytes)
    if textract_text:
        return textract_text

    # Gemini Vision fallback: extract raw text from image when other cloud OCR is unavailable.
    try:
        from app.services.gemini_client import gemini_ocr_image
        gemini_text = gemini_ocr_image(image_bytes, "ocr_image.png")
        if gemini_text:
            return gemini_text
    except Exception:
        pass

    if Image is None or pytesseract is None:
        return ""

    settings = get_settings()
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            return (pytesseract.image_to_string(image) or "").strip()
    except Exception:
        return ""


def _summarize_exception_message(error: Exception, max_len: int = 160) -> str:
    name = error.__class__.__name__
    text = str(error).strip().replace("\n", " ")
    if len(text) > max_len:
        text = text[: max_len - 3].rstrip() + "..."
    return f"{name}: {text}" if text else name


def _build_image_ocr_diagnostics(image_bytes: bytes) -> str:
    settings = get_settings()
    diagnostics: list[str] = ["diag_version=google_vision_v1"]

    tesseract_path = ""
    configured_cmd = (settings.tesseract_cmd or "").strip()
    if configured_cmd:
        tesseract_path = configured_cmd if os.path.exists(configured_cmd) else ""
    if not tesseract_path:
        tesseract_path = shutil.which("tesseract") or ""

    if Image is None or pytesseract is None:
        diagnostics.append("local_ocr=unavailable (missing PIL/pytesseract)")
    elif not tesseract_path:
        diagnostics.append("local_ocr=unavailable (tesseract binary not found)")
    else:
        diagnostics.append(f"local_ocr=ready ({tesseract_path})")

    if httpx is None:
        diagnostics.append("google_vision=unavailable (httpx missing)")
    else:
        auth_headers, params, auth_mode = _google_vision_auth_context()
        if auth_mode == "none":
            diagnostics.append("google_vision=unconfigured (set GOOGLE_VISION_API_KEY or GOOGLE_VISION_CREDENTIALS_FILE)")
        elif auth_mode == "service_account" and (service_account is None or GoogleAuthRequest is None):
            diagnostics.append("google_vision=unavailable (google-auth missing)")
        else:
            payload = {
                "requests": [
                    {
                        "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION", "maxResults": 1}],
                    }
                ]
            }
            endpoint = (settings.google_vision_endpoint or "").strip() or "https://vision.googleapis.com/v1/images:annotate"
            try:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(endpoint, params=params, headers=auth_headers, json=payload)
                    response.raise_for_status()
                    parsed = response.json()
                responses = parsed.get("responses", []) if isinstance(parsed, dict) else []
                first = responses[0] if isinstance(responses, list) and responses else {}
                if isinstance(first, dict) and isinstance(first.get("error"), dict):
                    details = str(first.get("error", {}).get("message") or "unknown error")
                    diagnostics.append(f"google_vision=error ({details[:140]})")
                else:
                    diagnostics.append(f"google_vision=ok ({auth_mode})")
            except Exception as exc:
                diagnostics.append(f"google_vision=error ({_summarize_exception_message(exc)})")

    if boto3 is None:
        diagnostics.append("aws_sdk=unavailable (boto3 missing)")
        return "; ".join(diagnostics)

    try:
        textract_client = boto3.client("textract", region_name=settings.aws_region)
        textract_client.detect_document_text(Document={"Bytes": image_bytes})
        diagnostics.append("textract=ok")
    except Exception as exc:
        diagnostics.append(f"textract=error ({_summarize_exception_message(exc)})")

    model = (settings.bedrock_chat_model or "").strip()
    if not model:
        diagnostics.append("bedrock=unconfigured (BEDROCK_CHAT_MODEL empty)")
    else:
        try:
            configure_bedrock_api_key(settings)
            bedrock_client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
            bedrock_client.converse(
                modelId=model,
                system=[{"text": "Return compact JSON only."}],
                messages=[{"role": "user", "content": [{"text": "Respond with {\"ok\":true}."}]}],
                inferenceConfig={"temperature": 0.0, "maxTokens": 64},
            )
            diagnostics.append("bedrock=ok")
        except Exception as exc:
            diagnostics.append(f"bedrock=error ({_summarize_exception_message(exc)})")

    # Gemini Vision diagnostic
    settings_gemini = get_settings()
    gemini_key = (settings_gemini.gemini_api_key or "").strip()
    if gemini_key:
        try:
            from app.services.gemini_client import get_last_gemini_error
            last_err = get_last_gemini_error()
            if "429" in last_err or "quota" in last_err.lower():
                diagnostics.append(f"gemini_vision=error (Quota Exceeded / HTTP 429 - get free key at https://aistudio.google.com/apikey)")
            elif last_err:
                diagnostics.append(f"gemini_vision=error ({last_err[:120]})")
            else:
                diagnostics.append(f"gemini_vision=ready (model={settings_gemini.gemini_model})")
        except Exception:
            diagnostics.append(f"gemini_vision=ready (model={settings_gemini.gemini_model})")
    else:
        diagnostics.append("gemini_vision=unconfigured (set GEMINI_API_KEY in .env)")

    return "; ".join(diagnostics)


def _looks_like_ui_screenshot(text: str) -> bool:
    lowered = (text or "").lower()
    strong_markers = [
        "warranty command center",
        "scan invoice",
        "assets in locker",
        "protected value",
        "expiring soon",
        "all assets",
        "warranty locker",
        "dashboard",
    ]
    if any(marker in lowered for marker in strong_markers):
        return True
    ui_markers = [
        "merchant dashboard",
        "consumer sync",
        "assign uploaded bill",
        "generate manual bill",
        "digital locker",
        "all warranties",
        "scan invoice",
        "warranty command center",
        "open full ai report",
        "extraction complete",
        "personal outputs",
        "settings",
        "logout",
    ]
    hits = sum(1 for marker in ui_markers if marker in lowered)
    return hits >= 2


def _looks_like_safebill_ui(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    if "warranty command center" in lowered:
        return True
    if "safebill" in lowered:
        secondary_markers = (
            "dashboard",
            "scan invoice",
            "all assets",
            "expiring soon",
            "claims",
            "vendors",
            "analytics",
            "assets in locker",
            "warranty locker",
        )
        return any(marker in lowered for marker in secondary_markers)
    return False


def _metadata_looks_like_ui(metadata: dict[str, object]) -> bool:
    if not isinstance(metadata, dict):
        return False
    text_parts: list[str] = []
    for key in ("bill_id", "vendor", "product_name", "category"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            text_parts.append(value.strip())
    line_items = metadata.get("line_items")
    if isinstance(line_items, list):
        for item in line_items:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    text_parts.append(name.strip())
    combined = " ".join(text_parts).lower()
    if not combined:
        return False
    ui_tokens = (
        "dashboard",
        "warranty command center",
        "scan invoice",
        "expiring soon",
        "assets in locker",
        "protected value",
        "all assets",
        "warranty locker",
        "safebill",
        "safe bill",
    )
    return any(token in combined for token in ui_tokens)


def _normalize_identifier_value(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper()).strip()


def _is_plausible_identifier(value: str) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return False
    if len(candidate) < 4 or len(candidate) > 64:
        return False
    if not any(ch.isdigit() for ch in candidate):
        return False
    lowered = candidate.lower()
    if lowered in {"na", "n/a", "none", "unknown", "nil"}:
        return False
    return True


def _extract_document_identifiers(metadata: dict[str, object], raw_text: str) -> dict[str, object]:
    invoice_number = str(metadata.get("bill_id") or "").strip()
    serial_number = str(metadata.get("serial_number") or "").strip()
    order_number = ""
    warranty_number = ""

    pattern = re.compile(
        r"(?im)\b(?P<label>invoice|inv|bill|receipt|order|po|purchase\s*order|warranty|guarantee|serial|s\/n|imei)"
        r"\s*(?:no|number|#|id)?\s*[:\-]?\s*(?P<value>[A-Z0-9][A-Z0-9\/\\-]{3,64})"
    )
    for match in pattern.finditer(raw_text or ""):
        label = str(match.group("label") or "").strip().lower()
        value = str(match.group("value") or "").strip()
        if not _is_plausible_identifier(value):
            continue
        if label in {"invoice", "inv", "bill", "receipt"} and not invoice_number:
            invoice_number = value
        elif label in {"order", "po", "purchase order"} and not order_number:
            order_number = value
        elif label in {"warranty", "guarantee"} and not warranty_number:
            warranty_number = value
        elif label in {"serial", "s/n", "imei"} and not serial_number:
            serial_number = value

    raw_candidates = [invoice_number, order_number, warranty_number, serial_number]
    raw_candidates = [value for value in raw_candidates if _is_plausible_identifier(value)]
    normalized = [_normalize_identifier_value(value) for value in raw_candidates]
    normalized = [value for value in normalized if _is_plausible_identifier(value)]

    return {
        "invoice_number": invoice_number if _is_plausible_identifier(invoice_number) else None,
        "order_number": order_number if _is_plausible_identifier(order_number) else None,
        "warranty_number": warranty_number if _is_plausible_identifier(warranty_number) else None,
        "serial_number": serial_number if _is_plausible_identifier(serial_number) else None,
        "identifiers_raw": raw_candidates[:8],
        "identifiers_norm": normalized[:8],
    }


def _heuristic_is_invoice_document(text: str) -> tuple[bool, float]:
    cleaned = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not cleaned:
        return False, 0.3
    if len(cleaned) < 30:
        return False, 0.4

    ui_tokens = (
        "warranty command center",
        "scan invoice",
        "assets in locker",
        "protected value",
        "expiring soon",
        "all assets",
        "warranty locker",
        "dashboard",
        "safebill",
        "safe bill",
    )
    if any(token in cleaned for token in ui_tokens):
        return False, 0.95

    strong_tokens = (
        "invoice",
        "tax invoice",
        "bill of supply",
        "cash memo",
        "receipt",
        "gstin",
        "gst",
        "cgst",
        "sgst",
        "igst",
        "hsn",
        "pan",
        "invoice no",
        "invoice number",
        "bill no",
        "order number",
        "total amount",
        "grand total",
        "amount due",
        "bill to",
        "ship to",
        "place of supply",
        "warranty",
        "guarantee",
        "warranty card",
        "guarantee card",
    )
    hits = sum(1 for token in strong_tokens if token in cleaned)

    if "warranty" in cleaned or "guarantee" in cleaned:
        return True, 0.7 if hits >= 1 else 0.6
    if hits >= 3:
        return True, 0.75
    if hits >= 2 and len(cleaned) >= 120:
        return True, 0.6

    if hits == 0 and len(cleaned) >= 120:
        return False, 0.8
    if hits == 0 and len(cleaned) >= 80:
        return False, 0.7
    return False, 0.5


def _classify_document_with_bedrock(ocr_text: str, filename: str) -> dict[str, object]:
    settings = get_settings()
    if not ocr_text:
        return {}
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {}

    model = (settings.bedrock_chat_model or "").strip()
    if boto3 is None or not model:
        # Fallback to Gemini for text-based classification.
        try:
            from app.services.gemini_client import gemini_extract_json
            prompt = (
                f"Classify this document text. Is it a bill/invoice/receipt or warranty card?\n"
                f"filename={filename}\nocr_text:\n{ocr_text[:8000]}\n\n"
                f"Return JSON with keys: is_invoice (boolean), document_type (string: invoice/receipt/warranty_card/guarantee_card/other), "
                f"confidence (0-1), reason (short string)."
            )
            parsed = gemini_extract_json(prompt)
            if isinstance(parsed, dict):
                raw_is = parsed.get("is_invoice")
                is_inv = None
                if isinstance(raw_is, bool):
                    is_inv = raw_is
                elif isinstance(raw_is, str):
                    low = raw_is.strip().lower()
                    is_inv = True if low in {"true", "1", "yes"} else (False if low in {"false", "0", "no"} else None)
                conf = _coerce_float(parsed.get("confidence"), default=0.0) or 0.0
                return {
                    "is_invoice": is_inv,
                    "document_type": str(parsed.get("document_type") or "").strip() or None,
                    "confidence": max(0.0, min(conf, 1.0)),
                    "reason": str(parsed.get("reason") or "").strip() or None,
                }
        except Exception:
            pass
        return {}

    system_prompt = (
        "You are a document classifier for SafeBill. "
        "Decide whether the text represents a bill/invoice/receipt or a warranty/guarantee card. "
        "Return only JSON with keys: is_invoice (boolean), document_type (string), confidence (0-1), reason (short). "
        "Acceptable document_type values: invoice, receipt, warranty_card, guarantee_card, other. "
        "If evidence is weak or unclear, set is_invoice to false with low confidence."
    )
    user_payload = f"filename={filename}\nocr_text:\n{ocr_text[:16000]}"

    try:
        configure_bedrock_api_key(settings)
        client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        response = client.converse(
            modelId=model,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_payload}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": 300},
        )
        content_blocks = (
            response.get("output", {})
            .get("message", {})
            .get("content", [])
        )
        raw_content = "".join(
            str(block.get("text", ""))
            for block in content_blocks
            if isinstance(block, dict)
        ).strip()
        if not raw_content:
            return {}
        if raw_content.startswith("```"):
            raw_content = raw_content.strip("`")
            raw_content = raw_content.replace("json\n", "", 1).strip()
        parsed = json.loads(raw_content)
    except Exception:
        return {}

    if not isinstance(parsed, dict):
        return {}

    raw_is_invoice = parsed.get("is_invoice")
    is_invoice: bool | None = None
    if isinstance(raw_is_invoice, bool):
        is_invoice = raw_is_invoice
    elif isinstance(raw_is_invoice, str):
        lowered = raw_is_invoice.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            is_invoice = True
        elif lowered in {"false", "0", "no", "n"}:
            is_invoice = False

    confidence = _coerce_float(parsed.get("confidence"), default=0.0) or 0.0
    if confidence < 0:
        confidence = 0.0
    if confidence > 1:
        confidence = 1.0

    return {
        "is_invoice": is_invoice,
        "document_type": str(parsed.get("document_type") or "").strip() or None,
        "confidence": confidence,
        "reason": str(parsed.get("reason") or "").strip() or None,
    }


def _is_meaningful_metadata_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _filename_stem(filename: str) -> str:
    cleaned = str(filename or "").strip()
    if not cleaned:
        return ""
    stem, _, _suffix = cleaned.rpartition(".")
    return (stem or cleaned).strip()


def _should_apply_bill_id_hint(current_value: object, *, filename: str) -> bool:
    current = str(current_value or "").strip()
    if not current:
        return True
    lowered = current.lower()
    if any(bad in lowered for bad in ("whatsapp image", "screenshot", "screen shot", "img_", "dsc_", "uploaded document")):
        return True
    stem = _filename_stem(filename)
    if stem and lowered == stem.lower():
        return True
    if current.upper().startswith(("IMAGE_OCR_", "IMAGE_OCR-", "INGEST_PDF-", "INGEST_IMAGE-")):
        return True
    return False


def _should_apply_vendor_hint(current_value: object) -> bool:
    current = str(current_value or "").strip()
    return not current or current.upper() == "UNKNOWN_VENDOR"


def _should_apply_date_hint(current_value: object) -> bool:
    return _coerce_date(current_value) is None


def _should_apply_total_hint(current_value: object) -> bool:
    current = _coerce_float(current_value)
    return current is None or current <= 0


def _apply_invoice_request_hints(
    metadata: dict[str, object],
    *,
    filename: str,
    authoritative: bool,
    bill_id: str | None,
    vendor: str | None,
    document_date: date | None,
    total_amount: float | None,
) -> dict[str, object]:
    resolved = dict(metadata)

    bill_id_hint = str(bill_id or "").strip()[:128]
    if bill_id_hint and (authoritative or _should_apply_bill_id_hint(resolved.get("bill_id"), filename=filename)):
        resolved["bill_id"] = bill_id_hint

    vendor_hint = str(vendor or "").strip()[:255]
    if vendor_hint and (authoritative or _should_apply_vendor_hint(resolved.get("vendor"))):
        resolved["vendor"] = vendor_hint

    if document_date and (authoritative or _should_apply_date_hint(resolved.get("date"))):
        resolved["date"] = document_date.isoformat()

    if total_amount is not None and (authoritative or _should_apply_total_hint(resolved.get("total_amount"))):
        resolved["total_amount"] = total_amount

    return resolved


def _merge_invoice_metadata(
    preferred: dict[str, object] | None,
    fallback: dict[str, object],
) -> dict[str, object]:
    merged = dict(fallback)
    if not preferred:
        return merged

    fallback_bill_id = str(fallback.get("bill_id") or "").strip()
    fallback_total = _coerce_float(fallback.get("total_amount"))
    fallback_product = str(sanitize_merchandise_name(fallback.get("product_name")) or "").strip()
    fallback_line_items = fallback.get("line_items") if isinstance(fallback.get("line_items"), list) else []

    for key, value in preferred.items():
        if not _is_meaningful_metadata_value(value):
            continue
        if key == "bill_id":
            preferred_bill_id = str(value).strip()[:128]
            if not preferred_bill_id:
                continue
            if fallback_bill_id:
                normalized_preferred = preferred_bill_id.upper()
                normalized_fallback = fallback_bill_id.upper()
                if normalized_preferred.startswith(normalized_fallback):
                    suffix = normalized_preferred[len(normalized_fallback):].strip()
                    if suffix and re.fullmatch(r"[-/][A-Z0-9]{3,}", suffix):
                        continue
            merged[key] = preferred_bill_id
            continue
        if key == "product_name":
            preferred_product = str(sanitize_merchandise_name(value) or "").strip()
            if not preferred_product and fallback_product:
                continue
            if preferred_product:
                merged[key] = preferred_product
            continue
        if key == "total_amount":
            preferred_total = _coerce_float(value)
            if preferred_total is None:
                continue
            if fallback_total is not None and fallback_total > 0:
                if preferred_total > fallback_total * 2.5 or preferred_total < fallback_total * 0.4:
                    continue
            merged[key] = preferred_total
            continue
        if key == "line_items" and isinstance(value, list):
            preferred_items = [item for item in value if isinstance(item, dict)]
            if fallback_line_items and len(preferred_items) < len(fallback_line_items):
                continue
        merged[key] = value
    return merged


def _normalize_invoice_metadata(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, object] = {}
    textual_keys = {
        "bill_id",
        "vendor",
        "date",
        "vendor_tax_id",
        "product_name",
        "brand",
        "serial_number",
        "warranty_start",
        "warranty_end",
        "category",
    }
    numeric_keys = {
        "total_amount",
        "taxable_amount",
        "gst_amount",
        "gst_rate",
        "cgst_amount",
        "sgst_amount",
        "igst_amount",
    }

    for key in textual_keys:
        value = raw.get(key)
        if value is None:
            continue
        if key == "product_name":
            text = str(sanitize_merchandise_name(value) or "").strip()
        else:
            text = str(value).strip()
        if text:
            normalized[key] = text

    for key in numeric_keys:
        value = _coerce_float(raw.get(key))
        if value is not None:
            normalized[key] = value

    warranty_months = _coerce_int(raw.get("warranty_months"))
    if warranty_months is not None and warranty_months > 0:
        normalized["warranty_months"] = warranty_months

    line_items_value = raw.get("line_items")
    if isinstance(line_items_value, list):
        filtered_items: list[dict[str, object]] = []
        for item in line_items_value:
            if not isinstance(item, dict):
                continue
            name = str(sanitize_merchandise_name(item.get("name")) or "").strip()
            amount = _coerce_float(item.get("amount"))
            quantity = _coerce_float(item.get("quantity"))
            unit_price = _coerce_float(item.get("unit_price"))
            normalized_item: dict[str, object] = {}
            if name:
                normalized_item["name"] = name[:255]
            if amount is not None:
                normalized_item["amount"] = amount
            if quantity is not None:
                normalized_item["quantity"] = quantity
            if unit_price is not None:
                normalized_item["unit_price"] = unit_price
            if normalized_item:
                filtered_items.append(normalized_item)
        if filtered_items:
            normalized["line_items"] = filtered_items[:50]

    return normalized


def _metadata_to_canonical_text(metadata: dict[str, object]) -> str:
    lines: list[str] = []
    bill_id = str(metadata.get("bill_id") or "").strip()
    vendor = str(metadata.get("vendor") or "").strip()
    invoice_date = str(metadata.get("date") or "").strip()
    total_amount = _coerce_float(metadata.get("total_amount"))
    product_name = str(sanitize_merchandise_name(metadata.get("product_name")) or metadata.get("product_name") or "").strip()
    vendor_tax_id = str(metadata.get("vendor_tax_id") or "").strip()

    if bill_id:
        lines.append(f"Invoice Number: {bill_id}")
    if vendor:
        lines.append(f"Vendor: {vendor}")
    if invoice_date:
        lines.append(f"Invoice Date: {invoice_date}")
    if total_amount is not None:
        lines.append(f"Total Amount: INR {total_amount:.2f}")
    if product_name:
        lines.append(f"Product Name: {product_name}")
    if vendor_tax_id:
        lines.append(f"GST Registration No: {vendor_tax_id}")

    line_items = metadata.get("line_items")
    if isinstance(line_items, list) and line_items:
        lines.append("Line Items:")
        for item in line_items[:20]:
            if not isinstance(item, dict):
                continue
            name = str(sanitize_merchandise_name(item.get("name")) or item.get("name") or "").strip()
            amount = _coerce_float(item.get("amount"))
            if not name and amount is None:
                continue
            if amount is None:
                lines.append(f"- {name}")
            else:
                lines.append(f"- {name or 'Item'}: INR {amount:.2f}")

    return "\n".join(lines).strip()


def _normalize_locker_category(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None

    direct = {
        "gadgets": "Gadgets",
        "gadget": "Gadgets",
        "electronics": "Gadgets",
        "electronic": "Gadgets",
        "appliances": "Appliances",
        "appliance": "Appliances",
        "home appliance": "Appliances",
        "home appliances": "Appliances",
        "vehicle": "Vehicle",
        "vehicles": "Vehicle",
        "automotive": "Vehicle",
        "others": "Others",
        "other": "Others",
    }
    if raw in direct:
        return direct[raw]
    return None


def _infer_locker_category(
    *,
    product_name: str | None,
    brand: str | None,
    vendor: str | None,
    line_items: list[dict[str, object]] | None,
    source_category: object = None,
) -> str:
    normalized_source = _normalize_locker_category(source_category)
    if normalized_source and normalized_source != "Others":
        return normalized_source

    combined_parts = [product_name or "", brand or "", vendor or ""]
    if line_items:
        for item in line_items[:30]:
            if not isinstance(item, dict):
                continue
            combined_parts.append(str(item.get("name") or ""))
    combined = " ".join(combined_parts).lower()
    combined = re.sub(r"\s+", " ", combined).strip()

    vehicle_tokens = (
        "car",
        "bike",
        "scooter",
        "motorcycle",
        "vehicle",
        "automotive",
        "tractor",
        "tyre",
        "helmet",
    )
    if any(token in combined for token in vehicle_tokens):
        return "Vehicle"

    appliance_tokens = (
        "refrigerator",
        "fridge",
        "washing machine",
        "microwave",
        "oven",
        "air conditioner",
        "airconditioner",
        "ac ",
        "geyser",
        "dishwasher",
        "television",
        "smart tv",
        "tv ",
        "vacuum",
        "water purifier",
        "chimney",
        "appliance",
    )
    if any(token in combined for token in appliance_tokens):
        return "Appliances"

    gadget_tokens = (
        "phone",
        "mobile",
        "smartphone",
        "iphone",
        "pixel",
        "tablet",
        "ipad",
        "laptop",
        "notebook",
        "ultrabook",
        "macbook",
        "desktop",
        "monitor",
        "camera",
        "dslr",
        "headphone",
        "earbud",
        "watch",
        "smartwatch",
        "printer",
        "router",
        "ssd",
        "hdd",
        "gpu",
        "processor",
        "hsn:8517",
    )
    gadget_brands = (
        "nokia",
        "samsung",
        "apple",
        "oneplus",
        "xiaomi",
        "redmi",
        "realme",
        "oppo",
        "vivo",
        "motorola",
        "google",
        "sony",
        "lenovo",
        "dell",
        "hp",
        "asus",
        "acer",
        "msi",
        "canon",
        "nikon",
        "boat",
        "jbl",
        "logitech",
    )
    if any(token in combined for token in gadget_tokens) or any(brand_token in combined for brand_token in gadget_brands):
        return "Gadgets"

    return "Others"


def _extract_image_metadata_with_bedrock(image_bytes: bytes, filename: str) -> dict[str, object]:
    settings = get_settings()
    if not image_bytes:
        return {}
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {}

    model = (settings.bedrock_chat_model or "").strip()
    if boto3 is None or not model:
        # Fallback to Gemini Vision for image metadata extraction.
        try:
            gemini_result = gemini_extract_image_metadata(image_bytes, filename)
            if gemini_result and isinstance(gemini_result, dict):
                return _normalize_invoice_metadata(gemini_result)
        except Exception:
            pass
        return {}
    mime_type = mimetypes.guess_type(filename)[0] or "image/png"
    image_format = "png"
    if "jpeg" in mime_type or "jpg" in mime_type:
        image_format = "jpeg"
    elif "webp" in mime_type:
        image_format = "webp"
    elif "gif" in mime_type:
        image_format = "gif"

    system_prompt = (
        "You are an invoice data extraction engine. "
        "Return only JSON. Do not guess missing values. Use null for missing fields. "
        "Dates must be ISO 8601 format (YYYY-MM-DD). Convert from formats like 10-Feb-2026 if present. "
        "Only set monetary fields when they are explicitly shown as money (currency symbol/code or labels like TOTAL/AMOUNT/MRP/PRICE). "
        "Never treat product dimensions (e.g., '42-inch'), model numbers, serial numbers, warranty months, phone numbers, or addresses as amounts. "
        "Extract these keys exactly: "
        "bill_id, vendor, date, total_amount, vendor_tax_id, taxable_amount, gst_amount, gst_rate, "
        "cgst_amount, sgst_amount, igst_amount, product_name, brand, serial_number, warranty_months, "
        "warranty_start, warranty_end, category, line_items. "
        "For line_items, return an array of objects with keys: name, quantity, unit_price, amount."
    )
    user_prompt = (
        "Extract invoice fields from this bill image. "
        "Keep original invoice number formatting and correct decimal amounts. "
        "If multiple totals appear, prefer grand total/final total."
    )

    try:
        client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        response = client.converse(
            modelId=model,
            system=[{"text": system_prompt}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": user_prompt},
                        {"image": {"format": image_format, "source": {"bytes": image_bytes}}},
                    ],
                }
            ],
            inferenceConfig={"temperature": 0.0, "maxTokens": 1000},
        )
        content_blocks = (
            response.get("output", {})
            .get("message", {})
            .get("content", [])
        )
        raw_content = "".join(
            str(block.get("text", ""))
            for block in content_blocks
            if isinstance(block, dict)
        ).strip()
        if not raw_content:
            return {}
        if raw_content.startswith("```"):
            raw_content = raw_content.strip("`")
            raw_content = raw_content.replace("json\n", "", 1).strip()
        parsed = json.loads(raw_content)
    except Exception:
        return {}

    return _normalize_invoice_metadata(parsed)


def _classify_document_image_with_bedrock(image_bytes: bytes, filename: str) -> dict[str, object]:
    settings = get_settings()
    if not image_bytes:
        return {}
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {}

    model = (settings.bedrock_chat_model or "").strip()
    if boto3 is None or not model:
        # Fallback to Gemini Vision for image classification.
        try:
            gemini_result = gemini_classify_image(image_bytes, filename)
            if gemini_result and isinstance(gemini_result, dict):
                raw_is = gemini_result.get("is_invoice")
                is_inv = None
                if isinstance(raw_is, bool):
                    is_inv = raw_is
                elif isinstance(raw_is, str):
                    low = raw_is.strip().lower()
                    is_inv = True if low in {"true", "1", "yes"} else (False if low in {"false", "0", "no"} else None)
                conf = _coerce_float(gemini_result.get("confidence"), default=0.0) or 0.0
                return {
                    "is_invoice": is_inv,
                    "document_type": str(gemini_result.get("document_type") or "").strip() or None,
                    "confidence": max(0.0, min(conf, 1.0)),
                    "reason": str(gemini_result.get("reason") or "").strip() or None,
                }
        except Exception:
            pass
        return {}

    mime_type = mimetypes.guess_type(filename)[0] or "image/png"
    image_format = "png"
    if "jpeg" in mime_type or "jpg" in mime_type:
        image_format = "jpeg"
    elif "webp" in mime_type:
        image_format = "webp"
    elif "gif" in mime_type:
        image_format = "gif"

    system_prompt = (
        "You are a document classifier for SafeBill. "
        "Decide whether this image is a bill/invoice/receipt or a warranty/guarantee card. "
        "If the image is a selfie, person, object photo, app screenshot, dashboard, or anything that is not a real document, "
        "set is_invoice to false and document_type to other. "
        "Return only JSON with keys: is_invoice (boolean), document_type (string), confidence (0-1), reason (short). "
        "Acceptable document_type values: invoice, receipt, warranty_card, guarantee_card, other."
    )
    user_prompt = "Classify the document type for this image."

    try:
        configure_bedrock_api_key(settings)
        client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        response = client.converse(
            modelId=model,
            system=[{"text": system_prompt}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"text": user_prompt},
                        {"image": {"format": image_format, "source": {"bytes": image_bytes}}},
                    ],
                }
            ],
            inferenceConfig={"temperature": 0.0, "maxTokens": 300},
        )
        content_blocks = (
            response.get("output", {})
            .get("message", {})
            .get("content", [])
        )
        raw_content = "".join(
            str(block.get("text", ""))
            for block in content_blocks
            if isinstance(block, dict)
        ).strip()
        if not raw_content:
            return {}
        if raw_content.startswith("```"):
            raw_content = raw_content.strip("`")
            raw_content = raw_content.replace("json\n", "", 1).strip()
        parsed = json.loads(raw_content)
    except Exception:
        return {}

    if not isinstance(parsed, dict):
        return {}

    raw_is_invoice = parsed.get("is_invoice")
    is_invoice: bool | None = None
    if isinstance(raw_is_invoice, bool):
        is_invoice = raw_is_invoice
    elif isinstance(raw_is_invoice, str):
        lowered = raw_is_invoice.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            is_invoice = True
        elif lowered in {"false", "0", "no", "n"}:
            is_invoice = False

    confidence = _coerce_float(parsed.get("confidence"), default=0.0) or 0.0
    if confidence < 0:
        confidence = 0.0
    if confidence > 1:
        confidence = 1.0

    return {
        "is_invoice": is_invoice,
        "document_type": str(parsed.get("document_type") or "").strip() or None,
        "confidence": confidence,
        "reason": str(parsed.get("reason") or "").strip() or None,
    }


def _extract_text_metadata_with_bedrock(ocr_text: str, filename: str) -> dict[str, object]:
    settings = get_settings()
    if not ocr_text:
        return {}
    if boto3 is None:
        return {}
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {}
    if not bool(getattr(settings, "bedrock_text_mapping_enabled", False)):
        return {}

    model = (settings.bedrock_chat_model or "").strip()
    if not model:
        return {}

    system_prompt = (
        "You are an invoice data extraction engine. "
        "Return only JSON. Do not guess missing values. Use null for missing fields. "
        "Dates must be ISO 8601 format (YYYY-MM-DD). Convert from formats like 10-Feb-2026 if present. "
        "Only set monetary fields when they are explicitly shown as money (currency symbol/code or labels like TOTAL/AMOUNT/MRP/PRICE). "
        "Never treat product dimensions (e.g., '42-inch'), model numbers, serial numbers, warranty months, phone numbers, or addresses as amounts. "
        "Extract these keys exactly: "
        "bill_id, vendor, date, total_amount, vendor_tax_id, taxable_amount, gst_amount, gst_rate, "
        "cgst_amount, sgst_amount, igst_amount, product_name, brand, serial_number, warranty_months, "
        "warranty_start, warranty_end, category, line_items. "
        "For line_items, return an array of objects with keys: name, quantity, unit_price, amount."
    )
    user_prompt = (
        "Extract invoice fields from this OCR text. "
        "Keep original invoice number formatting and correct decimal amounts. "
        "If multiple totals appear, prefer grand total/final total."
    )
    user_payload = (
        f"filename={filename}\n"
        "ocr_text:\n"
        f"{ocr_text[:18000]}"
    )

    try:
        configure_bedrock_api_key(settings)
        client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        response = client.converse(
            modelId=model,
            system=[{"text": system_prompt}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": f"{user_prompt}\n\n{user_payload}"}],
                }
            ],
            inferenceConfig={"temperature": 0.0, "maxTokens": 1000},
        )
        content_blocks = (
            response.get("output", {})
            .get("message", {})
            .get("content", [])
        )
        raw_content = "".join(
            str(block.get("text", ""))
            for block in content_blocks
            if isinstance(block, dict)
        ).strip()
        if not raw_content:
            return {}
        if raw_content.startswith("```"):
            raw_content = raw_content.strip("`")
            raw_content = raw_content.replace("json\n", "", 1).strip()
        parsed = json.loads(raw_content)
    except Exception:
        return {}

    return _normalize_invoice_metadata(parsed)


def _extract_product_name_with_bedrock(ocr_text: str, filename: str) -> dict[str, object]:
    settings = get_settings()
    if not ocr_text:
        return {}
    if boto3 is None:
        return {}
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {}
    if not bool(getattr(settings, "bedrock_text_mapping_enabled", False)):
        return {}

    model = (settings.bedrock_chat_model or "").strip()
    if not model:
        return {}

    system_prompt = (
        "You extract product line items from OCR text. "
        "Return only JSON with keys: product_name, line_items. "
        "product_name must be the primary purchased item, not the store name. "
        "If multiple items exist, choose the most expensive or primary one. "
        "line_items must be an array of objects with keys: name, quantity, unit_price, amount. "
        "Do not include totals, taxes, discounts, shipping, or 'amount in words' as line items."
    )
    user_payload = f"filename={filename}\nocr_text:\n{ocr_text[:18000]}"
    try:
        configure_bedrock_api_key(settings)
        client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        response = client.converse(
            modelId=model,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_payload}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": 800},
        )
        content_blocks = (
            response.get("output", {})
            .get("message", {})
            .get("content", [])
        )
        raw_content = "".join(
            str(block.get("text", ""))
            for block in content_blocks
            if isinstance(block, dict)
        ).strip()
        if not raw_content:
            return {}
        if raw_content.startswith("```"):
            raw_content = raw_content.strip("`")
            raw_content = raw_content.replace("json\n", "", 1).strip()
        parsed = json.loads(raw_content)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _extract_text_with_textract(image_bytes: bytes) -> str:
    settings = get_settings()
    if not image_bytes or boto3 is None:
        return ""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return ""
    try:
        client = boto3.client("textract", region_name=settings.aws_region)
        response = client.detect_document_text(Document={"Bytes": image_bytes})
    except Exception:
        return ""
    blocks = response.get("Blocks", [])
    lines: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if str(block.get("BlockType") or "").upper() != "LINE":
                continue
            text = str(block.get("Text") or "").strip()
            if text:
                lines.append(text)
    return "\n".join(lines).strip()


def _extract_text_with_google_vision(image_bytes: bytes) -> str:
    settings = get_settings()
    if not image_bytes or httpx is None:
        return ""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return ""
    auth_headers, params, auth_mode = _google_vision_auth_context()
    if auth_mode == "none":
        return ""
    if auth_mode == "service_account" and (service_account is None or GoogleAuthRequest is None):
        return ""

    endpoint = (settings.google_vision_endpoint or "").strip() or "https://vision.googleapis.com/v1/images:annotate"
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION", "maxResults": 1}],
            }
        ]
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(endpoint, params=params, headers=auth_headers, json=payload)
            response.raise_for_status()
            parsed = response.json()
    except Exception:
        return ""

    responses = parsed.get("responses", []) if isinstance(parsed, dict) else []
    first = responses[0] if isinstance(responses, list) and responses else {}
    if not isinstance(first, dict):
        return ""
    if isinstance(first.get("error"), dict):
        return ""
    full_text = first.get("fullTextAnnotation", {})
    if isinstance(full_text, dict):
        text = str(full_text.get("text") or "").strip()
        if text:
            return text
    text_annotations = first.get("textAnnotations", [])
    if isinstance(text_annotations, list) and text_annotations:
        first_ann = text_annotations[0]
        if isinstance(first_ann, dict):
            return str(first_ann.get("description") or "").strip()
    return ""


def _looks_like_person_photo(image_bytes: bytes) -> bool:
    if not image_bytes or httpx is None:
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    auth_headers, params, auth_mode = _google_vision_auth_context()
    if auth_mode == "none":
        return False
    if auth_mode == "service_account" and (service_account is None or GoogleAuthRequest is None):
        return False

    endpoint = (get_settings().google_vision_endpoint or "").strip() or "https://vision.googleapis.com/v1/images:annotate"
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": [
                    {"type": "FACE_DETECTION", "maxResults": 3},
                    {"type": "LABEL_DETECTION", "maxResults": 8},
                ],
            }
        ]
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(endpoint, params=params, headers=auth_headers, json=payload)
            response.raise_for_status()
            parsed = response.json()
    except Exception:
        return False

    responses = parsed.get("responses", []) if isinstance(parsed, dict) else []
    first = responses[0] if isinstance(responses, list) and responses else {}
    if not isinstance(first, dict):
        return False
    faces = first.get("faceAnnotations")
    if isinstance(faces, list) and faces:
        return True
    labels = first.get("labelAnnotations")
    if isinstance(labels, list):
        for label in labels:
            if not isinstance(label, dict):
                continue
            desc = str(label.get("description") or "").strip().lower()
            if desc in {"person", "people", "face", "selfie", "portrait", "human", "man", "woman"}:
                score = _coerce_float(label.get("score"), default=0.0) or 0.0
                if score >= 0.7:
                    return True
    return False


def _google_vision_auth_context() -> tuple[dict[str, str], dict[str, str] | None, str]:
    settings = get_settings()
    creds_file = (settings.google_vision_credentials_file or "").strip()
    if creds_file:
        if service_account is None or GoogleAuthRequest is None:
            return {}, None, "service_account"
        try:
            scopes = [str(settings.google_vision_scope or "").strip() or "https://www.googleapis.com/auth/cloud-platform"]
            creds = service_account.Credentials.from_service_account_file(creds_file, scopes=scopes)
            creds.refresh(GoogleAuthRequest())
            token = str(creds.token or "").strip()
            if token:
                return {"Authorization": f"Bearer {token}"}, None, "service_account"
        except Exception:
            return {}, None, "none"
    api_key = (settings.google_vision_api_key or "").strip()
    if api_key:
        endpoint = (settings.google_vision_endpoint or "").strip()
        params = None if "key=" in endpoint else {"key": api_key}
        return {}, params, "api_key"
    return {}, None, "none"


def _extract_image_metadata_with_google_vision(image_bytes: bytes, filename: str) -> tuple[dict[str, object], str]:
    text_output = _extract_text_with_google_vision(image_bytes)
    if not text_output:
        return {}, ""
    metadata = ensure_strict_extraction(extract_invoice_metadata(text_output, filename))
    return metadata, text_output


def _extract_image_metadata_with_textract(image_bytes: bytes, filename: str) -> tuple[dict[str, object], str]:
    settings = get_settings()
    if not image_bytes or boto3 is None:
        return {}, ""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {}, ""

    text_output = _extract_text_with_textract(image_bytes)
    extracted = ensure_strict_extraction(extract_invoice_metadata(text_output, filename)) if text_output else {}
    try:
        client = boto3.client("textract", region_name=settings.aws_region)
        expense = client.analyze_expense(Document={"Bytes": image_bytes})
    except Exception:
        return extracted, text_output

    summary_fields: list[dict[str, object]] = []
    line_items: list[dict[str, object]] = []
    for document in expense.get("ExpenseDocuments", []) if isinstance(expense, dict) else []:
        if not isinstance(document, dict):
            continue
        raw_summary = document.get("SummaryFields", [])
        if isinstance(raw_summary, list):
            summary_fields.extend([field for field in raw_summary if isinstance(field, dict)])
        groups = document.get("LineItemGroups", [])
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for item in group.get("LineItems", []) if isinstance(group.get("LineItems"), list) else []:
                    if not isinstance(item, dict):
                        continue
                    fields = item.get("LineItemExpenseFields", [])
                    if not isinstance(fields, list):
                        continue
                    parsed_item: dict[str, object] = {}
                    for field in fields:
                        if not isinstance(field, dict):
                            continue
                        field_type = str(field.get("Type", {}).get("Text") if isinstance(field.get("Type"), dict) else "").strip().upper()
                        field_val = str(field.get("ValueDetection", {}).get("Text") if isinstance(field.get("ValueDetection"), dict) else "").strip()
                        if not field_val:
                            continue
                        if field_type in {"ITEM", "ITEM_NAME", "DESCRIPTION"} and "name" not in parsed_item:
                            parsed_item["name"] = field_val[:255]
                        elif field_type in {"PRICE", "AMOUNT", "TOTAL"} and "amount" not in parsed_item:
                            amount = _coerce_float(field_val.replace(",", ""))
                            if amount is not None:
                                parsed_item["amount"] = amount
                        elif field_type in {"QUANTITY", "QTY"} and "quantity" not in parsed_item:
                            quantity = _coerce_float(field_val.replace(",", ""))
                            if quantity is not None:
                                parsed_item["quantity"] = quantity
                        elif field_type in {"UNIT_PRICE"} and "unit_price" not in parsed_item:
                            unit_price = _coerce_float(field_val.replace(",", ""))
                            if unit_price is not None:
                                parsed_item["unit_price"] = unit_price
                    if parsed_item:
                        line_items.append(parsed_item)

    mapped: dict[str, object] = dict(extracted)
    for field in summary_fields:
        label = str(field.get("Type", {}).get("Text") if isinstance(field.get("Type"), dict) else "").strip().upper()
        value_text = str(field.get("ValueDetection", {}).get("Text") if isinstance(field.get("ValueDetection"), dict) else "").strip()
        if not value_text:
            continue
        normalized_value = value_text.replace(",", "")
        if label in {"INVOICE_RECEIPT_ID", "RECEIPT_ID"} and not mapped.get("bill_id"):
            mapped["bill_id"] = value_text[:128]
        elif label in {"VENDOR_NAME"} and not mapped.get("vendor"):
            mapped["vendor"] = value_text[:255]
        elif label in {"INVOICE_RECEIPT_DATE"} and not mapped.get("date"):
            parsed_date = _coerce_date(value_text)
            if parsed_date:
                mapped["date"] = parsed_date.isoformat()
            else:
                mapped["date"] = value_text[:32]
        elif label in {"TOTAL"} and mapped.get("total_amount") is None:
            amount = _coerce_float(normalized_value)
            if amount is not None:
                mapped["total_amount"] = amount
        elif label in {"SUBTOTAL"} and mapped.get("taxable_amount") is None:
            amount = _coerce_float(normalized_value)
            if amount is not None:
                mapped["taxable_amount"] = amount
        elif label in {"TAX"} and mapped.get("gst_amount") is None:
            amount = _coerce_float(normalized_value)
            if amount is not None:
                mapped["gst_amount"] = amount
        elif label in {"VENDOR_VAT_NUMBER", "VENDOR_GST_NUMBER"} and not mapped.get("vendor_tax_id"):
            mapped["vendor_tax_id"] = value_text[:64]

    if line_items and not mapped.get("line_items"):
        mapped["line_items"] = line_items[:50]

    return ensure_strict_extraction(mapped), text_output


def _extract_image_metadata_with_proxy(
    *,
    image_bytes: bytes,
    filename: str,
    proxy_url: str,
    proxy_api_key: str,
) -> tuple[dict[str, object], str]:
    if not image_bytes or not proxy_url or httpx is None:
        return {}, ""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {}, ""

    encoded = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "filename": filename,
        "image_base64": encoded,
    }
    headers = {"Content-Type": "application/json"}
    token = (proxy_api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(proxy_url, headers=headers, json=payload)
        if response.status_code >= 400:
            return {}, ""
        parsed = response.json()
    except Exception:
        return {}, ""

    metadata: dict[str, object] = {}
    text = ""
    if isinstance(parsed, dict):
        raw_metadata = parsed.get("metadata")
        if isinstance(raw_metadata, dict):
            metadata = raw_metadata
        elif isinstance(parsed.get("result"), dict):
            metadata = parsed["result"]  # type: ignore[index]
        raw_text = parsed.get("text")
        if isinstance(raw_text, str):
            text = raw_text.strip()
    return ensure_strict_extraction(metadata), text


def _manual_override_metadata(
    *,
    bill_id: str | None,
    vendor: str | None,
    document_date: date | None,
    total_amount: float | None,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if bill_id:
        payload["bill_id"] = bill_id.strip()[:128]
    if vendor:
        payload["vendor"] = vendor.strip()[:255]
    if document_date:
        payload["date"] = document_date.isoformat()
    if total_amount is not None:
        payload["total_amount"] = total_amount
    return payload


def _run_image_extraction_router(
    *,
    image_bytes: bytes,
    filename: str,
    supplied_ocr_text: str,
    ocr_mode_override: str | None,
    bill_id: str | None,
    vendor: str | None,
    document_date: date | None,
    total_amount: float | None,
) -> dict[str, object]:
    settings = get_settings()
    requested_mode = str(ocr_mode_override or "").strip().lower()
    ocr_mode = requested_mode or str(getattr(settings, "image_ocr_mode", "auto") or "auto").strip().lower()
    local_only_mode = ocr_mode in {"local", "local_only", "tesseract", "tesseractjs"}
    google_fast_mode = ocr_mode in {"auto", "cloud", "cloud_only", "google", "google_only", "vision", "vision_only"}
    bedrock_hybrid_mode = ocr_mode in {"hybrid", "cloud_hybrid", "vision_bedrock", "google_bedrock"}
    engine_results: list[dict[str, object]] = []
    supplied_has_invoice_signals = False

    supplied_text = supplied_ocr_text.strip()
    if supplied_text:
        metadata = ensure_strict_extraction(extract_invoice_metadata(supplied_text, filename))
        supplied_vendor = str(metadata.get("vendor") or "").strip().lower()
        supplied_has_invoice_signals = (
            bool(supplied_vendor and supplied_vendor not in {"unknown_vendor", "unknown vendor"})
            or _is_meaningful_metadata_value(metadata.get("date"))
            or _is_meaningful_metadata_value(metadata.get("total_amount"))
        )
        engine_results.append(
            {
                "engine": "tesseract_regex",
                "metadata": metadata,
                "text": supplied_text,
                "field_confidences": compute_field_confidences(
                    metadata=metadata,
                    engine="tesseract_regex",
                    text_quality=estimate_text_quality(supplied_text),
                ),
            }
        )

    def _append_bedrock_engine_result() -> None:
        bedrock_metadata = ensure_strict_extraction(_extract_image_metadata_with_bedrock(image_bytes, filename))
        if any(
            _is_meaningful_metadata_value(bedrock_metadata.get(key))
            for key in ("bill_id", "vendor", "total_amount", "date")
        ):
            canonical_text = _metadata_to_canonical_text(bedrock_metadata)
            engine_results.append(
                {
                    "engine": "aws_bedrock_vision",
                    "metadata": bedrock_metadata,
                    "text": canonical_text,
                    "field_confidences": compute_field_confidences(
                        metadata=bedrock_metadata,
                        engine="aws_bedrock_vision",
                        text_quality=estimate_text_quality(canonical_text),
                    ),
                }
            )

    # --- Gemini Vision engine (primary when Gemini API key is configured) ---
    if not local_only_mode:
        try:
            gemini_raw = gemini_extract_image_metadata(image_bytes, filename)
            if gemini_raw and isinstance(gemini_raw, dict):
                gemini_full_text = str(gemini_raw.pop("full_text", "") or "").strip()
                gemini_metadata = ensure_strict_extraction(_normalize_invoice_metadata(gemini_raw))
                if any(
                    _is_meaningful_metadata_value(gemini_metadata.get(key))
                    for key in ("bill_id", "vendor", "total_amount", "date")
                ):
                    canonical_gemini_text = gemini_full_text or _metadata_to_canonical_text(gemini_metadata)
                    engine_results.append(
                        {
                            "engine": "gemini_vision",
                            "metadata": gemini_metadata,
                            "text": canonical_gemini_text,
                            "field_confidences": compute_field_confidences(
                                metadata=gemini_metadata,
                                engine="gemini_vision",
                                text_quality=estimate_text_quality(canonical_gemini_text),
                            ),
                        }
                    )
                elif gemini_full_text:
                    # Even if structured fields were weak, feed full text through regex extraction.
                    fallback_metadata = ensure_strict_extraction(extract_invoice_metadata(gemini_full_text, filename))
                    if any(
                        _is_meaningful_metadata_value(fallback_metadata.get(key))
                        for key in ("bill_id", "vendor", "total_amount", "date")
                    ):
                        engine_results.append(
                            {
                                "engine": "gemini_vision",
                                "metadata": fallback_metadata,
                                "text": gemini_full_text,
                                "field_confidences": compute_field_confidences(
                                    metadata=fallback_metadata,
                                    engine="gemini_vision",
                                    text_quality=estimate_text_quality(gemini_full_text),
                                ),
                            }
                        )
        except Exception:
            pass  # Gemini unavailable, continue with other engines.

    has_gemini_success = any(r.get("engine") == "gemini_vision" for r in engine_results)
    if not local_only_mode and not has_gemini_success:
        google_metadata, google_text = _extract_image_metadata_with_google_vision(image_bytes, filename)
        google_vendor = str(google_metadata.get("vendor") or "").strip().lower()
        google_has_invoice_signals = (
            bool(google_vendor and google_vendor not in {"unknown_vendor", "unknown vendor"})
            or _is_meaningful_metadata_value(google_metadata.get("date"))
            or _is_meaningful_metadata_value(google_metadata.get("total_amount"))
        )
        if any(
            _is_meaningful_metadata_value(google_metadata.get(key))
            for key in ("bill_id", "vendor", "total_amount", "date")
        ):
            canonical_google_text = google_text or _metadata_to_canonical_text(google_metadata)
            engine_results.append(
                {
                    "engine": "google_vision",
                    "metadata": google_metadata,
                    "text": canonical_google_text,
                    "field_confidences": compute_field_confidences(
                        metadata=google_metadata,
                        engine="google_vision",
                        text_quality=estimate_text_quality(canonical_google_text),
                    ),
                }
            )

        text_for_mapping = google_text or supplied_text
        if text_for_mapping and bool(getattr(settings, "bedrock_text_mapping_enabled", True)):
            bedrock_text_metadata = ensure_strict_extraction(
                _extract_text_metadata_with_bedrock(text_for_mapping, filename)
            )
            if any(
                _is_meaningful_metadata_value(bedrock_text_metadata.get(key))
                for key in ("bill_id", "vendor", "total_amount", "date")
            ):
                engine_results.append(
                    {
                        "engine": "aws_bedrock_text",
                        "metadata": bedrock_text_metadata,
                        "text": text_for_mapping,
                        "field_confidences": compute_field_confidences(
                            metadata=bedrock_text_metadata,
                            engine="aws_bedrock_text",
                            text_quality=estimate_text_quality(text_for_mapping),
                        ),
                    }
                )

        needs_bedrock_fast_fallback = google_fast_mode and (
            _looks_like_ui_screenshot(supplied_text or google_text)
            or (not supplied_has_invoice_signals and not google_has_invoice_signals)
        )
        if bedrock_hybrid_mode or needs_bedrock_fast_fallback:
            _append_bedrock_engine_result()

        if not google_fast_mode and not bedrock_hybrid_mode:
            textract_metadata, textract_text = _extract_image_metadata_with_textract(image_bytes, filename)
            if any(
                _is_meaningful_metadata_value(textract_metadata.get(key))
                for key in ("bill_id", "vendor", "total_amount", "date")
            ):
                canonical_textract_text = textract_text or _metadata_to_canonical_text(textract_metadata)
                engine_results.append(
                    {
                        "engine": "aws_textract",
                        "metadata": textract_metadata,
                        "text": canonical_textract_text,
                        "field_confidences": compute_field_confidences(
                            metadata=textract_metadata,
                            engine="aws_textract",
                            text_quality=estimate_text_quality(canonical_textract_text),
                        ),
                    }
                )

            tesseract_text = _ocr_image_bytes(image_bytes)
            if tesseract_text and tesseract_text.strip() and tesseract_text.strip() != supplied_text:
                tesseract_metadata = ensure_strict_extraction(extract_invoice_metadata(tesseract_text, filename))
                engine_results.append(
                    {
                        "engine": "tesseract_regex",
                        "metadata": tesseract_metadata,
                        "text": tesseract_text,
                        "field_confidences": compute_field_confidences(
                            metadata=tesseract_metadata,
                            engine="tesseract_regex",
                            text_quality=estimate_text_quality(tesseract_text),
                        ),
                    }
                )

            _append_bedrock_engine_result()

            if settings.textract_proxy_url:
                textract_proxy_metadata, textract_proxy_text = _extract_image_metadata_with_proxy(
                    image_bytes=image_bytes,
                    filename=filename,
                    proxy_url=settings.textract_proxy_url.strip(),
                    proxy_api_key=settings.textract_proxy_api_key,
                )
                if any(
                    _is_meaningful_metadata_value(textract_proxy_metadata.get(key))
                    for key in ("bill_id", "vendor", "total_amount", "date")
                ):
                    engine_results.append(
                        {
                            "engine": "aws_textract_proxy",
                            "metadata": textract_proxy_metadata,
                            "text": (textract_proxy_text or _metadata_to_canonical_text(textract_proxy_metadata)),
                            "field_confidences": compute_field_confidences(
                                metadata=textract_proxy_metadata,
                                engine="aws_textract",
                                text_quality=estimate_text_quality(textract_proxy_text),
                            ),
                        }
                    )

    manual_overrides = _manual_override_metadata(
        bill_id=bill_id,
        vendor=vendor,
        document_date=document_date,
        total_amount=total_amount,
    )
    if manual_overrides:
        engine_results.append(
            {
                "engine": "manual_override",
                "metadata": ensure_strict_extraction(manual_overrides),
                "text": _metadata_to_canonical_text(manual_overrides),
                "field_confidences": compute_field_confidences(
                    metadata=manual_overrides,
                    engine="manual_override",
                    text_quality=1.0,
                ),
            }
        )

    merged_metadata, field_confidences, field_sources = merge_engine_results(
        engine_results,
        manual_overrides=manual_overrides,
    )
    grounded_engine_names = {
        "google_vision",
        "gemini_vision",
        "aws_bedrock_text",
        "aws_textract",
        "aws_textract_proxy",
        "tesseract_regex",
        "manual_override",
    }
    grounded_results = [
        result
        for result in engine_results
        if str(result.get("engine") or "").strip().lower() in grounded_engine_names
    ]
    if grounded_results:
        grounded_metadata, grounded_confidences, grounded_sources = merge_engine_results(
            grounded_results,
            manual_overrides=manual_overrides,
        )
        merged_metadata, field_confidences, field_sources = prefer_grounded_ocr_fields(
            merged_metadata,
            grounded_metadata,
            confidence_map=field_confidences,
            source_map=field_sources,
            grounded_confidence_map=grounded_confidences,
            grounded_source_map=grounded_sources,
        )
    low_conf_fields = build_review_fields(
        field_confidences,
        threshold=float(settings.extraction_review_required_threshold),
    )
    candidate_texts = [
        str(result.get("text") or "").strip()
        for result in engine_results
        if str(result.get("text") or "").strip()
    ]
    resolved_text = supplied_text or (max(candidate_texts, key=len) if candidate_texts else "")
    if not resolved_text:
        resolved_text = _metadata_to_canonical_text(merged_metadata)

    return {
        "metadata": merged_metadata,
        "resolved_text": resolved_text,
        "field_confidences": field_confidences,
        "field_sources": field_sources,
        "low_confidence_fields": low_conf_fields,
        "engines_used": [str(result.get("engine") or "unknown") for result in engine_results],
        "engine_results": engine_results,
    }


def _persist_structured_document(
    db: Session,
    services: ServiceRegistry,
    *,
    filename: str,
    source: str,
    user_id: str | None,
    extracted_text: str,
    extracted_metadata: dict[str, object] | None = None,
    bill_id: str | None = None,
    vendor: str | None = None,
    document_date: date | None = None,
    total_amount: float | None = None,
    version: int = 1,
    field_confidences: dict[str, float] | None = None,
    field_sources: dict[str, str] | None = None,
    low_confidence_fields: list[str] | None = None,
    extraction_engines: list[str] | None = None,
    additional_references: dict[str, object] | None = None,
) -> tuple[Document, int]:
    settings = get_settings()
    fallback_metadata = ensure_strict_extraction(extract_invoice_metadata(extracted_text, filename))
    preferred_metadata = ensure_strict_extraction(extracted_metadata or {})
    metadata = ensure_strict_extraction(_merge_invoice_metadata(preferred_metadata, fallback_metadata))
    metadata = ensure_strict_extraction(
        _apply_invoice_request_hints(
            metadata,
            filename=filename,
            authoritative=(source == "merchant_manual"),
            bill_id=bill_id,
            vendor=vendor,
            document_date=document_date,
            total_amount=total_amount,
        )
    )
    if source in {"image_ocr", "image_ocr_router", "image_ocr_async"} and _metadata_looks_like_ui(metadata):
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )

    fallback_bill = f"{source.upper()}-{int(time.time() * 1000)}"
    raw_bill = str(metadata.get("bill_id") or "").strip()
    if not raw_bill or any(bad in raw_bill.lower() for bad in ("whatsapp image", "screenshot", "screen shot", "img_", "dsc_")):
        resolved_bill_id = fallback_bill[:128]
    else:
        resolved_bill_id = raw_bill[:128]
    resolved_vendor = str(metadata.get("vendor") or "UNKNOWN_VENDOR")[:256]
    resolved_date = _coerce_date(metadata.get("date"))
    resolved_total = _coerce_float(metadata.get("total_amount"))
    if resolved_total is None:
        taxable_amount = _coerce_float(metadata.get("taxable_amount"))
        gst_amount = _coerce_float(metadata.get("gst_amount"))
        if taxable_amount is not None and taxable_amount > 0 and gst_amount is not None and gst_amount >= 0:
            resolved_total = round(taxable_amount + gst_amount, 2)

    title_fallback = filename.rsplit(".", 1)[0] if filename else "Uploaded Document"
    extracted_product_name = str(sanitize_merchandise_name(metadata.get("product_name")) or "").strip()
    if extracted_product_name and _looks_like_non_merchandise_item_name(extracted_product_name):
        extracted_product_name = ""
    metadata_line_items = metadata.get("line_items")
    def _sanitize_line_items(raw_items: object) -> list[dict[str, object]]:
        sanitized: list[dict[str, object]] = []
        if isinstance(raw_items, list):
            for item in raw_items[:50]:
                if not isinstance(item, dict):
                    continue
                entry_name = sanitize_merchandise_name(item.get("name") or item.get("product_name"))
                if not entry_name:
                    continue
                normalized_item: dict[str, object] = {"name": entry_name}
                amount = _coerce_float(item.get("amount"))
                quantity = _coerce_float(item.get("quantity"))
                unit_price = _coerce_float(item.get("unit_price"))
                gst_component = _coerce_float(item.get("gst_amount"))
                if amount is not None:
                    normalized_item["amount"] = amount
                if quantity is not None:
                    normalized_item["quantity"] = quantity
                if unit_price is not None:
                    normalized_item["unit_price"] = unit_price
                if gst_component is not None:
                    normalized_item["gst_amount"] = gst_component
                sanitized.append(normalized_item)
        return sanitized

    sanitized_line_items = _sanitize_line_items(metadata_line_items)
    if not extracted_product_name:
        for item in sanitized_line_items:
            candidate = str(item.get("name") or "").strip()
            if candidate:
                extracted_product_name = candidate
                break
    if not extracted_product_name and extracted_text:
        bedrock_payload = _extract_product_name_with_bedrock(extracted_text, filename)
        bedrock_name = str(sanitize_merchandise_name(bedrock_payload.get("product_name")) or "").strip()
        if bedrock_name and not _looks_like_non_merchandise_item_name(bedrock_name):
            extracted_product_name = bedrock_name
            metadata["product_name"] = bedrock_name
        bedrock_items = bedrock_payload.get("line_items")
        if not sanitized_line_items and isinstance(bedrock_items, list):
            metadata["line_items"] = bedrock_items
            sanitized_line_items = _sanitize_line_items(bedrock_items)
            if not extracted_product_name:
                for item in sanitized_line_items:
                    candidate = str(item.get("name") or "").strip()
                    if candidate:
                        extracted_product_name = candidate
                        break
    if extracted_product_name:
        metadata["product_name"] = extracted_product_name
    title = extracted_product_name or _first_meaningful_line(extracted_text, fallback=title_fallback)
    existing_id = (
        db.execute(select(Document.id).where(Document.bill_id == resolved_bill_id, Document.version == version).limit(1))
        .scalar_one_or_none()
    )
    if existing_id:
        suffix = int(time.time() * 1000) % 1_000_000
        base = resolved_bill_id[:120]
        resolved_bill_id = f"{base}-{suffix}"

    extracted_warranty_months = _coerce_int(metadata.get("warranty_months"), default=12) or 12
    extracted_warranty_start = _coerce_date(metadata.get("warranty_start")) or resolved_date
    extracted_warranty_end = _coerce_date(metadata.get("warranty_end"))
    if extracted_warranty_end is None and extracted_warranty_start:
        extracted_warranty_end = add_months(extracted_warranty_start, extracted_warranty_months)
    inferred_category = _infer_locker_category(
        product_name=(extracted_product_name or title),
        brand=str(metadata.get("brand") or resolved_vendor),
        vendor=resolved_vendor,
        line_items=(sanitized_line_items or None),
        source_category=metadata.get("category"),
    )
    extraction_confidences = dict(field_confidences or {})
    if not extraction_confidences:
        extraction_confidences = compute_field_confidences(
            metadata=metadata,
            engine="tesseract_regex",
            text_quality=estimate_text_quality(extracted_text),
        )
    extraction_sources = dict(field_sources or {})
    if not extraction_sources:
        extraction_sources = {field: source for field in extraction_confidences}
    extracted_low_confidence = list(low_confidence_fields or [])
    if not extracted_low_confidence:
        extracted_low_confidence = build_review_fields(
            extraction_confidences,
            threshold=float(settings.extraction_review_required_threshold),
        )
    ocr_snapshot_references = _store_ocr_text_snapshot(
        services=services,
        extracted_text=extracted_text,
        filename=filename,
        source=source,
        document_user_id=user_id,
        merchant_user_id=str((additional_references or {}).get("merchant_user_id") or "").strip() or None,
    )

    fingerprint = extraction_fingerprint(metadata, extracted_text)
    identifier_payload = _extract_document_identifiers(metadata, extracted_text)
    identifier_raw = list(identifier_payload.get("identifiers_raw") or [])
    identifier_norm = list(identifier_payload.get("identifiers_norm") or [])

    duplicate_count = 0
    scoped_user_id = str(user_id or "").strip()
    scoped_merchant_id = str((additional_references or {}).get("merchant_user_id") or "").strip()
    owner_field = None
    owner_value = ""
    if scoped_user_id:
        owner_field = Document.references["user_id"].as_string()
        owner_value = scoped_user_id
    elif scoped_merchant_id:
        owner_field = Document.references["merchant_user_id"].as_string()
        owner_value = scoped_merchant_id

    if owner_field is not None and owner_value:
        duplicate_conditions: list[object] = [
            Document.references["extraction_fingerprint"].as_string() == fingerprint,
        ]
        if identifier_raw:
            duplicate_conditions.append(Document.bill_id.in_(identifier_raw))
        if identifier_norm:
            duplicate_conditions.append(Document.references["invoice_number_norm"].as_string().in_(identifier_norm))
            duplicate_conditions.append(Document.references["order_number_norm"].as_string().in_(identifier_norm))
            duplicate_conditions.append(Document.references["warranty_number_norm"].as_string().in_(identifier_norm))
            duplicate_conditions.append(Document.references["serial_number_norm"].as_string().in_(identifier_norm))
        duplicate_stmt = select(func.count(Document.id)).where(
            owner_field == owner_value,
            or_(*duplicate_conditions),
        )
        duplicate_count = int(db.execute(duplicate_stmt).scalar_one_or_none() or 0)
    duplicate_flag = duplicate_count > 0
    if duplicate_flag:
        raise HTTPException(
            status_code=409,
            detail="Duplicate bill detected. This bill already exists in your locker.",
        )

    references: dict[str, object] = {
        "filename": filename,
        "source": source,
        "user_id": user_id or "anonymous",
        "title": title,
        "product_name": title,
        "brand": str(metadata.get("brand") or resolved_vendor),
        "category": inferred_category,
        "is_verified": True,
        "raw_text": extracted_text[:50000],
        "ocr_confidence": (
            round(sum(extraction_confidences.values()) / max(len(extraction_confidences), 1), 4)
            if extraction_confidences
            else (0.7 if source == "image_ocr" else 1.0)
        ),
        "warranty_months": extracted_warranty_months,
        "extraction_confidence": extraction_confidences,
        "extraction_field_sources": extraction_sources,
        "low_confidence_fields": extracted_low_confidence,
        "extraction_review_required": len(extracted_low_confidence) > 0,
        "extraction_engines": extraction_engines or [source],
        "extraction_fingerprint": fingerprint,
        "duplicate_suspected": duplicate_flag,
        "duplicate_match_count": duplicate_count,
        "strict_schema_version": "invoice.v1",
    }
    if additional_references:
        for key, value in additional_references.items():
            if value is None or value == "":
                continue
            references[key] = value
    if ocr_snapshot_references:
        references.update(ocr_snapshot_references)

    if source in {"image_ocr", "image_ocr_router", "image_ocr_async"}:
        document_type_payload = _enforce_document_text_classification(
            services=services,
            filename=filename,
            snapshot_references=ocr_snapshot_references,
            fallback_text=extracted_text,
        )
        if document_type_payload:
            references.update(document_type_payload)
    if metadata.get("vendor_tax_id"):
        references["vendor_tax_id"] = str(metadata["vendor_tax_id"])
    for tax_key in ("taxable_amount", "gst_amount", "gst_rate", "cgst_amount", "sgst_amount", "igst_amount"):
        tax_value = _coerce_float(metadata.get(tax_key))
        if tax_value is not None and references.get(tax_key) is None:
            references[tax_key] = tax_value
    if sanitized_line_items and not references.get("line_items"):
        references["line_items"] = sanitized_line_items[:50]
    if metadata.get("serial_number") and not references.get("serial_number"):
        references["serial_number"] = str(metadata["serial_number"])
    if identifier_payload:
        if identifier_payload.get("invoice_number") and not references.get("invoice_number"):
            references["invoice_number"] = identifier_payload["invoice_number"]
        if identifier_payload.get("order_number") and not references.get("order_number"):
            references["order_number"] = identifier_payload["order_number"]
        if identifier_payload.get("warranty_number") and not references.get("warranty_number"):
            references["warranty_number"] = identifier_payload["warranty_number"]
        if identifier_payload.get("serial_number") and not references.get("serial_number"):
            references["serial_number"] = identifier_payload["serial_number"]
        if identifier_payload.get("identifiers_raw"):
            references["document_identifiers"] = identifier_payload["identifiers_raw"]
        if identifier_payload.get("identifiers_norm"):
            references["document_identifiers_norm"] = identifier_payload["identifiers_norm"]
        if identifier_payload.get("invoice_number"):
            references["invoice_number_norm"] = _normalize_identifier_value(identifier_payload["invoice_number"])
        if identifier_payload.get("order_number"):
            references["order_number_norm"] = _normalize_identifier_value(identifier_payload["order_number"])
        if identifier_payload.get("warranty_number"):
            references["warranty_number_norm"] = _normalize_identifier_value(identifier_payload["warranty_number"])
        if identifier_payload.get("serial_number"):
            references["serial_number_norm"] = _normalize_identifier_value(identifier_payload["serial_number"])
    if extracted_warranty_start and not references.get("warranty_start"):
        references["warranty_start"] = extracted_warranty_start.isoformat()
    if extracted_warranty_end and not references.get("warranty_end"):
        references["warranty_end"] = extracted_warranty_end.isoformat()

    compliance_input = {
        "bill_id": resolved_bill_id,
        "vendor": resolved_vendor,
        "date": (resolved_date.isoformat() if resolved_date else None),
        "total_amount": resolved_total,
        "vendor_tax_id": references.get("vendor_tax_id"),
        "taxable_amount": references.get("taxable_amount"),
        "gst_amount": references.get("gst_amount"),
        "gst_rate": references.get("gst_rate"),
        "cgst_amount": references.get("cgst_amount"),
        "sgst_amount": references.get("sgst_amount"),
        "igst_amount": references.get("igst_amount"),
        "line_items": references.get("line_items"),
    }
    compliance_payload = validate_invoice_compliance(
        metadata=compliance_input,
        raw_text=str(references.get("raw_text") or ""),
    )
    references["compliance"] = compliance_payload
    references["compliance_status"] = str(compliance_payload.get("status") or "watch")
    references["compliance_score"] = int(compliance_payload.get("score") or 0)
    if not references.get("vendor_tax_id"):
        gstin_payload = compliance_payload.get("gstin")
        if isinstance(gstin_payload, dict) and gstin_payload.get("value"):
            references["vendor_tax_id"] = str(gstin_payload["value"])

    document = Document(
        bill_id=resolved_bill_id,
        vendor=resolved_vendor,
        date=resolved_date,
        total_amount=resolved_total,
        version=version,
        references=references,
    )
    db.add(document)
    db.flush()

    metadata_content = {
        "bill_id": resolved_bill_id,
        "vendor": resolved_vendor,
        "date": resolved_date.isoformat() if resolved_date else None,
        "total_amount": resolved_total,
        "vendor_tax_id": references.get("vendor_tax_id"),
        "taxable_amount": references.get("taxable_amount"),
        "gst_amount": references.get("gst_amount"),
        "gst_rate": references.get("gst_rate"),
        "cgst_amount": references.get("cgst_amount"),
        "sgst_amount": references.get("sgst_amount"),
        "igst_amount": references.get("igst_amount"),
        "product_name": references.get("product_name"),
        "brand": references.get("brand"),
        "category": references.get("category"),
        "serial_number": references.get("serial_number"),
        "warranty_months": references.get("warranty_months"),
        "warranty_start": references.get("warranty_start"),
        "warranty_end": references.get("warranty_end"),
        "compliance_status": references.get("compliance_status"),
        "compliance_score": references.get("compliance_score"),
        "line_items": references.get("line_items"),
        "is_scanned": source == "image_ocr",
    }
    chunk_inputs: list[tuple[str, str, dict[str, str]]] = [
        ("invoice_metadata", json.dumps(metadata_content, ensure_ascii=True), {"section": "metadata", "source": source}),
    ]
    body_content = extracted_text.strip()
    if body_content:
        chunk_inputs.append(("body_section", body_content[:12000], {"section": "ocr_text", "source": source}))
    if isinstance(references.get("line_items"), list):
        line_items_content = json.dumps(references.get("line_items"), ensure_ascii=True)
        if line_items_content and line_items_content != "[]":
            chunk_inputs.append(("line_items", line_items_content[:12000], {"section": "line_items", "source": source}))

    # Keep OCR- and manual-entry ingestion fast by avoiding per-chunk model calls.
    fast_chunk_metadata = source in {
        "image_ocr",
        "image_ocr_async",
        "image_ocr_router",
        "merchant_manual",
    }
    chunk_records: list[Chunk] = []
    for chunk_type, content, metadata_json in chunk_inputs:
        chunk_id = uuid.uuid4()
        if fast_chunk_metadata:
            generated = services.ingestion.metadata_generator._fallback_metadata(content, chunk_type)  # type: ignore[attr-defined]
        else:
            try:
                generated = services.ingestion.metadata_generator.generate(
                    content=content,
                    chunk_type=chunk_type,
                    document_id=str(document.id),
                    chunk_id=str(chunk_id),
                )
            except Exception:
                logger.exception(
                    "Chunk metadata generation failed for document_id=%s source=%s chunk_type=%s; using fallback metadata.",
                    str(document.id),
                    source,
                    chunk_type,
                )
                generated = services.ingestion.metadata_generator._fallback_metadata(content, chunk_type)  # type: ignore[attr-defined]
        chunk_records.append(
            Chunk(
                id=chunk_id,
                document_id=document.id,
                chunk_type=chunk_type,
                content=content,
                summary=generated["summary"],
                keywords=generated["keywords"],
                hypothetical_questions=generated["hypothetical_questions"],
                metadata_json=metadata_json,
            )
        )

    for chunk in chunk_records:
        db.add(chunk)

    review_record: ExtractionReview | None = None
    if references.get("user_id") and str(references.get("user_id")) != "anonymous":
        review_record = ExtractionReview(
            document_id=document.id,
            user_id=str(references["user_id"]),
            status=("pending" if extracted_low_confidence else "confirmed"),
            field_confidences=extraction_confidences,
            low_confidence_fields=extracted_low_confidence,
            extracted_fields=metadata,
            confirmed_fields={},
            reviewer_user_id=(str(references["user_id"]) if not extracted_low_confidence else None),
            reviewed_at=(datetime.now(timezone.utc) if not extracted_low_confidence else None),
        )
        db.add(review_record)

    merchant_user_id = str(references.get("merchant_user_id") or "").strip()
    consumer_user_id = str(references.get("user_id") or "").strip()
    assignment_source = str(references.get("assignment_source") or "").strip()
    if merchant_user_id and consumer_user_id and assignment_source in {"merchant_upload", "merchant_manual"}:
        assignment_audit = MerchantAssignmentAudit(
            document_id=document.id,
            merchant_user_id=merchant_user_id,
            consumer_user_id=consumer_user_id,
            status="assigned",
            assignment_source=assignment_source,
            notes="Auto-created during merchant ingestion workflow",
        )
        db.add(assignment_audit)

    db.commit()
    db.refresh(document)
    if review_record is not None:
        try:
            db.refresh(review_record)
        except Exception:
            pass
    _sync_document_mirror(services, document)
    return document, len(chunk_records)


def _safe_references(document: Document) -> dict:
    references = getattr(document, "references", None)
    return references if isinstance(references, dict) else {}


def _serialize_product_image_state(document: Document) -> DocumentProductImageView:
    references = _safe_references(document)
    payload = references.get("product_image") if isinstance(references.get("product_image"), dict) else {}
    storage_key = str(payload.get("storage_key") or "").strip()
    return DocumentProductImageView(
        docId=str(document.id),
        productImageAvailable=bool(storage_key),
        generatedAt=(str(payload["generated_at"]) if payload.get("generated_at") else None),
        subject=(str(payload["subject"]) if payload.get("subject") else None),
        modelUsed=(str(payload["model_id"]) if payload.get("model_id") else None),
    )


def _schedule_document_notifications(
    db: Session,
    document: Document,
    *,
    consumer_user_id: str | None,
    consumer_email: str | None,
    consumer_name: str | None,
) -> None:
    references = _safe_references(document)
    resolved_user_id = str(consumer_user_id or references.get("user_id") or "").strip()
    resolved_email = str(consumer_email or references.get("consumer_email") or "").strip()
    resolved_name = str(consumer_name or references.get("consumer_name") or "").strip()
    resolved_merchant_user_id = str(references.get("merchant_user_id") or "").strip()
    if not resolved_user_id:
        return
    try:
        _notification_service.schedule_document_notifications(
            db,
            document=document,
            consumer_user_id=resolved_user_id,
            consumer_email=(resolved_email or None),
            consumer_name=(resolved_name or None),
            merchant_user_id=(resolved_merchant_user_id or None),
        )
    except Exception:
        logger.exception(
            "Notification scheduling failed for document_id=%s user_id=%s merchant_user_id=%s",
            str(document.id),
            resolved_user_id,
            resolved_merchant_user_id,
        )
        if hasattr(db, "rollback"):
            try:
                db.rollback()
            except Exception:
                pass


def _cancel_document_notifications(db: Session, *, document_id: UUID) -> None:
    try:
        _notification_service.cancel_document_jobs(db, document_id=document_id)
    except Exception:
        logger.exception(
            "Notification cancel failed for document_id=%s",
            str(document_id),
        )
        if hasattr(db, "rollback"):
            try:
                db.rollback()
            except Exception:
                pass


def _ensure_extraction_review_for_document(db: Session, *, document: Document) -> None:
    existing = db.execute(
        select(ExtractionReview)
        .where(ExtractionReview.document_id == document.id)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return

    references = _safe_references(document).copy()
    user_id = str(references.get("user_id") or "").strip()
    if not user_id or user_id == "anonymous":
        return

    extracted_fields = ensure_strict_extraction(
        {
            "bill_id": document.bill_id,
            "vendor": document.vendor,
            "date": (document.date.isoformat() if document.date else None),
            "total_amount": (float(document.total_amount) if document.total_amount is not None else None),
            "vendor_tax_id": references.get("vendor_tax_id"),
            "taxable_amount": references.get("taxable_amount"),
            "gst_amount": references.get("gst_amount"),
            "gst_rate": references.get("gst_rate"),
            "cgst_amount": references.get("cgst_amount"),
            "sgst_amount": references.get("sgst_amount"),
            "igst_amount": references.get("igst_amount"),
            "product_name": references.get("product_name"),
            "brand": references.get("brand"),
            "serial_number": references.get("serial_number"),
            "warranty_months": references.get("warranty_months"),
            "warranty_start": references.get("warranty_start"),
            "warranty_end": references.get("warranty_end"),
            "category": references.get("category"),
            "line_items": references.get("line_items"),
        }
    )
    confidence_map = (
        references.get("extraction_confidence")
        if isinstance(references.get("extraction_confidence"), dict)
        else {}
    )
    if not confidence_map:
        confidence_map = compute_field_confidences(
            metadata=extracted_fields,
            engine="tesseract_regex",
            text_quality=estimate_text_quality(str(references.get("raw_text") or "")),
        )
    low_conf = build_review_fields(
        confidence_map,
        threshold=float(get_settings().extraction_review_required_threshold),
    )
    review = ExtractionReview(
        document_id=document.id,
        user_id=user_id,
        status=("pending" if low_conf else "confirmed"),
        field_confidences=confidence_map,
        low_confidence_fields=low_conf,
        extracted_fields=extracted_fields,
        confirmed_fields={},
        reviewer_user_id=(user_id if not low_conf else None),
        reviewed_at=(datetime.now(timezone.utc) if not low_conf else None),
    )
    db.add(review)

    references["extraction_confidence"] = confidence_map
    references["low_confidence_fields"] = low_conf
    references["extraction_review_required"] = bool(low_conf)
    references["extraction_review_status"] = ("pending" if low_conf else "confirmed")
    document.references = references
    db.add(document)
    db.commit()
    db.refresh(document)


def _mark_document_consumer_activated(db: Session, *, document: Document, consumer_user_id: str) -> None:
    references = _safe_references(document).copy()
    if str(references.get("user_id") or "").strip() != consumer_user_id:
        return
    if references.get("consumer_activated_at"):
        return
    references["consumer_activated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    references["assignment_status"] = "accepted"
    document.references = references

    assignment_row = db.execute(
        select(MerchantAssignmentAudit)
        .where(MerchantAssignmentAudit.document_id == document.id)
        .where(MerchantAssignmentAudit.consumer_user_id == consumer_user_id)
        .order_by(desc(MerchantAssignmentAudit.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if assignment_row is not None:
        assignment_row.status = "accepted"
        assignment_row.accepted_at = datetime.now(timezone.utc)
        db.add(assignment_row)

    db.add(document)
    db.commit()
    db.refresh(document)


def _normalize_scope_value(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _resolve_notification_user_scope(
    principal: Principal,
    *,
    user_id: str | None = None,
) -> str:
    requested_user_id = _normalize_scope_value(user_id)
    if principal.role in {"consumer", "merchant"}:
        principal_subject = _normalize_scope_value(principal.subject)
        if requested_user_id and principal_subject and requested_user_id != principal_subject:
            raise HTTPException(status_code=403, detail="User scope mismatch.")
        if principal_subject:
            return principal_subject
    if requested_user_id:
        return requested_user_id
    if principal.subject:
        return principal.subject
    raise HTTPException(status_code=400, detail="user_id is required.")


def _notification_preference_hints(
    principal: Principal,
    *,
    user_scope: str,
) -> tuple[str | None, str | None]:
    if not principal.subject or principal.subject != user_scope:
        return None, None
    email_hint = _normalize_scope_value(principal.email)
    full_name_hint = _normalize_scope_value(principal.full_name)
    return email_hint, full_name_hint


def _resolve_document_scope(
    principal: Principal,
    *,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
) -> tuple[str | None, str | None]:
    requested_user_id = _normalize_scope_value(user_id)
    requested_merchant_id = _normalize_scope_value(merchant_user_id)

    if not principal.subject:
        return requested_user_id, requested_merchant_id

    if principal.role == "consumer":
        if requested_merchant_id:
            raise HTTPException(status_code=403, detail="Consumers cannot query merchant scope.")
        if requested_user_id and requested_user_id != principal.subject:
            raise HTTPException(status_code=403, detail="User scope mismatch.")
        return principal.subject, None

    if principal.role == "merchant":
        if requested_merchant_id and requested_merchant_id != principal.subject:
            raise HTTPException(status_code=403, detail="Merchant scope mismatch.")
        return requested_user_id, principal.subject

    return requested_user_id, requested_merchant_id


def _scoped_metadata_filter(principal: Principal, base_filter) -> tuple[str | None, str | None]:
    user_scope, merchant_scope = _resolve_document_scope(
        principal,
        user_id=getattr(base_filter, "user_id", None),
        merchant_user_id=getattr(base_filter, "merchant_user_id", None),
    )
    base_filter.user_id = user_scope
    base_filter.merchant_user_id = merchant_scope
    return user_scope, merchant_scope


def _apply_document_scope(stmt, *, user_id: str | None, merchant_user_id: str | None):
    if user_id:
        stmt = stmt.where(Document.references["user_id"].as_string() == user_id)
    if merchant_user_id:
        stmt = stmt.where(Document.references["merchant_user_id"].as_string() == merchant_user_id)
    return stmt


def _document_in_scope(document: Document, *, user_id: str | None, merchant_user_id: str | None) -> bool:
    references = _safe_references(document)
    if user_id and str(references.get("user_id") or "") != user_id:
        return False
    if merchant_user_id and str(references.get("merchant_user_id") or "") != merchant_user_id:
        return False
    return True


def _shared_members_from_references(references: dict[str, object]) -> list[dict[str, str]]:
    raw = references.get("shared_with")
    if not isinstance(raw, list):
        return []
    members: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        members.append(
            {
                "user_id": user_id[:128],
                "permission": (str(item.get("permission") or "view").strip().lower() or "view")[:16],
                "granted_by": str(item.get("granted_by") or "").strip()[:128],
                "granted_at": str(item.get("granted_at") or "").strip()[:64],
            }
        )
    return members


def _document_is_shared_with(document: Document, user_id: str | None) -> bool:
    if not user_id:
        return False
    references = _safe_references(document)
    for member in _shared_members_from_references(references):
        if member.get("user_id") == user_id:
            return True
    return False


def _can_manage_document_sharing(principal: Principal, document: Document) -> bool:
    if principal.role in {"admin", "analyst"}:
        return True
    subject = str(principal.subject or "").strip()
    if not subject:
        return False
    references = _safe_references(document)
    owner_user_id = str(references.get("user_id") or "").strip()
    owner_merchant_id = str(references.get("merchant_user_id") or "").strip()
    return subject in {owner_user_id, owner_merchant_id}


def _serialize_document_shares(document: Document) -> DocumentShareResponse:
    references = _safe_references(document)
    members = [
        DocumentShareMemberView(
            userId=member["user_id"],
            permission=member.get("permission") or "view",
            grantedBy=(member.get("granted_by") or None),
            grantedAt=(member.get("granted_at") or None),
        )
        for member in _shared_members_from_references(references)
    ]
    return DocumentShareResponse(
        docId=str(document.id),
        ownerUserId=(str(references.get("user_id")) if references.get("user_id") else None),
        sharedWith=members,
    )


def _serialize_extraction_review(review: ExtractionReview) -> ExtractionReviewView:
    return ExtractionReviewView(
        reviewId=str(review.id),
        documentId=str(review.document_id),
        userId=review.user_id,
        status=review.status,
        fieldConfidences=(review.field_confidences if isinstance(review.field_confidences, dict) else {}),
        lowConfidenceFields=(
            review.low_confidence_fields if isinstance(review.low_confidence_fields, list) else []
        ),
        extractedFields=(review.extracted_fields if isinstance(review.extracted_fields, dict) else {}),
        confirmedFields=(review.confirmed_fields if isinstance(review.confirmed_fields, dict) else {}),
        reviewerUserId=review.reviewer_user_id,
        reviewNotes=review.review_notes,
        reviewedAt=(review.reviewed_at.isoformat() if review.reviewed_at else None),
        createdAt=review.created_at.isoformat(),
        updatedAt=review.updated_at.isoformat(),
    )


def _serialize_assignment_audit(audit: MerchantAssignmentAudit) -> MerchantAssignmentAuditView:
    return MerchantAssignmentAuditView(
        assignmentId=str(audit.id),
        documentId=str(audit.document_id),
        merchantUserId=audit.merchant_user_id,
        consumerUserId=audit.consumer_user_id,
        status=audit.status,
        assignmentSource=audit.assignment_source,
        acceptedAt=(audit.accepted_at.isoformat() if audit.accepted_at else None),
        escalatedAt=(audit.escalated_at.isoformat() if audit.escalated_at else None),
        notes=audit.notes,
        createdAt=audit.created_at.isoformat(),
        updatedAt=audit.updated_at.isoformat(),
    )


def _review_in_scope(
    review: ExtractionReview,
    *,
    principal: Principal,
    db: Session,
) -> bool:
    if principal.role in {"admin", "analyst", "auditor", "viewer"}:
        return True
    if principal.role == "consumer":
        return bool(principal.subject and review.user_id == principal.subject)
    if principal.role == "merchant":
        if not principal.subject:
            return False
        document = db.get(Document, review.document_id)
        if document is None:
            return False
        references = _safe_references(document)
        return str(references.get("merchant_user_id") or "").strip() == principal.subject
    return False


def _deadline_band(*, warranty_end: date | None, today: date) -> str | None:
    if warranty_end is None:
        return None
    days_left = (warranty_end - today).days
    if days_left <= 0:
        return "expired"
    if days_left <= 7:
        return "critical"
    if days_left <= 30:
        return "watch"
    return "stable"


def _serialize_document(document: Document) -> DocumentView:
    references = _safe_references(document)
    purchase_date = document.date
    purchase_price = float(document.total_amount) if document.total_amount is not None else None

    warranty_months = _coerce_int(references.get("warranty_months"), default=12)
    warranty_start = _coerce_date(references.get("warranty_start")) or purchase_date
    warranty_end = _coerce_date(references.get("warranty_end"))
    if warranty_end is None and warranty_start and warranty_months:
        warranty_end = add_months(warranty_start, warranty_months)

    purchase_date_iso = purchase_date.isoformat() if purchase_date else None
    warranty_start_iso = warranty_start.isoformat() if warranty_start else None
    warranty_end_iso = warranty_end.isoformat() if warranty_end else None

    product_name_raw = str(references.get("product_name") or references.get("title") or document.bill_id).strip()
    product_name = str(sanitize_merchandise_name(product_name_raw) or product_name_raw or document.bill_id).strip()
    items: list[WarrantyItemView] = []
    reference_items = references.get("line_items")
    cleaned_reference_items: list[dict[str, object]] = []
    if isinstance(reference_items, list):
        for entry in reference_items[:50]:
            if not isinstance(entry, dict):
                continue
            entry_name_raw = str(entry.get("name") or entry.get("product_name") or "").strip()
            if _looks_like_non_merchandise_item_name(entry_name_raw):
                continue
            entry_name = sanitize_merchandise_name(entry_name_raw)
            if not entry_name:
                continue
            normalized_entry = dict(entry)
            normalized_entry["name"] = entry_name
            cleaned_reference_items.append(normalized_entry)

    single_line_item = len(cleaned_reference_items) == 1
    for index, entry in enumerate(cleaned_reference_items, start=1):
        entry_amount = _coerce_float(entry.get("amount"))
        display_amount = (
            purchase_price
            if single_line_item and purchase_price is not None and purchase_price > 0
            else entry_amount
        )
        items.append(
            WarrantyItemView(
                itemId=f"{document.id}:{index}",
                productName=str(entry.get("name") or product_name),
                model=str(references.get("brand") or document.vendor),
                invoiceNo=document.bill_id,
                purchaseDate=purchase_date_iso,
                purchasePrice=display_amount,
                quantity=_coerce_float(entry.get("quantity")),
                unitPrice=_coerce_float(entry.get("unit_price")),
                gstAmount=_coerce_float(entry.get("gst_amount")),
                warrantyMonths=warranty_months,
                warrantyStart=warranty_start_iso,
                warrantyEnd=warranty_end_iso,
                serialNumber=(str(references["serial_number"]) if references.get("serial_number") else None),
                serviceCenters=(
                    references.get("service_centers") if isinstance(references.get("service_centers"), list) else []
                ),
                extendedWarrantyPurchased=_coerce_bool(references.get("extended_warranty_purchased"), default=False),
                notes=(str(references["notes"]) if references.get("notes") else None),
            )
        )

    if not items:
        items = [
            WarrantyItemView(
                itemId=str(document.id),
                productName=product_name,
                model=str(references.get("brand") or document.vendor),
                invoiceNo=document.bill_id,
                purchaseDate=purchase_date_iso,
                purchasePrice=purchase_price,
                gstAmount=_coerce_float(references.get("gst_amount")),
                warrantyMonths=warranty_months,
                warrantyStart=warranty_start_iso,
                warrantyEnd=warranty_end_iso,
                serialNumber=(str(references["serial_number"]) if references.get("serial_number") else None),
                serviceCenters=(
                    references.get("service_centers") if isinstance(references.get("service_centers"), list) else []
                ),
                extendedWarrantyPurchased=_coerce_bool(references.get("extended_warranty_purchased"), default=False),
                notes=(str(references["notes"]) if references.get("notes") else None),
            )
        ]

    today = date.today()
    status = "expired" if warranty_end and warranty_end < today else "active"
    extraction_confidence = (
        references.get("extraction_confidence")
        if isinstance(references.get("extraction_confidence"), dict)
        else {}
    )
    low_confidence_fields = (
        references.get("low_confidence_fields")
        if isinstance(references.get("low_confidence_fields"), list)
        else []
    )
    review_required = bool(
        references.get("extraction_review_required")
        or (isinstance(low_confidence_fields, list) and len(low_confidence_fields) > 0)
    )
    review_status = str(
        references.get("extraction_review_status")
        or ("pending" if review_required else "confirmed")
    )
    service_centers = references.get("service_centers") if isinstance(references.get("service_centers"), list) else []
    claim_readiness_payload = estimate_claim_readiness(
        warranty_end=warranty_end,
        now=today,
        has_invoice_number=bool(document.bill_id.strip()),
        has_vendor=bool(document.vendor and document.vendor != "UNKNOWN_VENDOR"),
        has_purchase_date=bool(purchase_date),
        has_amount=(purchase_price is not None and purchase_price > 0),
        has_serial=bool(references.get("serial_number")),
        has_service_centers=bool(service_centers),
    )
    deadline_band = _deadline_band(warranty_end=warranty_end, today=today)
    compliance_payload = references.get("compliance") if isinstance(references.get("compliance"), dict) else None
    if compliance_payload is None:
        compliance_payload = validate_invoice_compliance(
            metadata={
                "bill_id": document.bill_id,
                "vendor": document.vendor,
                "date": (purchase_date.isoformat() if purchase_date else None),
                "total_amount": purchase_price,
                "vendor_tax_id": references.get("vendor_tax_id"),
                "taxable_amount": references.get("taxable_amount"),
                "gst_amount": references.get("gst_amount"),
                "gst_rate": references.get("gst_rate"),
                "cgst_amount": references.get("cgst_amount"),
                "sgst_amount": references.get("sgst_amount"),
                "igst_amount": references.get("igst_amount"),
                "line_items": references.get("line_items"),
            },
            raw_text=str(references.get("raw_text") or ""),
        )

    return DocumentView(
        docId=str(document.id),
        userId=str(references.get("user_id") or "anonymous"),
        title=str(references.get("title") or product_name),
        items=items,
        createdAt=document.created_at.isoformat(),
        updatedAt=document.created_at.isoformat(),
        rawText=(str(references["raw_text"]) if references.get("raw_text") else None),
        status=status,
        sellerName=document.vendor,
        ocrConfidence=_coerce_float(references.get("ocr_confidence")),
        isVerified=_coerce_bool(references.get("is_verified"), default=True),
        category=str(references.get("category") or "Others"),
        source=(str(references["source"]) if references.get("source") else None),
        assignedByMerchantId=(str(references["merchant_user_id"]) if references.get("merchant_user_id") else None),
        assignedByMerchantName=(str(references["merchant_name"]) if references.get("merchant_name") else None),
        assignedByMerchantCustomId=(
            str(references["merchant_custom_id"]) if references.get("merchant_custom_id") else None
        ),
        consumerCustomId=(str(references["consumer_custom_id"]) if references.get("consumer_custom_id") else None),
        assignmentStatus=(str(references["assignment_status"]) if references.get("assignment_status") else None),
        assignmentAcceptedAt=(
            str(references["consumer_activated_at"]) if references.get("consumer_activated_at") else None
        ),
        assignmentEscalatedAt=(
            str(references["consumer_escalated_at"]) if references.get("consumer_escalated_at") else None
        ),
        totalAmount=purchase_price,
        taxableAmount=_coerce_float(references.get("taxable_amount")),
        gstAmount=_coerce_float(references.get("gst_amount")),
        gstRate=_coerce_float(references.get("gst_rate")),
        cgstAmount=_coerce_float(references.get("cgst_amount")),
        sgstAmount=_coerce_float(references.get("sgst_amount")),
        igstAmount=_coerce_float(references.get("igst_amount")),
        extractionConfidence={
            str(key): float(value)
            for key, value in extraction_confidence.items()
            if _coerce_float(value) is not None
        },
        reviewStatus=review_status,
        reviewRequired=review_required,
        lowConfidenceFields=[str(field) for field in low_confidence_fields],
        claimReadiness=claim_readiness_payload,
        deadlineBand=deadline_band,
        compliance=compliance_payload,
        productImageAvailable=bool(
            isinstance(references.get("product_image"), dict)
            and str((references.get("product_image") or {}).get("storage_key") or "").strip()
        ),
        productImageGeneratedAt=(
            str((references.get("product_image") or {}).get("generated_at"))
            if isinstance(references.get("product_image"), dict)
            and (references.get("product_image") or {}).get("generated_at")
            else None
        ),
    )


def _document_view_in_scope(
    view: DocumentView,
    *,
    user_id: str | None,
    merchant_user_id: str | None,
) -> bool:
    if user_id and str(view.userId or "") != user_id:
        return False
    if merchant_user_id and str(view.assignedByMerchantId or "") != merchant_user_id:
        return False
    return True


def _sync_document_mirror(services: ServiceRegistry, document: Document) -> None:
    store = getattr(services, "dynamodb_store", None)
    if store is None or not getattr(store, "enabled", False):
        return
    try:
        payload = _response_model_payload(_serialize_document(document))
        store.upsert_document_record(payload=payload)
    except Exception:
        logger.exception("Failed to mirror document_id=%s into DynamoDB.", str(document.id))


def _sync_async_extraction_job_mirror(services: ServiceRegistry, job: ExtractionJob) -> None:
    store = getattr(services, "dynamodb_store", None)
    if store is None or not getattr(store, "enabled", False):
        return
    try:
        payload = _response_model_payload(_serialize_async_extraction_job(job))
        store.upsert_extraction_job_record(
            payload=payload,
            user_id=job.user_id,
            merchant_user_id=job.merchant_user_id,
        )
    except Exception:
        logger.exception("Failed to mirror extraction job_id=%s into DynamoDB.", str(job.id))


def _list_document_views_from_mirror(
    services: ServiceRegistry,
    *,
    user_id: str | None,
    merchant_user_id: str | None,
    limit: int,
) -> list[DocumentView]:
    store = getattr(services, "dynamodb_store", None)
    if (
        store is None
        or not getattr(store, "enabled", False)
        or not getattr(store, "read_fallback_enabled", False)
    ):
        return []
    views: list[DocumentView] = []
    for record in store.list_document_records(
        user_id=user_id,
        merchant_user_id=merchant_user_id,
        limit=limit,
    ):
        try:
            view = DocumentView(**record.payload)
        except Exception:
            logger.exception("Failed to parse mirrored document payload for DynamoDB fallback.")
            continue
        if _document_view_in_scope(view, user_id=user_id, merchant_user_id=merchant_user_id):
            views.append(view)
    return views[: max(1, int(limit))]


def _load_document_view_from_mirror(
    services: ServiceRegistry,
    *,
    doc_id: UUID,
    user_id: str | None,
    merchant_user_id: str | None,
) -> DocumentView | None:
    store = getattr(services, "dynamodb_store", None)
    if (
        store is None
        or not getattr(store, "enabled", False)
        or not getattr(store, "read_fallback_enabled", False)
    ):
        return None
    record = store.get_document_record(str(doc_id))
    if record is None:
        return None
    try:
        view = DocumentView(**record.payload)
    except Exception:
        logger.exception("Failed to parse mirrored document payload doc_id=%s", str(doc_id))
        return None
    if not _document_view_in_scope(view, user_id=user_id, merchant_user_id=merchant_user_id):
        return None
    return view


def _load_async_job_status_from_mirror(
    services: ServiceRegistry,
    *,
    job_id: UUID,
    user_id: str | None,
    merchant_user_id: str | None,
) -> AsyncExtractionJobStatusResponse | None:
    store = getattr(services, "dynamodb_store", None)
    if (
        store is None
        or not getattr(store, "enabled", False)
        or not getattr(store, "read_fallback_enabled", False)
    ):
        return None
    record = store.get_extraction_job_record(str(job_id))
    if record is None:
        return None
    if user_id and str(record.user_id or "") != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if merchant_user_id and str(record.merchant_user_id or "") != merchant_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        return AsyncExtractionJobStatusResponse(**record.payload)
    except Exception:
        logger.exception("Failed to parse mirrored extraction job payload job_id=%s", str(job_id))
        return None


def _manual_bill_text_payload(request: MerchantManualBillRequest, resolved_bill_id: str, resolved_vendor: str) -> str:
    lines = [
        f"Invoice Number: {resolved_bill_id}",
        f"Merchant: {resolved_vendor}",
        f"Product: {request.product_name}",
    ]
    if request.consumer_name:
        lines.append(f"Consumer: {request.consumer_name}")
    if request.purchase_date:
        lines.append(f"Purchase Date: {request.purchase_date.isoformat()}")
    if request.total_amount is not None:
        lines.append(f"Total Amount: {request.total_amount:.2f}")
    if request.warranty_months:
        lines.append(f"Warranty Months: {request.warranty_months}")
    if request.serial_number:
        lines.append(f"Serial Number: {request.serial_number}")
    if request.notes:
        lines.append(f"Notes: {request.notes}")
    return "\n".join(lines)


def _merchant_activity_action(source: str, assignment_source: str) -> str:
    if source == "merchant_manual":
        return "generated"
    if assignment_source == "merchant_reassign":
        return "reassigned"
    return "uploaded"


def _ics_timestamp(dt: datetime) -> str:
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


def _build_warranty_ics(*, uid: str, title: str, description: str, start_at: datetime, end_at: datetime) -> str:
    stamp = _ics_timestamp(datetime.now(timezone.utc))
    formatted_desc = description.replace(chr(10), "\\n")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SafeBill//Warranty Reminder//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_ics_timestamp(start_at)}",
        f"DTEND:{_ics_timestamp(end_at)}",
        f"SUMMARY:{title}",
        f"DESCRIPTION:{formatted_desc}",
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
    return "\r\n".join(lines)


def _build_claim_packet_payload(document: Document, view: DocumentView) -> ClaimPacketResponse:
    references = _safe_references(document)
    item = view.items[0] if view.items else None
    warranty_end = _coerce_date(item.warrantyEnd if item else None)
    purchase_date = _coerce_date(item.purchaseDate if item else None)
    timeline: list[str] = []
    if purchase_date:
        timeline.append(f"{purchase_date.isoformat()}: Product purchased.")
    if item and item.warrantyStart:
        timeline.append(f"{item.warrantyStart}: Warranty coverage started.")
    if warranty_end:
        timeline.append(f"{warranty_end.isoformat()}: Warranty coverage ends.")
    if view.createdAt:
        timeline.append(f"{view.createdAt[:10]}: Bill indexed in SafeBill.")

    facts = {
        "invoice_number": item.invoiceNo if item else None,
        "product_name": item.productName if item else view.title,
        "brand": item.model if item else references.get("brand"),
        "vendor": view.sellerName,
        "purchase_date": item.purchaseDate if item else None,
        "purchase_price": item.purchasePrice if item else None,
        "serial_number": item.serialNumber if item else references.get("serial_number"),
        "warranty_end": item.warrantyEnd if item else None,
        "consumer_id": view.userId,
    }

    issue_template = (
        "Issue Summary:\n"
        "- Device/Product: {product}\n"
        "- Problem observed: <describe malfunction>\n"
        "- First observed date: <YYYY-MM-DD>\n"
        "- Troubleshooting already tried: <steps>\n"
        "- Preferred resolution: repair / replacement / refund"
    ).format(product=facts.get("product_name") or "Product")

    email_template = (
        "Subject: Warranty Claim Request - {invoice}\n\n"
        "Hello {vendor_team},\n\n"
        "I am raising a warranty claim for {product} (Invoice: {invoice}). "
        "The product was purchased on {purchase_date} and is within coverage until {warranty_end}.\n\n"
        "Issue details:\n<add issue summary>\n\n"
        "Please guide me with next steps and required service center/process.\n\n"
        "Regards,\n{consumer_id}"
    ).format(
        invoice=facts.get("invoice_number") or "N/A",
        vendor_team=facts.get("vendor") or "Support Team",
        product=facts.get("product_name") or "Product",
        purchase_date=facts.get("purchase_date") or "N/A",
        warranty_end=facts.get("warranty_end") or "N/A",
        consumer_id=facts.get("consumer_id") or "Consumer",
    )

    checklist = [
        "Invoice copy with invoice number clearly visible",
        "Product serial number photo",
        "Issue photos/videos",
        "Original packaging details (if available)",
        "Previous service ticket references (if any)",
        "Government ID proof (if requested by vendor)",
    ]

    return ClaimPacketResponse(
        docId=view.docId,
        generatedAt=datetime.now(timezone.utc).isoformat(),
        facts=facts,
        timeline=timeline,
        issueSummaryTemplate=issue_template,
        emailTemplate=email_template,
        attachmentChecklist=checklist,
    )


def _claim_next_best_actions(view: DocumentView) -> list[str]:
    readiness = view.claimReadiness
    if readiness is None:
        return ["Review extracted invoice fields before initiating claim."]
    actions = list(readiness.recommendedActions or [])
    if view.deadlineBand in {"expired", "critical", "watch"}:
        actions.append("Use claim packet and contact support channel immediately.")
    if not actions:
        actions.append("Claim packet is ready. Submit via vendor support.")
    return actions[:6]


def _reminder_action_from_days(days_remaining: int | None) -> str:
    if days_remaining is None:
        return "Review warranty details."
    if days_remaining <= 0:
        return "Warranty expired. Check grace-period claim options."
    if days_remaining <= 7:
        return "Raise claim now and attach issue photos."
    if days_remaining <= 30:
        return "Prepare claim packet and book service center visit."
    return "Set up follow-up reminder and keep documents ready."


def _compute_fraud_signals(document: Document, view: DocumentView) -> list[FraudSignalView]:
    references = _safe_references(document)
    signals: list[FraudSignalView] = []

    if bool(references.get("duplicate_suspected")):
        count = int(_coerce_int(references.get("duplicate_match_count"), default=1) or 1)
        signals.append(
            FraudSignalView(
                code="DUPLICATE_SUSPECTED",
                severity=("high" if count > 1 else "medium"),
                detail=f"Possible duplicate detected with {count} similar record(s).",
            )
        )

    compliance = view.compliance.model_dump() if view.compliance is not None else {}
    alerts = compliance.get("alerts") if isinstance(compliance, dict) else []
    if isinstance(alerts, list):
        for item in alerts[:6]:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            severity = str(item.get("severity") or "low").strip().lower()
            message = str(item.get("message") or "").strip()
            if not code:
                continue
            signals.append(
                FraudSignalView(
                    code=f"COMPLIANCE_{code}",
                    severity=("high" if severity == "high" else ("medium" if severity == "medium" else "low")),
                    detail=message or "Compliance anomaly found.",
                )
            )

    references_fingerprint = str(references.get("extraction_fingerprint") or "").strip()
    if not references_fingerprint:
        signals.append(
            FraudSignalView(
                code="TRACEABILITY_WEAK",
                severity="low",
                detail="Extraction fingerprint missing; source traceability should be reviewed.",
            )
        )

    return signals


def _fraud_status_from_score(score: float) -> str:
    if score >= 0.66:
        return "high_risk"
    if score >= 0.33:
        return "watch"
    return "low_risk"


def _renewal_options_for_document(view: DocumentView, *, currency: str = "INR") -> list[RenewalOptionView]:
    item = view.items[0] if view.items else None
    base_price = float(item.purchasePrice) if item and item.purchasePrice is not None else 0.0
    category = str(view.category or "Others").strip().lower()
    category_multiplier = 1.0
    if category in {"gadgets", "electronics"}:
        category_multiplier = 1.15
    elif category in {"appliances"}:
        category_multiplier = 1.0
    elif category in {"vehicle"}:
        category_multiplier = 1.35

    if base_price <= 0:
        base_price = 5000.0

    plan_specs = [
        ("basic", "SafeBill Protect Basic", 12, 0.045, "Repairs for manufacturing defects and parts replacement."),
        ("plus", "SafeBill Protect Plus", 24, 0.075, "Includes accidental damage support and doorstep pickup."),
        ("premium", "SafeBill Protect Premium", 36, 0.11, "Comprehensive coverage with priority turnaround."),
    ]
    options: list[RenewalOptionView] = []
    for idx, (plan_id, name, months, rate, summary) in enumerate(plan_specs):
        premium = round(base_price * rate * category_multiplier, 2)
        partner_code = "sbx_guardian"
        webhook_ref = f"renewal_{plan_id}_{int(time.time())}"
        options.append(
            RenewalOptionView(
                planId=plan_id,
                partnerCode=partner_code,
                provider="SafeBill Marketplace",
                planName=name,
                extensionMonths=months,
                estimatedPremium=max(premium, 299.0),
                currency=currency,
                coverageSummary=summary,
                recommended=(idx == 1),
                quoteUrl=f"/api/v1/marketplace/renewal/quote?plan_id={plan_id}",
                purchaseUrl="/api/v1/marketplace/renewal/purchase-intent",
                webhookRef=webhook_ref,
            )
        )
    return options


def _renewal_webhook_ref(*, doc_id: str, plan_id: str, partner_code: str) -> str:
    raw = f"{doc_id}:{plan_id}:{partner_code}".encode("utf-8", errors="ignore")
    digest = hashlib.sha256(raw).hexdigest()[:24]
    return f"renewal_{digest}"


def _clean_company_token(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"(?i)^\s*(?:name|brand|company)\s*[:=\-]\s*", "", text)
    text = text.split(",", 1)[0].strip()
    text = re.sub(r"\s+", " ", text).strip(" .,:;-'\"!?")
    text = re.sub(r"^[`'\"]+|[`'\"]+$", "", text).strip()
    if not text:
        return None

    leading_stopwords = {
        "this",
        "my",
        "our",
        "the",
        "a",
        "an",
        "is",
        "of",
        "for",
        "from",
        "find",
        "show",
        "where",
        "nearest",
        "nearby",
        "product",
        "item",
        "device",
        "name",
        "brand",
        "company",
        "model",
    }
    trailing_stopwords = {
        "company",
        "brand",
        "product",
        "item",
        "device",
        "service",
        "repair",
        "support",
        "center",
        "centre",
        "nearest",
        "nearby",
        "which",
        "where",
        "is",
        "in",
        "near",
        "around",
        "within",
        "range",
        "radius",
    }
    words = text.split(" ")
    while words and words[0].lower() in leading_stopwords:
        words = words[1:]
    while words and words[-1].lower() in trailing_stopwords:
        words = words[:-1]
    text = " ".join(words).strip(" .,:;-'\"!?")
    if not text:
        return None

    lowered = text.lower()
    invalid = {
        "",
        "unknown",
        "unknown_vendor",
        "service center",
        "service centre",
        "repair center",
        "repair centre",
        "authorized service center",
    }
    if lowered in invalid:
        return None
    if len(text) < 2:
        return None
    return text[:120]


def _query_company_candidates(query: str) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r"(?i)\b([a-z0-9][a-z0-9&.\-\s]{1,80})\s+service\s+cent(?:er|re)\b",
        r"(?i)\b(?:product|item|device|brand|company(?:\s+name)?)\s*(?:is|=|:|of|from)\s+([a-z0-9][a-z0-9&.\-\s]{1,80}?)(?=\b(?:and|nearest|nearby|service|repair|support|in|near|within|around|range|distance|for|where|which|city|km|miles)\b|[?.!,]|$)",
        r"(?i)\b(?:of|for|from)\s+([a-z0-9][a-z0-9&.\-\s]{1,80}?)(?=\b(?:service|repair|support|center|centre|in|near|within|around|range|distance|where|which|city|km|miles)\b|[?.!,]|$)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, query):
            candidate = _clean_company_token(match.group(1))
            if candidate:
                candidates.append(candidate)
    return candidates


def _query_location_hint(query: str) -> str | None:
    pincode_match = re.search(r"\b([1-9][0-9]{5})\b", query)
    if pincode_match:
        return pincode_match.group(1)
    patterns = [
        r"(?i)\b(?:in|near|around|at)\s+([a-z][a-z.\-\s]{1,60})(?=\b(?:for|within|range|radius|km|miles|service|center|centre|repair|support)\b|[?.!,]|$)",
        r"(?i)\bcity(?:\s+name)?\s*(?:is|:)?\s*([a-z][a-z.\-\s]{1,60})(?=[?.!,]|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, query)
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;-")
        if len(candidate) >= 2:
            return candidate[:80]
    return None


def _hit_company_candidates(hits: list) -> list[str]:
    candidates: list[str] = []
    for hit in hits[:12]:
        vendor = _clean_company_token(getattr(hit, "vendor", None))
        if vendor:
            candidates.append(vendor)

        metadata = getattr(hit, "metadata", {})
        if isinstance(metadata, dict):
            for key in ("brand", "vendor", "product_name", "seller", "store"):
                candidate = _clean_company_token(metadata.get(key))
                if candidate:
                    candidates.append(candidate)

        if getattr(hit, "chunk_type", "") == "invoice_metadata":
            try:
                payload = json.loads(getattr(hit, "content", "") or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                for key in ("brand", "vendor", "product_name"):
                    candidate = _clean_company_token(payload.get(key))
                    if candidate:
                        candidates.append(candidate)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _resolve_company_name(query: str, hits: list, filter_vendor: str | None = None) -> str | None:
    query_sources = _query_company_candidates(query)
    if query_sources:
        return query_sources[0]

    sources: list[str] = []
    filter_candidate = _clean_company_token(filter_vendor)
    if filter_candidate:
        sources.append(filter_candidate)
    sources.extend(_hit_company_candidates(hits))

    deduped: list[str] = []
    seen: set[str] = set()
    for source in sources:
        key = source.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped[0] if deduped else None


def _format_service_centers_block(
    base_answer: str,
    *,
    company_name: str | None,
    centers: list[ServiceCenterCandidate],
    has_user_location: bool,
    radius_km: float | None,
) -> str:
    if centers:
        cleaned_label = re.sub(r"(?i)^\s*(?:name|brand|company)\s*[:=\-]\s*", "", (company_name or "")).strip()
        company_label = cleaned_label if cleaned_label else (company_name if company_name else "the requested")
        header = f"Found service centers for {company_label}"
        if radius_km is not None:
            header = f"{header} (range: {radius_km:.0f} km)"
        lines: list[str] = [f"{header}:"]
        for index, center in enumerate(centers, start=1):
            distance_text = (
                f" ({center.distance_km:.2f} km away)"
                if center.distance_km is not None
                else " (distance unavailable)"
            )
            confidence_text = f"[{center.confidence}]"
            contact_parts: list[str] = []
            if center.phone:
                contact_parts.append(f"Phone: {center.phone}")
            if center.website:
                contact_parts.append(f"Website: {center.website}")
            if center.map_url:
                contact_parts.append(f"Maps: {center.map_url}")
            service_parts: list[str] = []
            if center.pincode:
                service_parts.append(f"Pincode: {center.pincode}")
            if center.pickup_available is True:
                service_parts.append("Pickup: Available")
            elif center.pickup_available is False:
                service_parts.append("Pickup: Call center to confirm")
            if center.estimated_tat_days is not None:
                service_parts.append(f"Estimated TAT: ~{center.estimated_tat_days} day(s)")
            lines.append(f"{index}. {center.name} {confidence_text}")
            lines.append(f"   Address: {center.address}{distance_text}")
            if service_parts:
                lines.append(f"   {' | '.join(service_parts)}")
            if contact_parts:
                lines.append(f"   {' | '.join(contact_parts)}")
        return "\n".join(lines)

    if company_name:
        if has_user_location:
            guidance = (
                f"I could not find nearby {company_name} service centers in the selected range. "
                "Try a larger range or share city name with correct spelling."
            )
        else:
            guidance = (
                f"I could not find {company_name} service centers for this place yet. "
                "Try adding state/city or 6-digit pincode clearly (for example: Delhi, 560001) or increase range."
            )
    else:
        guidance = "Please include the company name (for example: Samsung, LG, Sony) and city/state."
    return guidance


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/examples/queries")
def example_queries() -> dict[str, list[str]]:
    return {
        "examples": [
            "Show invoices where GST was incorrectly calculated above ₹50,000",
            "Compare Q3 marketing bills with Q2 and highlight outliers",
            "List all invoices missing vendor tax IDs",
        ]
    }


@router.post("/ingest/pdf", response_model=IngestPDFResponse)
async def ingest_pdf(
    request: Request,
    file: UploadFile = File(...),
    bill_id: str | None = Form(default=None),
    vendor: str | None = Form(default=None),
    document_date: date | None = Form(default=None),
    total_amount: float | None = Form(default=None),
    ocr_mode: str | None = Form(default=None),
    user_id: str | None = Form(default=None),
    consumer_custom_id: str | None = Form(default=None),
    consumer_name: str | None = Form(default=None),
    consumer_email: str | None = Form(default=None),
    merchant_user_id: str | None = Form(default=None),
    merchant_name: str | None = Form(default=None),
    merchant_custom_id: str | None = Form(default=None),
    version: int = Form(default=1),
    principal: Principal = Depends(require_roles("admin", "analyst", "merchant", "consumer")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> IngestPDFResponse:
    _rate_limit_or_429(
        request=request,
        principal=principal,
        bucket="ingest_pdf",
        limit=get_settings().api_rate_limit_ingest_per_window,
    )
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )

    if principal.role == "consumer":
        user_id = principal.subject
    if principal.role == "merchant":
        merchant_user_id = principal.subject
        if not user_id:
            raise HTTPException(status_code=400, detail="consumer user_id is required for merchant ingestion.")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a PDF document.")
    payload = await file.read()
    storage_references = _store_source_blob(
        services=services,
        payload=payload,
        filename=file.filename,
        source="ingest_pdf",
        principal=principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    parsed = parse_pdf_document(file_bytes=payload, filename=file.filename, ocr_mode_override=ocr_mode)
    parsed_metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
    resolved_pdf_text = str(getattr(parsed, "raw_text", "") or "").strip()
    if not resolved_pdf_text:
        resolved_pdf_text = _metadata_to_canonical_text(parsed_metadata)
    ocr_snapshot_references = _store_ocr_text_snapshot(
        services=services,
        extracted_text=resolved_pdf_text,
        filename=file.filename,
        source="ingest_pdf",
        document_user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document_type_payload = _enforce_document_text_classification(
        services=services,
        filename=file.filename,
        snapshot_references=ocr_snapshot_references,
        fallback_text=resolved_pdf_text,
    )
    fingerprint = extraction_fingerprint(parsed_metadata, resolved_pdf_text)
    identifier_payload = _extract_document_identifiers(parsed_metadata, resolved_pdf_text)
    identifier_raw = list(identifier_payload.get("identifiers_raw") or [])
    identifier_norm = list(identifier_payload.get("identifiers_norm") or [])
    owner_field = None
    owner_value = ""
    if user_id:
        owner_field = Document.references["user_id"].as_string()
        owner_value = user_id
    elif merchant_user_id:
        owner_field = Document.references["merchant_user_id"].as_string()
        owner_value = merchant_user_id
    if owner_field is not None and owner_value:
        duplicate_conditions: list[object] = [
            Document.references["extraction_fingerprint"].as_string() == fingerprint,
        ]
        if identifier_raw:
            duplicate_conditions.append(Document.bill_id.in_(identifier_raw))
        if identifier_norm:
            duplicate_conditions.append(Document.references["invoice_number_norm"].as_string().in_(identifier_norm))
            duplicate_conditions.append(Document.references["order_number_norm"].as_string().in_(identifier_norm))
            duplicate_conditions.append(Document.references["warranty_number_norm"].as_string().in_(identifier_norm))
            duplicate_conditions.append(Document.references["serial_number_norm"].as_string().in_(identifier_norm))
        duplicate_stmt = select(func.count(Document.id)).where(
            owner_field == owner_value,
            or_(*duplicate_conditions),
        )
        duplicate_count = int(db.execute(duplicate_stmt).scalar_one_or_none() or 0)
        if duplicate_count > 0:
            raise HTTPException(
                status_code=409,
                detail="Duplicate bill detected. This bill already exists in your locker.",
            )
    has_invoice_signals = any(
        _is_meaningful_metadata_value(parsed_metadata.get(key))
        for key in ("bill_id", "vendor", "total_amount", "date")
    )
    if resolved_pdf_text:
        classification = _classify_document_with_bedrock(resolved_pdf_text, file.filename)
        doc_is_invoice = classification.get("is_invoice") if classification else None
        doc_confidence = _coerce_float(classification.get("confidence"), default=0.0) if classification else 0.0
        doc_type = str(classification.get("document_type") or "").strip().lower() if classification else ""
        is_allowed_doc_type = doc_type in {"warranty_card", "guarantee_card"}
        if doc_is_invoice is False and not is_allowed_doc_type and doc_confidence >= 0.75:
            raise HTTPException(
                status_code=422,
                detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
            )
        if doc_is_invoice is None or doc_is_invoice is False:
            if not is_allowed_doc_type:
                heuristic_is_invoice, heuristic_confidence = _heuristic_is_invoice_document(resolved_pdf_text)
                if not heuristic_is_invoice and heuristic_confidence >= 0.8:
                    raise HTTPException(
                        status_code=422,
                        detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
                    )
    references: dict[str, object] = {
        "filename": file.filename,
        "source": "pdf",
        "is_verified": True,
    }
    if ocr_snapshot_references:
        references.update(ocr_snapshot_references)
    if document_type_payload:
        references.update(document_type_payload)
    if fingerprint:
        references["extraction_fingerprint"] = fingerprint
    if identifier_payload:
        if identifier_payload.get("invoice_number") and not references.get("invoice_number"):
            references["invoice_number"] = identifier_payload["invoice_number"]
        if identifier_payload.get("order_number") and not references.get("order_number"):
            references["order_number"] = identifier_payload["order_number"]
        if identifier_payload.get("warranty_number") and not references.get("warranty_number"):
            references["warranty_number"] = identifier_payload["warranty_number"]
        if identifier_payload.get("serial_number") and not references.get("serial_number"):
            references["serial_number"] = identifier_payload["serial_number"]
        if identifier_payload.get("identifiers_raw"):
            references["document_identifiers"] = identifier_payload["identifiers_raw"]
        if identifier_payload.get("identifiers_norm"):
            references["document_identifiers_norm"] = identifier_payload["identifiers_norm"]
        if identifier_payload.get("invoice_number"):
            references["invoice_number_norm"] = _normalize_identifier_value(identifier_payload["invoice_number"])
        if identifier_payload.get("order_number"):
            references["order_number_norm"] = _normalize_identifier_value(identifier_payload["order_number"])
        if identifier_payload.get("warranty_number"):
            references["warranty_number_norm"] = _normalize_identifier_value(identifier_payload["warranty_number"])
        if identifier_payload.get("serial_number"):
            references["serial_number_norm"] = _normalize_identifier_value(identifier_payload["serial_number"])
    if storage_references:
        references.update(storage_references)
    if user_id:
        references["user_id"] = user_id
    if consumer_custom_id:
        references["consumer_custom_id"] = consumer_custom_id
    if consumer_name:
        references["consumer_name"] = consumer_name
    if consumer_email:
        references["consumer_email"] = consumer_email
    if merchant_user_id:
        references["merchant_user_id"] = merchant_user_id
    if merchant_name:
        references["merchant_name"] = merchant_name
    if merchant_custom_id:
        references["merchant_custom_id"] = merchant_custom_id
    if principal.role == "merchant" and principal.email:
        references["merchant_email"] = principal.email
    if merchant_user_id and user_id:
        references["assignment_source"] = "merchant_upload"
    document, chunk_count = services.ingestion.ingest_pdf(
        db=db,
        file_bytes=payload,
        filename=file.filename,
        bill_id=bill_id,
        vendor=vendor,
        document_date=document_date,
        total_amount=total_amount,
        ocr_mode=ocr_mode,
        version=version,
        references=references,
        parsed=parsed,
    )
    _ensure_extraction_review_for_document(db, document=document)
    _schedule_document_notifications(
        db,
        document,
        consumer_user_id=user_id,
        consumer_email=consumer_email,
        consumer_name=consumer_name,
    )
    _log_security_event(
        db,
        event_type="document.ingest_pdf",
        principal=principal,
        resource=f"documents/{document.id}",
        request=request,
        metadata={
            "bill_id": document.bill_id,
            "merchant_user_id": merchant_user_id,
            "user_id": user_id,
        },
    )
    return IngestPDFResponse(
        document_id=document.id,
        chunk_count=chunk_count,
        bill_id=document.bill_id,
        vendor=document.vendor,
        created_at=document.created_at,
    )


@router.post("/extraction-jobs/image", response_model=AsyncExtractionJobCreateResponse)
async def create_image_extraction_job(
    request: Request,
    file: UploadFile = File(...),
    bill_id: str | None = Form(default=None),
    vendor: str | None = Form(default=None),
    document_date: date | None = Form(default=None),
    total_amount: float | None = Form(default=None),
    user_id: str | None = Form(default=None),
    consumer_email: str | None = Form(default=None),
    consumer_name: str | None = Form(default=None),
    merchant_user_id: str | None = Form(default=None),
    merchant_name: str | None = Form(default=None),
    merchant_custom_id: str | None = Form(default=None),
    ocr_mode: str | None = Form(default=None),
    principal: Principal = Depends(require_roles("admin", "analyst", "merchant", "consumer")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> AsyncExtractionJobCreateResponse:
    _rate_limit_or_429(
        request=request,
        principal=principal,
        bucket="ingest_image",
        limit=get_settings().api_rate_limit_ingest_per_window,
    )
    if not _async_extraction_enabled():
        raise HTTPException(status_code=503, detail="Async extraction pipeline is not enabled.")

    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    if principal.role == "consumer":
        user_id = principal.subject
    if principal.role == "merchant":
        merchant_user_id = principal.subject
        if not user_id:
            raise HTTPException(status_code=400, detail="consumer user_id is required for merchant ingestion.")

    filename = file.filename or "uploaded-image"
    lowered = filename.lower()
    is_image = lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"))
    if file.content_type and file.content_type.lower().startswith("image/"):
        is_image = True
    if not is_image:
        raise HTTPException(status_code=400, detail="Upload an image document.")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded image is empty.")

    settings = get_settings()
    store = getattr(services, "object_store", None)
    local_worker_enabled = _local_async_extraction_worker_enabled()
    if (store is None or not getattr(store, "enabled", False)) and not local_worker_enabled:
        raise HTTPException(status_code=503, detail="Async extraction requires S3 or the local async worker.")

    job_id = uuid.uuid4()
    source_prefix = str(settings.async_extraction_source_prefix or "async-extraction").strip() or "async-extraction"
    key = store.build_object_key(filename=filename, source=source_prefix) if store is not None and getattr(store, "enabled", False) else ""
    content_type = store.guess_content_type(filename) if store is not None and getattr(store, "enabled", False) else (file.content_type or "image/png")
    request_metadata = {
        "bill_id": str(bill_id or "").strip(),
        "vendor": str(vendor or "").strip(),
        "document_date": (document_date.isoformat() if document_date else None),
        "total_amount": total_amount,
        "consumer_email": str(consumer_email or "").strip(),
        "consumer_name": str(consumer_name or "").strip(),
        "merchant_name": str(merchant_name or "").strip(),
        "merchant_custom_id": str(merchant_custom_id or "").strip(),
        "ocr_mode": str(ocr_mode or "").strip() or str(settings.async_extraction_ocr_mode or "hybrid"),
    }
    if local_worker_enabled:
        request_metadata["inline_image_base64"] = base64.b64encode(payload).decode("ascii")
    storage_metadata = {
        "job_id": str(job_id),
        "filename": filename,
        "user_id": str(user_id or ""),
        "merchant_user_id": str(merchant_user_id or ""),
    }
    uploaded: dict[str, object] | None = None
    if store is not None and getattr(store, "enabled", False):
        try:
            uploaded = store.put_bytes(
                key=key,
                payload=payload,
                filename=filename,
                content_type=content_type,
                metadata=storage_metadata,
            )
        except Exception as exc:
            logger.exception("Async extraction S3 upload failed filename=%s", filename)
            if not local_worker_enabled:
                raise HTTPException(status_code=500, detail=f"Async extraction upload failed: {exc}") from exc
            uploaded = None
        if not uploaded and not local_worker_enabled:
            raise HTTPException(status_code=500, detail="Async extraction upload failed.")

    job = ExtractionJob(
        id=job_id,
        status="queued",
        filename=filename,
        content_type=content_type,
        source_object_key=str((uploaded or {}).get("storage_key") or key or ""),
        source_bucket=str((uploaded or {}).get("storage_bucket") or getattr(store, "bucket", "")),
        source_region=str((uploaded or {}).get("storage_region") or getattr(store, "region", "")),
        user_id=user_id,
        merchant_user_id=merchant_user_id,
        request_metadata=request_metadata,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    _sync_async_extraction_job_mirror(services, job)

    return AsyncExtractionJobCreateResponse(
        jobId=job.id,
        status=str(job.status or "queued"),
        createdAt=job.created_at,
    )


@router.get("/extraction-jobs/{job_id}", response_model=AsyncExtractionJobStatusResponse)
def get_async_extraction_job(
    job_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "merchant", "consumer")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> AsyncExtractionJobStatusResponse:
    scoped_user_id, scoped_merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    job = db.get(ExtractionJob, job_id)
    if job is not None:
        if not _async_job_in_scope(job, user_id=scoped_user_id, merchant_user_id=scoped_merchant_user_id):
            raise HTTPException(status_code=403, detail="Forbidden")
        return _serialize_async_extraction_job(job)

    mirrored = _load_async_job_status_from_mirror(
        services,
        job_id=job_id,
        user_id=scoped_user_id,
        merchant_user_id=scoped_merchant_user_id,
    )
    if mirrored is not None:
        return mirrored
    raise HTTPException(status_code=404, detail="Extraction job not found")


@router.post("/extraction-jobs/{job_id}/callback")
def complete_async_extraction_job(
    job_id: UUID,
    payload: AsyncExtractionCallbackRequest,
    request: Request,
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
):
    _require_async_callback(request)
    job = db.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Extraction job not found")

    now = datetime.now(timezone.utc)
    normalized_status = payload.status.strip().lower()
    job.started_at = job.started_at or now
    if normalized_status in {"failed", "error"}:
        job.result_metadata = dict(payload.extracted_metadata or {})
        job.result_text = (payload.extracted_text or None)
        _mark_async_job_failed(
            db,
            job,
            error_message=str(payload.error_message or "Async extraction failed"),
            engines_used=[str(engine) for engine in payload.engines_used],
            services=services,
        )
        return {"status": "acknowledged", "jobId": str(job.id)}

    extracted_metadata = dict(payload.extracted_metadata or {})
    extracted_text = str(payload.extracted_text or "").strip() or _metadata_to_canonical_text(extracted_metadata)
    document = _finalize_async_extraction_job(
        db=db,
        services=services,
        job=job,
        extracted_text=extracted_text,
        extracted_metadata=extracted_metadata,
        field_confidences=dict(payload.field_confidences or {}),
        field_sources={str(key): str(value) for key, value in dict(payload.field_sources or {}).items()},
        low_confidence_fields=[str(field) for field in list(payload.low_confidence_fields or [])],
        engines_used=[str(engine) for engine in payload.engines_used],
    )
    return {"status": "acknowledged", "jobId": str(job.id), "documentId": str(document.id)}


@router.post("/ingest/image", response_model=IngestPDFResponse)
async def ingest_image(
    request: Request,
    file: UploadFile = File(...),
    bill_id: str | None = Form(default=None),
    vendor: str | None = Form(default=None),
    document_date: date | None = Form(default=None),
    total_amount: float | None = Form(default=None),
    ocr_text: str | None = Form(default=None),
    ocr_mode: str | None = Form(default=None),
    user_id: str | None = Form(default=None),
    consumer_custom_id: str | None = Form(default=None),
    consumer_name: str | None = Form(default=None),
    consumer_email: str | None = Form(default=None),
    merchant_user_id: str | None = Form(default=None),
    merchant_name: str | None = Form(default=None),
    merchant_custom_id: str | None = Form(default=None),
    version: int = Form(default=1),
    principal: Principal = Depends(require_roles("admin", "analyst", "merchant", "consumer")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> IngestPDFResponse:
    _rate_limit_or_429(
        request=request,
        principal=principal,
        bucket="ingest_image",
        limit=get_settings().api_rate_limit_ingest_per_window,
    )
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )

    if principal.role == "consumer":
        user_id = principal.subject
    if principal.role == "merchant":
        merchant_user_id = principal.subject
        if not user_id:
            raise HTTPException(status_code=400, detail="consumer user_id is required for merchant ingestion.")

    filename = file.filename or "uploaded-image"
    lowered = filename.lower()
    is_image = lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"))
    if file.content_type and file.content_type.lower().startswith("image/"):
        is_image = True
    if not is_image:
        raise HTTPException(status_code=400, detail="Upload an image document.")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded image is empty.")
    storage_references = _store_source_blob(
        services=services,
        payload=payload,
        filename=filename,
        source="ingest_image",
        principal=principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    if _looks_like_person_photo(payload):
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )
    image_classification = _classify_document_image_with_bedrock(payload, filename)
    image_doc_is_invoice = image_classification.get("is_invoice") if image_classification else None
    image_doc_confidence = _coerce_float(image_classification.get("confidence"), default=0.0) if image_classification else 0.0
    image_doc_type = str(image_classification.get("document_type") or "").strip().lower() if image_classification else ""
    image_allowed_type = image_doc_type in {"warranty_card", "guarantee_card"}
    if image_doc_is_invoice is False and not image_allowed_type and image_doc_confidence >= 0.7:
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )

    routed = _run_image_extraction_router(
        image_bytes=payload,
        filename=filename,
        supplied_ocr_text=(ocr_text or ""),
        ocr_mode_override=(ocr_mode or ""),
        bill_id=bill_id,
        vendor=vendor,
        document_date=document_date,
        total_amount=total_amount,
    )
    strict_metadata = routed.get("metadata")
    if not isinstance(strict_metadata, dict):
        strict_metadata = {}
    engines_used = [str(name) for name in routed.get("engines_used") or []]
    resolved_ocr_text = str(routed.get("resolved_text") or "").strip()
    if not resolved_ocr_text:
        resolved_ocr_text = _metadata_to_canonical_text(strict_metadata)
    if not resolved_ocr_text:
        diagnostics = _build_image_ocr_diagnostics(payload)
        engine_hint = f"engines={','.join(engines_used)}" if engines_used else "engines=none"
        raise HTTPException(
            status_code=422,
            detail=(
                "Unable to extract readable text from this image. "
                "Retry with a clearer bill image. "
                "If OCR still fails, add invoice fields manually. "
                f"Diagnostics: {engine_hint}; {diagnostics}"
            ),
        )

    has_invoice_signals = any(
        _is_meaningful_metadata_value(strict_metadata.get(key))
        for key in ("bill_id", "vendor", "total_amount", "date")
    )
    has_strong_invoice_engine = any(
        name in {"google_vision", "gemini_vision", "aws_bedrock_text", "aws_bedrock_vision", "aws_textract", "aws_textract_proxy", "manual_override"}
        for name in engines_used
    )

    if not (has_strong_invoice_engine and has_invoice_signals) and _metadata_looks_like_ui(strict_metadata):
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )

    if not (has_strong_invoice_engine and has_invoice_signals) and (_looks_like_safebill_ui(resolved_ocr_text) or _looks_like_ui_screenshot(resolved_ocr_text)):
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )

    classification = _classify_document_with_bedrock(resolved_ocr_text, filename)
    doc_is_invoice = classification.get("is_invoice") if classification else None
    doc_confidence = _coerce_float(classification.get("confidence"), default=0.0) if classification else 0.0
    doc_type = str(classification.get("document_type") or "").strip().lower() if classification else ""
    is_allowed_doc_type = doc_type in {"warranty_card", "guarantee_card"}
    if doc_is_invoice is False and not is_allowed_doc_type and doc_confidence >= 0.75:
        raise HTTPException(
            status_code=422,
            detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
        )
    if doc_is_invoice is None or doc_is_invoice is False:
        if not is_allowed_doc_type and not (has_strong_invoice_engine and has_invoice_signals):
            heuristic_is_invoice, heuristic_confidence = _heuristic_is_invoice_document(resolved_ocr_text)
            if not heuristic_is_invoice and heuristic_confidence >= 0.8:
                raise HTTPException(
                    status_code=422,
                    detail="Not a bill/invoice. Please upload a valid invoice or warranty card.",
                )
    if not has_invoice_signals:
        raise HTTPException(
            status_code=422,
            detail=(
                "Invoice fields were not extracted with confidence. "
                "Provide clearer bill image or enter invoice details manually."
            ),
        )

    additional_references: dict[str, object] = {}
    if storage_references:
        additional_references.update(storage_references)
    if consumer_custom_id:
        additional_references["consumer_custom_id"] = consumer_custom_id
    if consumer_name:
        additional_references["consumer_name"] = consumer_name
    if consumer_email:
        additional_references["consumer_email"] = consumer_email
    if merchant_user_id:
        additional_references["merchant_user_id"] = merchant_user_id
    if merchant_name:
        additional_references["merchant_name"] = merchant_name
    if merchant_custom_id:
        additional_references["merchant_custom_id"] = merchant_custom_id
    if principal.role == "merchant" and principal.email:
        additional_references["merchant_email"] = principal.email
    if merchant_user_id and user_id:
        additional_references["assignment_source"] = "merchant_upload"
    additional_references["metadata_source"] = (
        ",".join(engines_used) if engines_used else "ocr_router"
    )

    document, chunk_count = _persist_structured_document(
        db=db,
        services=services,
        filename=filename,
        source="image_ocr_router",
        user_id=user_id,
        extracted_text=resolved_ocr_text,
        extracted_metadata=strict_metadata,
        bill_id=bill_id,
        vendor=vendor,
        document_date=document_date,
        total_amount=total_amount,
        version=version,
        field_confidences=(
            routed.get("field_confidences")
            if isinstance(routed.get("field_confidences"), dict)
            else None
        ),
        field_sources=(
            routed.get("field_sources")
            if isinstance(routed.get("field_sources"), dict)
            else None
        ),
        low_confidence_fields=(
            routed.get("low_confidence_fields")
            if isinstance(routed.get("low_confidence_fields"), list)
            else None
        ),
        extraction_engines=engines_used or None,
        additional_references=additional_references,
    )
    _schedule_document_notifications(
        db,
        document,
        consumer_user_id=user_id,
        consumer_email=consumer_email,
        consumer_name=consumer_name,
    )
    _log_security_event(
        db,
        event_type="document.ingest_image",
        principal=principal,
        resource=f"documents/{document.id}",
        request=request,
        metadata={
            "bill_id": document.bill_id,
            "engines_used": engines_used,
            "user_id": user_id,
            "merchant_user_id": merchant_user_id,
        },
    )
    return IngestPDFResponse(
        document_id=document.id,
        chunk_count=chunk_count,
        bill_id=document.bill_id,
        vendor=document.vendor,
        created_at=document.created_at,
    )


@router.post("/ingest/vendor-table", response_model=IngestVendorTableResponse)
async def ingest_vendor_table(
    file: UploadFile = File(...),
    version: int = Form(default=1),
    principal: Principal = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> IngestVendorTableResponse:
    _ = principal
    if not file.filename or not file.filename.lower().endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Upload CSV/XLSX/XLS vendor table.")
    payload = await file.read()
    storage_references = _store_source_blob(
        services=services,
        payload=payload,
        filename=file.filename,
        source="ingest_vendor_table",
        principal=principal,
        user_id=None,
        merchant_user_id=None,
    )
    documents, row_count = services.ingestion.ingest_vendor_table(
        db=db,
        file_bytes=payload,
        filename=file.filename,
        version=version,
        source_references=storage_references,
    )
    created_at = documents[0].created_at if documents else None
    return IngestVendorTableResponse(
        document_ids=[doc.id for doc in documents],
        row_count=row_count,
        created_at=created_at,  # type: ignore[arg-type]
    )


@router.post("/merchant/manual-bill", response_model=MerchantIssueBillResponse)
def create_merchant_manual_bill(
    request: MerchantManualBillRequest,
    http_request: Request,
    principal: Principal = Depends(require_roles("admin", "analyst", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> MerchantIssueBillResponse:
    _rate_limit_or_429(
        request=http_request,
        principal=principal,
        bucket="merchant_manual_bill",
        limit=get_settings().api_rate_limit_ingest_per_window,
    )
    if principal.role == "merchant":
        if request.merchant_user_id != principal.subject:
            raise HTTPException(status_code=403, detail="Merchant scope mismatch.")
    resolved_vendor = (request.vendor or request.merchant_name or "UNKNOWN_VENDOR").strip()[:256]
    fallback_bill_id = f"MB-{int(time.time() * 1000)}"
    resolved_bill_id = (request.bill_id or fallback_bill_id).strip()[:128]
    warranty_start = request.purchase_date
    warranty_end = add_months(warranty_start, request.warranty_months) if warranty_start else None
    extracted_text = _manual_bill_text_payload(
        request=request,
        resolved_bill_id=resolved_bill_id,
        resolved_vendor=resolved_vendor,
    )
    manual_payload = extracted_text.encode("utf-8", errors="ignore")
    storage_references = _store_source_blob(
        services=services,
        payload=manual_payload,
        filename=f"{resolved_bill_id}.txt",
        source="merchant_manual_bill",
        principal=principal,
        user_id=request.consumer_user_id,
        merchant_user_id=request.merchant_user_id,
    )

    references: dict[str, object] = {
        "title": request.product_name,
        "product_name": request.product_name,
        "brand": resolved_vendor,
        "category": request.category or "Others",
        "warranty_months": request.warranty_months,
        "merchant_user_id": request.merchant_user_id,
        "merchant_name": request.merchant_name or resolved_vendor,
        "assignment_source": "merchant_manual",
    }
    if storage_references:
        references.update(storage_references)
    if principal.role == "merchant" and principal.email:
        references["merchant_email"] = principal.email
    if request.merchant_custom_id:
        references["merchant_custom_id"] = request.merchant_custom_id
    if request.consumer_custom_id:
        references["consumer_custom_id"] = request.consumer_custom_id
    if request.consumer_name:
        references["consumer_name"] = request.consumer_name
    if request.consumer_email:
        references["consumer_email"] = request.consumer_email
    else:
        references.pop("consumer_email", None)
    if request.serial_number:
        references["serial_number"] = request.serial_number
    if request.notes:
        references["notes"] = request.notes
    if warranty_start:
        references["warranty_start"] = warranty_start.isoformat()
    if warranty_end:
        references["warranty_end"] = warranty_end.isoformat()

    manual_metadata = ensure_strict_extraction(
        {
            "bill_id": resolved_bill_id,
            "vendor": resolved_vendor,
            "date": (request.purchase_date.isoformat() if request.purchase_date else None),
            "total_amount": request.total_amount,
            "product_name": request.product_name,
            "brand": resolved_vendor,
            "serial_number": request.serial_number,
            "warranty_months": request.warranty_months,
            "warranty_start": (warranty_start.isoformat() if warranty_start else None),
            "warranty_end": (warranty_end.isoformat() if warranty_end else None),
            "category": request.category or "Others",
        }
    )
    manual_conf = compute_field_confidences(
        metadata=manual_metadata,
        engine="manual_override",
        text_quality=1.0,
    )

    document, chunk_count = _persist_structured_document(
        db=db,
        services=services,
        filename=f"{resolved_bill_id}.txt",
        source="merchant_manual",
        user_id=request.consumer_user_id,
        extracted_text=extracted_text,
        extracted_metadata=manual_metadata,
        bill_id=resolved_bill_id,
        vendor=resolved_vendor,
        document_date=request.purchase_date,
        total_amount=request.total_amount,
        field_confidences=manual_conf,
        field_sources={field: "manual_override" for field in manual_conf},
        low_confidence_fields=[],
        extraction_engines=["manual_override"],
        additional_references=references,
    )
    _schedule_document_notifications(
        db,
        document,
        consumer_user_id=request.consumer_user_id,
        consumer_email=request.consumer_email,
        consumer_name=request.consumer_name,
    )
    _log_security_event(
        db,
        event_type="document.manual_bill",
        principal=principal,
        resource=f"documents/{document.id}",
        request=http_request,
        metadata={
            "merchant_user_id": request.merchant_user_id,
            "consumer_user_id": request.consumer_user_id,
        },
    )
    return MerchantIssueBillResponse(
        document=_serialize_document(document),
        chunk_count=chunk_count,
    )


@router.post("/merchant/documents/{doc_id}/assign", response_model=DocumentView)
def assign_document_to_consumer(
    doc_id: UUID,
    request: MerchantAssignRequest,
    http_request: Request,
    principal: Principal = Depends(require_roles("admin", "analyst", "merchant")),
    db: Session = Depends(get_db),
) -> DocumentView:
    if principal.role == "merchant":
        if request.merchant_user_id != principal.subject:
            raise HTTPException(status_code=403, detail="Merchant scope mismatch.")
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    references = _safe_references(document).copy()
    references["user_id"] = request.consumer_user_id
    references["merchant_user_id"] = request.merchant_user_id
    references["assignment_source"] = "merchant_reassign"
    references["assignment_status"] = "assigned"
    references["assigned_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if request.consumer_custom_id:
        references["consumer_custom_id"] = request.consumer_custom_id
    if request.consumer_name:
        references["consumer_name"] = request.consumer_name
    if request.consumer_email:
        references["consumer_email"] = request.consumer_email
    else:
        references.pop("consumer_email", None)
    if request.merchant_name:
        references["merchant_name"] = request.merchant_name
    if request.merchant_custom_id:
        references["merchant_custom_id"] = request.merchant_custom_id
    if principal.role == "merchant" and principal.email:
        references["merchant_email"] = principal.email
    document.references = references

    audit_entry = MerchantAssignmentAudit(
        document_id=document.id,
        merchant_user_id=request.merchant_user_id,
        consumer_user_id=request.consumer_user_id,
        status="assigned",
        assignment_source="merchant_reassign",
        notes="Assigned from merchant dashboard",
    )

    db.add(document)
    db.add(audit_entry)
    db.commit()
    db.refresh(document)
    _cancel_document_notifications(db, document_id=doc_id)
    _schedule_document_notifications(
        db,
        document,
        consumer_user_id=request.consumer_user_id,
        consumer_email=request.consumer_email,
        consumer_name=request.consumer_name,
    )
    _log_security_event(
        db,
        event_type="document.assigned",
        principal=principal,
        resource=f"documents/{document.id}",
        request=http_request,
        metadata={
            "merchant_user_id": request.merchant_user_id,
            "consumer_user_id": request.consumer_user_id,
            "assignment_source": "merchant_reassign",
        },
    )
    return _serialize_document(document)


@router.get("/merchant/activity", response_model=MerchantActivityResponse)
def list_merchant_activity(
    merchant_user_id: str | None = None,
    limit: int = 100,
    principal: Principal = Depends(require_roles("admin", "analyst", "viewer", "merchant")),
    db: Session = Depends(get_db),
) -> MerchantActivityResponse:
    _, merchant_scope = _resolve_document_scope(
        principal,
        merchant_user_id=merchant_user_id,
    )
    if not merchant_scope:
        raise HTTPException(status_code=400, detail="merchant_user_id is required.")
    merchant_user_id = merchant_scope

    safe_limit = max(1, min(limit, 500))
    stmt = (
        select(Document)
        .where(Document.references["merchant_user_id"].as_string() == merchant_user_id)
        .order_by(desc(Document.created_at))
        .limit(safe_limit)
    )
    filtered = [
        document
        for document in list(db.execute(stmt).scalars())
        if _document_in_scope(document, user_id=None, merchant_user_id=merchant_user_id)
    ]

    activities: list[MerchantActivityItem] = []
    for document in filtered:
        references = _safe_references(document)
        view = _serialize_document(document)
        source = str(references.get("source") or "unknown")
        assignment_source = str(references.get("assignment_source") or "")
        activities.append(
            MerchantActivityItem(
                activityId=f"{document.id}:{int(document.created_at.timestamp())}",
                merchantUserId=merchant_user_id,
                consumerUserId=(str(references["user_id"]) if references.get("user_id") else None),
                consumerCustomId=(
                    str(references["consumer_custom_id"]) if references.get("consumer_custom_id") else None
                ),
                consumerName=(str(references["consumer_name"]) if references.get("consumer_name") else None),
                documentId=str(document.id),
                billId=document.bill_id,
                title=view.title,
                vendor=document.vendor,
                amount=(float(document.total_amount) if document.total_amount is not None else None),
                category=view.category,
                source=source,
                action=_merchant_activity_action(source, assignment_source),
                createdAt=document.created_at.isoformat(),
            )
        )
    return MerchantActivityResponse(activities=activities)


@router.post("/search", response_model=SearchResponse)
def search(
    request: SearchRequest,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> SearchResponse:
    safe_query = enforce_safe_query(request.query)
    scoped_filters = request.filters.model_copy(deep=True)
    _scoped_metadata_filter(principal, scoped_filters)
    hits = services.retrieval_agent.retrieve(db=db, query=safe_query, filters=scoped_filters, top_k=request.top_k)
    return SearchResponse(
        results=[
            SearchResult(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                bill_id=hit.bill_id,
                vendor=hit.vendor,
                date=hit.date,
                total_amount=hit.total_amount,
                chunk_type=hit.chunk_type,
                content=hit.content,
                summary=hit.summary,
                score=hit.score,
                vector_score=hit.vector_score,
                keyword_score=hit.keyword_score,
                metadata=hit.metadata,
            )
            for hit in hits
        ]
    )


@router.post("/ask", response_model=AskResponse)
def ask(
    request: AskRequest,
    http_request: Request,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> AskResponse:
    _rate_limit_or_429(
        request=http_request,
        principal=principal,
        bucket="ask",
        limit=get_settings().api_rate_limit_ask_per_window,
    )
    start = time.perf_counter()
    safe_query = enforce_safe_query(request.query)
    is_service_center_query = services.service_center_locator.is_service_center_query(safe_query)
    scoped_filters = request.filters.model_copy(deep=True)
    _scoped_metadata_filter(principal, scoped_filters)

    plan = services.planner.plan(safe_query)
    hits = services.retrieval_agent.retrieve(db=db, query=safe_query, filters=scoped_filters, top_k=request.top_k)
    calculations = services.calculation_agent.execute(safe_query, hits)
    policy = services.policy_agent.evaluate(safe_query, hits, calculations)
    answer_payload = services.generator.generate(safe_query, plan, hits, calculations, policy)
    math_validation = services.calculation_agent.validate_answer_math(answer_payload, calculations)
    audit = services.auditor_agent.audit(answer_payload=answer_payload, hits=hits, math_validation=math_validation)

    service_center_company: str | None = None
    service_center_candidates: list[ServiceCenterCandidate] = []
    service_centers: list[ServiceCenterView] = []
    has_user_location = request.user_latitude is not None and request.user_longitude is not None
    service_center_radius_km: float | None = None
    service_center_location_hint: str | None = request.user_location_text
    if is_service_center_query:
        service_center_radius_km = services.service_center_locator.parse_radius_km(
            safe_query,
            default_km=request.service_radius_km,
        )
        if not service_center_location_hint:
            service_center_location_hint = _query_location_hint(safe_query)

        service_center_company = _resolve_company_name(
            safe_query,
            hits,
            filter_vendor=scoped_filters.vendor,
        )
        if service_center_company:
            service_center_candidates = services.service_center_locator.find_service_centers(
                company_name=service_center_company,
                user_latitude=request.user_latitude,
                user_longitude=request.user_longitude,
                location_hint=service_center_location_hint,
                radius_km=service_center_radius_km,
                limit=8,
            )
            service_centers = [
                ServiceCenterView(
                    name=center.name,
                    address=center.address,
                    latitude=center.latitude,
                    longitude=center.longitude,
                    distance_km=center.distance_km,
                    source=center.source,
                    confidence=center.confidence,
                    map_url=center.map_url,
                    city=center.city,
                    phone=center.phone,
                    website=center.website,
                    pincode=center.pincode,
                    pickup_available=center.pickup_available,
                    estimated_tat_days=center.estimated_tat_days,
                )
                for center in service_center_candidates
            ]

    citation_map = {str(hit.chunk_id): hit for hit in hits}
    citation_ids = [str(item) for item in answer_payload.get("citation_chunk_ids", [])]
    deduped_ids = []
    seen = set()
    for cid in citation_ids:
        if cid in seen:
            continue
        seen.add(cid)
        deduped_ids.append(cid)

    citations: list[Citation] = []
    for chunk_id in deduped_ids:
        hit = citation_map.get(chunk_id)
        if not hit:
            continue
        citations.append(
            Citation(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                bill_id=hit.bill_id,
                vendor=hit.vendor,
                score=hit.score,
                keyword_score=hit.keyword_score,
                excerpt=hit.content[:280],
            )
        )

    extraction_trace: list[ExtractionTraceStep] = []
    for citation in citations[:10]:
        hit = citation_map.get(str(citation.chunk_id))
        if hit is None:
            continue
        extraction_trace.append(
            ExtractionTraceStep(
                field="retrieval",
                value=f"{hit.bill_id}:{hit.chunk_type}",
                confidence=max(0.0, min(hit.score, 1.0)),
                source="lexical_retrieval",
                reason=(
                    f"Selected because keyword score={hit.keyword_score:.3f}."
                ),
                citations=[str(hit.chunk_id)],
            )
        )
        if hit.chunk_type == "invoice_metadata":
            try:
                payload = json.loads(hit.content or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                for key in ("bill_id", "vendor", "date", "total_amount", "warranty_end", "product_name"):
                    if not _is_meaningful_metadata_value(payload.get(key)):
                        continue
                    extraction_trace.append(
                        ExtractionTraceStep(
                            field=key,
                            value=payload.get(key),
                            confidence=max(0.0, min(hit.score, 1.0)),
                            source="invoice_metadata_chunk",
                            reason="Value present in structured invoice metadata chunk used by answer grounding.",
                            citations=[str(hit.chunk_id)],
                        )
                    )

    runtime_ms = int((time.perf_counter() - start) * 1000)
    qa_log = create_qa_log(
        db=db,
        query=safe_query,
        runtime_ms=runtime_ms,
        precision=audit.precision,
        recall=audit.recall,
        hallucination_flag=audit.hallucination_flag,
        confidence_score=audit.confidence_score,
        citations=[citation.model_dump(mode="json") for citation in citations],
        diagnostics={
            "planner_complexity": plan.complexity,
            "calculation_summary": calculations,
            "policy_summary": policy,
            "service_center_query": is_service_center_query,
            "service_center_company": service_center_company,
            "service_center_count": len(service_centers),
            "service_center_radius_km": service_center_radius_km,
            "service_center_location_hint": service_center_location_hint,
            "service_center_sources": [center.source for center in service_center_candidates],
            "extraction_trace_size": len(extraction_trace),
            **audit.diagnostics,
            "runtime_ms": runtime_ms,
        },
    )

    answer_text = str(answer_payload.get("answer", ""))
    if is_service_center_query:
        answer_text = _format_service_centers_block(
            answer_text,
            company_name=service_center_company,
            centers=service_center_candidates,
            has_user_location=has_user_location,
            radius_km=service_center_radius_km,
        )

    planner_output = PlannerOutput(
        complexity=plan.complexity,
        steps=[PlannerStep(name=step.name, action=step.action, completed=True) for step in plan.steps],
    )
    _log_security_event(
        db,
        event_type="rag.ask",
        principal=principal,
        resource="rag/ask",
        request=http_request,
        metadata={
            "query_length": len(safe_query),
            "citations": len(citations),
            "qa_log_id": str(qa_log.id),
        },
    )
    return AskResponse(
        answer=answer_text,
        confidence_score=audit.confidence_score,
        hallucination_flag=audit.hallucination_flag,
        planner=planner_output,
        citations=citations,
        extraction_trace=extraction_trace,
        service_centers=service_centers,
        qa_log_id=qa_log.id,
        qa_metrics={
            "precision": audit.precision,
            "recall": audit.recall,
            "runtime_ms": float(runtime_ms),
        },
    )


@router.get("/documents", response_model=DocumentsResponse)
def list_documents(
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    limit: int = 100,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> DocumentsResponse:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    safe_limit = max(1, min(limit, 500))
    stmt = select(Document).order_by(desc(Document.created_at))
    stmt = _apply_document_scope(stmt, user_id=user_id, merchant_user_id=merchant_user_id).limit(safe_limit)
    documents = [
        document
        for document in list(db.execute(stmt).scalars())
        if _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    ][:safe_limit]
    if documents:
        return DocumentsResponse(documents=[_serialize_document(document) for document in documents])
    mirrored = _list_document_views_from_mirror(
        services,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
        limit=safe_limit,
    )
    return DocumentsResponse(documents=mirrored)


@router.get("/documents/{doc_id}", response_model=DocumentView)
def get_document(
    doc_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> DocumentView:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if document and _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id):
        if principal.role == "consumer" and principal.subject:
            try:
                _mark_document_consumer_activated(db, document=document, consumer_user_id=principal.subject)
            except Exception:
                if hasattr(db, "rollback"):
                    try:
                        db.rollback()
                    except Exception:
                        pass
        return _serialize_document(document)

    mirrored = _load_document_view_from_mirror(
        services,
        doc_id=doc_id,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    if mirrored is not None:
        return mirrored
    raise HTTPException(status_code=404, detail="Document not found")


@router.get("/documents/{doc_id}/source-url")
def get_document_source_url(
    doc_id: UUID,
    expires_in: int | None = None,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> dict[str, object]:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")

    references = _safe_references(document)
    object_key = str(references.get("storage_key") or "").strip()
    if not object_key:
        raise HTTPException(status_code=404, detail="Source object not available for this document")

    store = getattr(services, "object_store", None)
    if store is None or not getattr(store, "enabled", False):
        raise HTTPException(status_code=503, detail="Object storage is not configured")

    ttl = int(expires_in or get_settings().s3_presign_ttl_seconds)
    ttl = max(60, min(ttl, 3600 * 24))
    signed_url = store.generate_download_url(key=object_key, expires_in_seconds=ttl)
    if not signed_url:
        raise HTTPException(status_code=500, detail="Could not generate source download URL")
    return {"docId": str(doc_id), "url": signed_url, "expiresInSeconds": ttl}


@router.post("/documents/{doc_id}/product-image/generate", response_model=DocumentProductImageView)
def generate_document_product_image(
    doc_id: UUID,
    payload: DocumentProductImageGenerateRequest = Body(default=DocumentProductImageGenerateRequest()),
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> DocumentProductImageView:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")

    current_state = _serialize_product_image_state(document)
    if current_state.productImageAvailable and not payload.force:
        return current_state

    generator = getattr(services, "product_images", None)
    if generator is None or not getattr(generator, "enabled", False):
        raise HTTPException(status_code=503, detail="Product image generation is not configured")

    try:
        generated_payload = generator.generate_for_document(
            document=document,
            object_store=getattr(services, "object_store", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("Product image generation failed for doc_id=%s", str(doc_id))
        raise HTTPException(status_code=502, detail=str(exc) or "Product image generation failed")

    references = _safe_references(document).copy()
    references["product_image"] = generated_payload
    document.references = references
    db.add(document)
    db.commit()
    db.refresh(document)
    _sync_document_mirror(services, document)
    return _serialize_product_image_state(document)


@router.get("/documents/{doc_id}/product-image-url", response_model=DocumentProductImageUrlResponse)
def get_document_product_image_url(
    doc_id: UUID,
    expires_in: int | None = None,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> DocumentProductImageUrlResponse:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")

    references = _safe_references(document)
    product_image = references.get("product_image") if isinstance(references.get("product_image"), dict) else {}
    object_key = str(product_image.get("storage_key") or "").strip()
    if not object_key:
        raise HTTPException(status_code=404, detail="Product image not available for this document")

    store = getattr(services, "object_store", None)
    if store is None or not getattr(store, "enabled", False):
        raise HTTPException(status_code=503, detail="Object storage is not configured")

    ttl = int(expires_in or get_settings().s3_presign_ttl_seconds)
    ttl = max(60, min(ttl, 3600 * 24))
    signed_url = store.generate_download_url(key=object_key, expires_in_seconds=ttl)
    if not signed_url:
        raise HTTPException(status_code=500, detail="Could not generate product image URL")
    return DocumentProductImageUrlResponse(docId=str(doc_id), url=signed_url, expiresInSeconds=ttl)


@router.get("/documents/{doc_id}/calendar-links", response_model=CalendarLinkResponse)
def get_document_calendar_links(
    doc_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> CalendarLinkResponse:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document or not _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id):
        raise HTTPException(status_code=404, detail="Document not found")
    view = _serialize_document(document)
    if not view.items or not view.items[0].warrantyEnd:
        raise HTTPException(status_code=422, detail="Warranty end date is missing for calendar reminder.")

    warranty_end = _coerce_date(view.items[0].warrantyEnd)
    if warranty_end is None:
        raise HTTPException(status_code=422, detail="Warranty end date is invalid for calendar reminder.")
    start_at = datetime.combine(warranty_end, datetime.min.time(), tzinfo=timezone.utc).replace(hour=9)
    end_at = start_at + timedelta(hours=1)
    title = f"Warranty deadline: {view.title}"
    description = (
        f"Invoice: {view.items[0].invoiceNo or document.bill_id}\n"
        f"Vendor: {view.sellerName or document.vendor}\n"
        f"Warranty end: {warranty_end.isoformat()}"
    )
    ics_url = f"/api/documents/{view.docId}/calendar.ics"
    google_url = (
        "https://calendar.google.com/calendar/render?"
        + urlencode(
            {
                "action": "TEMPLATE",
                "text": title,
                "details": description,
                "dates": f"{start_at.strftime('%Y%m%dT%H%M%SZ')}/{end_at.strftime('%Y%m%dT%H%M%SZ')}",
            }
        )
    )
    return CalendarLinkResponse(
        docId=view.docId,
        googleCalendarUrl=google_url,
        icsDownloadUrl=ics_url,
    )


@router.get("/documents/{doc_id}/calendar.ics")
def download_document_calendar_ics(
    doc_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> Response:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document or not _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id):
        raise HTTPException(status_code=404, detail="Document not found")
    view = _serialize_document(document)
    if not view.items or not view.items[0].warrantyEnd:
        raise HTTPException(status_code=422, detail="Warranty end date is missing for calendar reminder.")
    warranty_end = _coerce_date(view.items[0].warrantyEnd)
    if warranty_end is None:
        raise HTTPException(status_code=422, detail="Warranty end date is invalid for calendar reminder.")

    start_at = datetime.combine(warranty_end, datetime.min.time(), tzinfo=timezone.utc).replace(hour=9)
    end_at = start_at + timedelta(hours=1)
    title = f"Warranty deadline: {view.title}"
    description = (
        f"Invoice: {view.items[0].invoiceNo or document.bill_id}\n"
        f"Vendor: {view.sellerName or document.vendor}\n"
        f"Warranty end: {warranty_end.isoformat()}"
    )
    ics = _build_warranty_ics(
        uid=f"{view.docId}@safebill",
        title=title,
        description=description,
        start_at=start_at,
        end_at=end_at,
    )
    headers = {
        "Content-Disposition": f'attachment; filename="warranty-{view.docId}.ics"',
    }
    return Response(content=ics, media_type="text/calendar; charset=utf-8", headers=headers)


@router.get("/documents/{doc_id}/claim-packet", response_model=ClaimPacketResponse)
def generate_claim_packet(
    doc_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> ClaimPacketResponse:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document or not _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id):
        raise HTTPException(status_code=404, detail="Document not found")
    view = _serialize_document(document)
    packet = _build_claim_packet_payload(document, view)
    references = _safe_references(document).copy()
    references["claim_packet_generated_at"] = packet.generatedAt
    document.references = references
    db.add(document)
    db.commit()
    return packet


@router.get("/documents/{doc_id}/claim-assistant", response_model=ClaimAssistantResponse)
def get_claim_assistant(
    doc_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> ClaimAssistantResponse:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")
    view = _serialize_document(document)
    references = _safe_references(document)
    channel_candidates = ["in_app", "email"]
    if str(references.get("consumer_email") or "").strip():
        channel_candidates.append("email")
    if str(references.get("whatsapp_number") or "").strip():
        channel_candidates.append("whatsapp")
    if str(references.get("sms_number") or "").strip():
        channel_candidates.append("sms")
    recommended_channels = []
    for item in channel_candidates:
        if item not in recommended_channels:
            recommended_channels.append(item)

    return ClaimAssistantResponse(
        docId=view.docId,
        readiness=view.claimReadiness,
        deadlineBand=view.deadlineBand,
        nextBestActions=_claim_next_best_actions(view),
        recommendedChannels=recommended_channels,
        claimPacketUrl=f"/api/v1/documents/{view.docId}/claim-packet",
        calendarIcsUrl=f"/api/v1/documents/{view.docId}/calendar.ics",
        serviceCentersUrl=f"/api/v1/documents/{view.docId}/service-centers",
    )


@router.get("/documents/{doc_id}/service-centers", response_model=ServiceCentersRecommendationResponse)
def get_document_service_centers(
    doc_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    user_latitude: float | None = None,
    user_longitude: float | None = None,
    user_location_text: str | None = None,
    radius_km: float | None = None,
    limit: int = 5,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> ServiceCentersRecommendationResponse:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")
    view = _serialize_document(document)
    references = _safe_references(document)
    company_candidates = [
        _clean_company_token(view.sellerName),
        _clean_company_token(str(references.get("brand") or "")),
        _clean_company_token(str(view.items[0].model if view.items else "")),
        _clean_company_token(str(view.title or "")),
    ]
    company = None
    normalize_company_name = getattr(services.service_center_locator, "normalize_company_name", None)
    for candidate in company_candidates:
        normalized = (
            normalize_company_name(candidate)
            if callable(normalize_company_name)
            else _clean_company_token(candidate)
        )
        if normalized:
            company = normalized
            break
    if not company:
        return ServiceCentersRecommendationResponse(
            docId=view.docId,
            company=None,
            locationHint=user_location_text,
            radiusKm=float(radius_km or 30.0),
            count=0,
            guidance="Could not infer brand/vendor. Please update document brand or vendor details.",
            centers=[],
        )

    safe_limit = max(1, min(limit, 10))
    safe_radius = services.service_center_locator.parse_radius_km("", default_km=radius_km)
    allow_live_lookup = bool(getattr(services.service_center_locator, "live_lookup_enabled", False))
    candidates = services.service_center_locator.find_service_centers(
        company_name=company,
        user_latitude=user_latitude,
        user_longitude=user_longitude,
        location_hint=user_location_text,
        radius_km=safe_radius,
        limit=safe_limit,
        allow_external_lookup=allow_live_lookup,
    )
    guidance = _format_service_centers_block(
        "",
        company_name=company,
        centers=candidates,
        has_user_location=bool(user_latitude is not None and user_longitude is not None) or bool(user_location_text),
        radius_km=safe_radius,
    )
    response_centers = [
        ServiceCenterView(
            name=center.name,
            address=center.address,
            latitude=center.latitude,
            longitude=center.longitude,
            distance_km=center.distance_km,
            source=center.source,
            confidence=center.confidence,
            map_url=center.map_url,
            city=center.city,
            phone=center.phone,
            website=center.website,
            pincode=center.pincode,
            pickup_available=center.pickup_available,
            estimated_tat_days=center.estimated_tat_days,
        )
        for center in candidates
    ]
    return ServiceCentersRecommendationResponse(
        docId=view.docId,
        company=company,
        locationHint=user_location_text,
        radiusKm=float(safe_radius),
        count=len(response_centers),
        guidance=guidance,
        centers=response_centers,
    )


@router.post("/documents/{doc_id}/share", response_model=DocumentShareResponse)
def share_document(
    doc_id: UUID,
    request: DocumentShareRequest,
    principal: Principal = Depends(require_roles("admin", "analyst", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> DocumentShareResponse:
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _can_manage_document_sharing(principal, document):
        raise HTTPException(status_code=403, detail="You do not have permission to share this document.")

    references = _safe_references(document).copy()
    owner_user_id = str(references.get("user_id") or "").strip()
    target_user_id = request.target_user_id.strip()
    if owner_user_id and target_user_id == owner_user_id:
        raise HTTPException(status_code=400, detail="Owner already has access.")

    members = _shared_members_from_references(references)
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    granted_by = str(principal.subject or principal.role or "system")
    updated_members: list[dict[str, str]] = []
    upserted = False
    for member in members:
        if member.get("user_id") == target_user_id:
            member["permission"] = request.permission
            member["granted_by"] = granted_by
            member["granted_at"] = now_iso
            upserted = True
        updated_members.append(member)
    if not upserted:
        updated_members.append(
            {
                "user_id": target_user_id,
                "permission": request.permission,
                "granted_by": granted_by,
                "granted_at": now_iso,
            }
        )
    references["shared_with"] = updated_members
    document.references = references
    db.add(document)
    db.commit()
    db.refresh(document)
    return _serialize_document_shares(document)


@router.delete("/documents/{doc_id}/share/{target_user_id}", response_model=DocumentShareResponse)
def unshare_document(
    doc_id: UUID,
    target_user_id: str,
    principal: Principal = Depends(require_roles("admin", "analyst", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> DocumentShareResponse:
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _can_manage_document_sharing(principal, document):
        raise HTTPException(status_code=403, detail="You do not have permission to unshare this document.")

    references = _safe_references(document).copy()
    members = _shared_members_from_references(references)
    filtered = [member for member in members if member.get("user_id") != target_user_id.strip()]
    references["shared_with"] = filtered
    document.references = references
    db.add(document)
    db.commit()
    db.refresh(document)
    return _serialize_document_shares(document)


@router.get("/vault/shared-with-me", response_model=SharedVaultResponse)
def list_shared_vault_documents(
    user_id: str | None = None,
    limit: int = 100,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> SharedVaultResponse:
    requested_user = _normalize_scope_value(user_id)
    if principal.role in {"admin", "analyst"}:
        target_user_id = requested_user
    else:
        target_user_id = str(principal.subject or "").strip()
        if requested_user and requested_user != target_user_id:
            raise HTTPException(status_code=403, detail="User scope mismatch.")
    if not target_user_id:
        raise HTTPException(status_code=400, detail="user_id is required.")

    safe_limit = max(1, min(limit, 500))
    stmt = select(Document).order_by(desc(Document.created_at)).limit(max(50, safe_limit * 5))
    rows = list(db.execute(stmt).scalars())
    shared_docs = [doc for doc in rows if _document_is_shared_with(doc, target_user_id)]
    serialized = [_serialize_document(doc) for doc in shared_docs[:safe_limit]]
    return SharedVaultResponse(documents=serialized)


@router.get("/documents/{doc_id}/claim-whatsapp-draft", response_model=WhatsAppClaimDraftResponse)
def get_claim_whatsapp_draft(
    doc_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> WhatsAppClaimDraftResponse:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")

    view = _serialize_document(document)
    packet = _build_claim_packet_payload(document, view)
    references = _safe_references(document)
    consumer_user_id = str(references.get("user_id") or principal.subject or "").strip()
    consumer_email = str(references.get("consumer_email") or "").strip() or None
    consumer_name = str(references.get("consumer_name") or "").strip() or None
    preference = _notification_service.get_preference(
        db,
        user_id=(consumer_user_id or "anonymous"),
        email_hint=consumer_email,
        full_name_hint=consumer_name,
    )
    whatsapp_enabled = bool(preference.whatsapp_enabled and preference.whatsapp_number)
    destination = preference.whatsapp_number if whatsapp_enabled else None
    issue_line = packet.issueSummaryTemplate.splitlines()[0] if packet.issueSummaryTemplate else "Issue Summary"
    message = (
        f"Hi {view.sellerName or 'Support Team'}, I want to raise a warranty claim.\n"
        f"Invoice: {packet.facts.get('invoice_number') or 'N/A'}\n"
        f"Product: {packet.facts.get('product_name') or view.title}\n"
        f"Warranty End: {packet.facts.get('warranty_end') or 'N/A'}\n"
        f"{issue_line}\n"
        f"Please share next steps for authorized service."
    )
    next_steps = _claim_next_best_actions(view)
    if not whatsapp_enabled:
        next_steps = [
            "Enable WhatsApp notifications in /api/v1/notifications/preferences.",
            "Set whatsapp_enabled=true and add whatsapp_number.",
        ] + next_steps

    references["claim_whatsapp_draft_generated_at"] = datetime.now(timezone.utc).isoformat()
    document.references = references
    db.add(document)
    db.commit()

    return WhatsAppClaimDraftResponse(
        docId=view.docId,
        whatsappEnabled=whatsapp_enabled,
        destination=destination,
        message=message,
        nextSteps=next_steps[:6],
    )


@router.get("/documents/{doc_id}/fraud-check", response_model=FraudCheckResponse)
def get_document_fraud_check(
    doc_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> FraudCheckResponse:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")

    view = _serialize_document(document)
    signals = _compute_fraud_signals(document, view)
    weighted = 0.0
    for signal in signals:
        if signal.severity == "high":
            weighted += 0.28
        elif signal.severity == "medium":
            weighted += 0.16
        else:
            weighted += 0.06
    risk_score = round(max(0.0, min(weighted, 1.0)), 3)
    status = _fraud_status_from_score(risk_score)
    recommended_actions = [
        "Re-verify invoice number, vendor, and date against original document.",
        "Cross-check GST/tax fields and duplicate indicators before approving claim.",
        "Request manual review if high-risk compliance alerts are present.",
    ]
    return FraudCheckResponse(
        docId=view.docId,
        riskScore=risk_score,
        status=status,
        signals=signals,
        recommendedActions=recommended_actions,
    )


@router.get("/documents/{doc_id}/renewal-options", response_model=RenewalOptionsResponse)
def get_document_renewal_options(
    doc_id: UUID,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    currency: str = "INR",
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> RenewalOptionsResponse:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")

    safe_currency = (currency or "INR").strip().upper()[:8] or "INR"
    view = _serialize_document(document)
    options = _renewal_options_for_document(view, currency=safe_currency)
    current_end = view.items[0].warrantyEnd if view.items else None
    notes = [
        "Premiums are estimates based on invoice value/category and may vary by provider.",
        "Choose plans with pickup support when service-center coverage is limited.",
    ]
    for option in options:
        option.quoteUrl = (
            f"/api/v1/marketplace/renewal/quote?doc_id={view.docId}"
            f"&plan_id={option.planId}&partner_code={option.partnerCode}&currency={safe_currency}"
        )
        option.purchaseUrl = "/api/v1/marketplace/renewal/purchase-intent"
        option.webhookRef = _renewal_webhook_ref(
            doc_id=view.docId,
            plan_id=option.planId,
            partner_code=option.partnerCode,
        )
    return RenewalOptionsResponse(
        docId=view.docId,
        productName=(view.items[0].productName if view.items else view.title),
        currentWarrantyEnd=current_end,
        options=options,
        notes=notes,
    )


@router.get("/marketplace/renewal/quote", response_model=RenewalQuoteResponse)
def get_marketplace_renewal_quote(
    doc_id: str,
    plan_id: str,
    partner_code: str = "sbx_guardian",
    currency: str = "INR",
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> RenewalQuoteResponse:
    try:
        document_uuid = UUID(doc_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid doc_id") from exc

    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, document_uuid)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")

    view = _serialize_document(document)
    options = _renewal_options_for_document(view, currency=(currency or "INR").upper()[:8] or "INR")
    selected = next((option for option in options if option.planId == plan_id), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="Renewal plan not found")

    base_premium = round(float(selected.estimatedPremium), 2)
    tax_amount = round(base_premium * 0.18, 2)
    total = round(base_premium + tax_amount, 2)
    valid_until = (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat()
    quote_ref = _renewal_webhook_ref(
        doc_id=view.docId,
        plan_id=plan_id,
        partner_code=partner_code,
    )
    return RenewalQuoteResponse(
        docId=view.docId,
        planId=plan_id,
        partnerCode=partner_code,
        currency=selected.currency,
        basePremium=base_premium,
        taxAmount=tax_amount,
        totalPremium=total,
        validUntil=valid_until,
        quoteRef=quote_ref,
    )


@router.post("/marketplace/renewal/purchase-intent", response_model=RenewalPurchaseIntentResponse)
def create_marketplace_purchase_intent(
    request: RenewalPurchaseRequest,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> RenewalPurchaseIntentResponse:
    try:
        document_uuid = UUID(request.doc_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid doc_id") from exc

    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=request.user_id,
    )
    document = db.get(Document, document_uuid)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    in_scope = _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    shared_access = _document_is_shared_with(document, principal.subject)
    if not in_scope and not shared_access:
        raise HTTPException(status_code=404, detail="Document not found")

    webhook_ref = _renewal_webhook_ref(
        doc_id=request.doc_id,
        plan_id=request.plan_id,
        partner_code=request.partner_code,
    )
    references = _safe_references(document).copy()
    references["renewal_purchase_intent"] = {
        "plan_id": request.plan_id,
        "partner_code": request.partner_code,
        "webhook_ref": webhook_ref,
        "status": "initiated",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    document.references = references
    db.add(document)
    db.commit()

    checkout_url = (
        f"/api/v1/marketplace/renewal/checkout"
        f"?doc_id={request.doc_id}&plan_id={request.plan_id}&partner_code={request.partner_code}"
    )
    if request.return_url:
        checkout_url = f"{checkout_url}&return_url={request.return_url}"
    return RenewalPurchaseIntentResponse(
        docId=request.doc_id,
        planId=request.plan_id,
        partnerCode=request.partner_code,
        checkoutUrl=checkout_url,
        webhookRef=webhook_ref,
        status="initiated",
    )


@router.post("/marketplace/renewal/provider-events")
def ingest_marketplace_provider_event(
    request: RenewalProviderWebhookRequest,
    principal: Principal = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    _ = principal
    safe_ref = request.webhook_ref.strip()
    rows = list(db.execute(select(Document)).scalars())
    matched: Document | None = None
    for document in rows:
        references = _safe_references(document)
        payload = references.get("renewal_purchase_intent")
        if not isinstance(payload, dict):
            continue
        if str(payload.get("webhook_ref") or "").strip() == safe_ref:
            matched = document
            break

    if matched is None:
        raise HTTPException(status_code=404, detail="webhook_ref not found")

    references = _safe_references(matched).copy()
    intent = references.get("renewal_purchase_intent")
    if not isinstance(intent, dict):
        intent = {}
    intent["status"] = request.status.strip().lower()
    intent["provider"] = request.provider
    intent["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    intent["provider_payload"] = request.payload
    references["renewal_purchase_intent"] = intent
    matched.references = references
    db.add(matched)
    db.commit()
    return {"status": "acknowledged", "webhook_ref": safe_ref}


@router.get("/extraction-reviews", response_model=ExtractionReviewQueueResponse)
def list_extraction_reviews(
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> ExtractionReviewQueueResponse:
    safe_limit = max(1, min(limit, 500))
    stmt = select(ExtractionReview).order_by(desc(ExtractionReview.created_at)).limit(safe_limit)
    if principal.role == "consumer":
        if principal.subject:
            stmt = stmt.where(ExtractionReview.user_id == principal.subject)
    elif principal.role == "merchant":
        merchant_scope = principal.subject
        if merchant_scope:
            stmt = (
                stmt.join(Document, ExtractionReview.document_id == Document.id)
                .where(Document.references["merchant_user_id"].as_string() == merchant_scope)
            )
    else:
        scoped_user = _normalize_scope_value(user_id)
        scoped_merchant = _normalize_scope_value(merchant_user_id)
        if scoped_user:
            stmt = stmt.where(ExtractionReview.user_id == scoped_user)
        if scoped_merchant:
            stmt = (
                stmt.join(Document, ExtractionReview.document_id == Document.id)
                .where(Document.references["merchant_user_id"].as_string() == scoped_merchant)
            )
    if status:
        stmt = stmt.where(ExtractionReview.status == status.strip().lower())
    rows = list(db.execute(stmt).scalars())
    return ExtractionReviewQueueResponse(reviews=[_serialize_extraction_review(review) for review in rows])


@router.get("/extraction-reviews/{review_id}", response_model=ExtractionReviewView)
def get_extraction_review(
    review_id: UUID,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> ExtractionReviewView:
    review = db.get(ExtractionReview, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Extraction review not found")
    if not _review_in_scope(review, principal=principal, db=db):
        raise HTTPException(status_code=403, detail="Forbidden")
    return _serialize_extraction_review(review)


@router.put("/extraction-reviews/{review_id}", response_model=ExtractionReviewView)
def confirm_extraction_review(
    review_id: UUID,
    payload: ExtractionReviewConfirmRequest,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> ExtractionReviewView:
    review = db.get(ExtractionReview, review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Extraction review not found")
    if not _review_in_scope(review, principal=principal, db=db):
        raise HTTPException(status_code=403, detail="Forbidden")

    confirmed_fields = payload.confirmed_fields if isinstance(payload.confirmed_fields, dict) else {}
    review.confirmed_fields = confirmed_fields
    review.status = payload.status
    review.review_notes = payload.review_notes
    review.reviewer_user_id = principal.subject
    review.reviewed_at = datetime.now(timezone.utc)
    db.add(review)

    document = db.get(Document, review.document_id)
    if document:
        references = _safe_references(document).copy()
        for field, value in confirmed_fields.items():
            references[field] = value
        references["extraction_review_status"] = payload.status
        references["extraction_review_required"] = payload.status != "confirmed"
        if payload.status == "confirmed":
            references["low_confidence_fields"] = []
        document.references = references
        db.add(document)

    db.commit()
    db.refresh(review)
    return _serialize_extraction_review(review)


@router.get("/merchant/assignment-audits", response_model=MerchantAssignmentAuditResponse)
def list_assignment_audits(
    merchant_user_id: str | None = None,
    consumer_user_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "merchant", "consumer")),
    db: Session = Depends(get_db),
) -> MerchantAssignmentAuditResponse:
    safe_limit = max(1, min(limit, 500))
    stmt = select(MerchantAssignmentAudit).order_by(desc(MerchantAssignmentAudit.created_at)).limit(safe_limit)
    if principal.role == "merchant" and principal.subject:
        stmt = stmt.where(MerchantAssignmentAudit.merchant_user_id == principal.subject)
    elif principal.role == "consumer" and principal.subject:
        stmt = stmt.where(MerchantAssignmentAudit.consumer_user_id == principal.subject)
    else:
        if merchant_user_id:
            stmt = stmt.where(MerchantAssignmentAudit.merchant_user_id == merchant_user_id)
        if consumer_user_id:
            stmt = stmt.where(MerchantAssignmentAudit.consumer_user_id == consumer_user_id)
    if status:
        stmt = stmt.where(MerchantAssignmentAudit.status == status.strip().lower())
    rows = list(db.execute(stmt).scalars())
    return MerchantAssignmentAuditResponse(assignments=[_serialize_assignment_audit(row) for row in rows])


@router.post("/documents/{doc_id}/assignment/ack", response_model=MerchantAssignmentAuditView)
def acknowledge_assignment(
    doc_id: UUID,
    payload: MerchantAssignmentAcceptRequest,
    principal: Principal = Depends(require_roles("admin", "analyst", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> MerchantAssignmentAuditView:
    document = db.get(Document, doc_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    references = _safe_references(document).copy()
    consumer_id = payload.consumer_user_id
    if principal.role in {"consumer", "merchant"} and principal.subject:
        consumer_id = principal.subject
    if str(references.get("user_id") or "") != consumer_id:
        raise HTTPException(status_code=403, detail="User scope mismatch.")

    row = db.execute(
        select(MerchantAssignmentAudit)
        .where(MerchantAssignmentAudit.document_id == doc_id)
        .where(MerchantAssignmentAudit.consumer_user_id == consumer_id)
        .order_by(desc(MerchantAssignmentAudit.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        row = MerchantAssignmentAudit(
            document_id=doc_id,
            merchant_user_id=str(references.get("merchant_user_id") or "unknown"),
            consumer_user_id=consumer_id,
            status="assigned",
            assignment_source=str(references.get("assignment_source") or "unknown"),
        )

    row.status = payload.status
    row.notes = payload.notes
    now = datetime.now(timezone.utc)
    if payload.status == "accepted":
        row.accepted_at = now
        references["consumer_activated_at"] = now.replace(microsecond=0).isoformat()
    if payload.status == "escalated":
        row.escalated_at = now
    references["assignment_status"] = payload.status
    document.references = references

    db.add(row)
    db.add(document)
    db.commit()
    db.refresh(row)
    return _serialize_assignment_audit(row)


@router.delete("/documents/{doc_id}")
def delete_document(
    doc_id: UUID,
    request: Request,
    user_id: str | None = None,
    merchant_user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "consumer", "merchant")),
    db: Session = Depends(get_db),
    services: ServiceRegistry = Depends(get_services),
) -> dict[str, str]:
    user_id, merchant_user_id = _resolve_document_scope(
        principal,
        user_id=user_id,
        merchant_user_id=merchant_user_id,
    )
    document = db.get(Document, doc_id)
    if document is None:
        mirrored = _load_document_view_from_mirror(
            services,
            doc_id=doc_id,
            user_id=user_id,
            merchant_user_id=merchant_user_id,
        )
        if mirrored is None:
            raise HTTPException(status_code=404, detail="Document not found")
        store = getattr(services, "dynamodb_store", None)
        if store is not None and getattr(store, "enabled", False):
            store.delete_document_record(str(doc_id))
        return {"status": "deleted", "docId": str(doc_id)}
    if not _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id):
        raise HTTPException(status_code=404, detail="Document not found")
    references = _safe_references(document)
    storage_key = str(references.get("storage_key") or "").strip()
    _cancel_document_notifications(db, document_id=doc_id)
    db.delete(document)
    db.commit()
    store = getattr(services, "dynamodb_store", None)
    if store is not None and getattr(store, "enabled", False):
        store.delete_document_record(str(doc_id))
    if storage_key:
        try:
            store = getattr(services, "object_store", None)
            if store is not None and getattr(store, "enabled", False):
                store.delete_object(key=storage_key)
        except Exception:
            logger.exception("Failed to delete S3 source object for document_id=%s", str(doc_id))
    _log_security_event(
        db,
        event_type="document.deleted",
        principal=principal,
        resource=f"documents/{doc_id}",
        request=request,
        metadata={"user_id": user_id, "merchant_user_id": merchant_user_id},
    )
    return {"status": "deleted", "docId": str(doc_id)}


@router.get("/reminders", response_model=RemindersResponse)
def list_reminders(
    user_id: str | None = None,
    days_ahead: int = 60,
    limit: int = 200,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> RemindersResponse:
    user_id, merchant_user_id = _resolve_document_scope(principal, user_id=user_id)
    safe_days = max(1, min(days_ahead, 3650))
    safe_limit = max(1, min(limit, 500))

    stmt = select(Document).order_by(desc(Document.created_at))
    stmt = _apply_document_scope(stmt, user_id=user_id, merchant_user_id=merchant_user_id).limit(safe_limit)
    filtered = [
        document
        for document in list(db.execute(stmt).scalars())
        if _document_in_scope(document, user_id=user_id, merchant_user_id=merchant_user_id)
    ]

    now = date.today()
    cutoff = now + timedelta(days=safe_days)
    reminders: list[ReminderView] = []
    for document in filtered:
        view = _serialize_document(document)
        if not view.items:
            continue
        warranty_end = _coerce_date(view.items[0].warrantyEnd)
        if warranty_end is None or warranty_end > cutoff:
            continue
        days_remaining = (warranty_end - now).days
        urgency_tone = "stable"
        if days_remaining <= 0:
            urgency_tone = "expired"
        elif days_remaining <= 7:
            urgency_tone = "critical"
        elif days_remaining <= 30:
            urgency_tone = "watch"
        reminders.append(
            ReminderView(
                reminderId=f"{view.docId}-expiry",
                docId=view.docId,
                title=f"{view.title} warranty expiry",
                triggerAt=f"{warranty_end.isoformat()}T09:00:00Z",
                triggerType="expiry",
                deliveryChannels=["push", "email"],
                status=("expired" if warranty_end < now else "scheduled"),
                daysRemaining=days_remaining,
                urgencyTone=urgency_tone,
                recommendedAction=_reminder_action_from_days(days_remaining),
            )
        )

    reminders.sort(key=lambda reminder: reminder.triggerAt)
    return RemindersResponse(reminders=reminders[:safe_limit])


@router.get("/notifications", response_model=NotificationsResponse)
def list_notifications(
    user_id: str | None = None,
    include_read: bool = False,
    limit: int = 100,
    offset: int = 0,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> NotificationsResponse:
    user_scope = _resolve_notification_user_scope(principal, user_id=user_id)
    items = _notification_service.list_in_app_notifications(
        db,
        user_id=user_scope,
        include_read=include_read,
        limit=limit,
        offset=offset,
    )
    return NotificationsResponse(notifications=[NotificationItem(**item) for item in items])


@router.get("/notifications/preferences", response_model=NotificationPreferenceView)
def get_notification_preferences(
    user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> NotificationPreferenceView:
    user_scope = _resolve_notification_user_scope(principal, user_id=user_id)
    email_hint, full_name_hint = _notification_preference_hints(
        principal,
        user_scope=user_scope,
    )
    preference = _notification_service.get_preference(
        db,
        user_id=user_scope,
        email_hint=email_hint,
        full_name_hint=full_name_hint,
    )
    return NotificationPreferenceView(**_notification_service.serialize_preference(preference))


@router.put("/notifications/preferences", response_model=NotificationPreferenceView)
def update_notification_preferences(
    request: NotificationPreferenceUpdateRequest,
    user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> NotificationPreferenceView:
    user_scope = _resolve_notification_user_scope(principal, user_id=user_id)
    email_hint, full_name_hint = _notification_preference_hints(
        principal,
        user_scope=user_scope,
    )
    preference = _notification_service.update_preference(
        db,
        user_id=user_scope,
        updates=request.model_dump(exclude_unset=True),
        email_hint=email_hint,
        full_name_hint=full_name_hint,
    )
    return NotificationPreferenceView(**_notification_service.serialize_preference(preference))


@router.post("/notifications/process-due", response_model=NotificationProcessResult)
def process_due_notifications(
    request: Request,
    limit: int | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
) -> NotificationProcessResult:
    _rate_limit_or_429(
        request=request,
        principal=principal,
        bucket="notifications_process",
        limit=get_settings().api_rate_limit_notification_per_window,
    )
    result = _notification_service.process_due_jobs(db, limit=limit)
    return NotificationProcessResult(**result)


@router.get("/notifications/analytics", response_model=NotificationAnalyticsResponse)
def notifications_analytics(
    user_id: str | None = None,
    days: int = 30,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> NotificationAnalyticsResponse:
    user_scope: str | None
    if principal.role in {"admin", "analyst"} and not _normalize_scope_value(user_id):
        user_scope = None
    else:
        user_scope = _resolve_notification_user_scope(principal, user_id=user_id)
    metrics = _notification_service.get_delivery_analytics(
        db,
        user_id=user_scope,
        window_days=days,
    )
    return NotificationAnalyticsResponse(**metrics)


@router.get("/notifications/deliverability", response_model=NotificationDeliverabilityDashboardResponse)
def notification_deliverability_dashboard(
    user_id: str | None = None,
    days: int = 30,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> NotificationDeliverabilityDashboardResponse:
    safe_days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=safe_days)

    if principal.role in {"admin", "analyst"} and not _normalize_scope_value(user_id):
        user_scope = None
    else:
        user_scope = _resolve_notification_user_scope(principal, user_id=user_id)

    stmt = (
        select(NotificationDelivery, NotificationJob)
        .join(NotificationJob, NotificationDelivery.job_id == NotificationJob.id)
        .where(NotificationDelivery.created_at >= since)
    )
    if user_scope:
        stmt = stmt.where(NotificationJob.user_id == user_scope)

    rows = list(db.execute(stmt).all())
    totals = {"attempts": 0, "sent": 0, "failed": 0, "dead_lettered": 0}
    channel_map: dict[str, dict[str, int]] = {}

    for delivery, job in rows:
        totals["attempts"] += 1
        channel = str(delivery.channel or job.channel or "unknown")
        channel_bucket = channel_map.setdefault(channel, {"attempts": 0, "sent": 0, "failed": 0, "dead_lettered": 0})
        channel_bucket["attempts"] += 1

        status = str(delivery.status or "").lower()
        if status == "sent":
            totals["sent"] += 1
            channel_bucket["sent"] += 1
        elif status in {"dead_letter", "deadletter", "dead-letter"}:
            totals["dead_lettered"] += 1
            channel_bucket["dead_lettered"] += 1
        else:
            totals["failed"] += 1
            channel_bucket["failed"] += 1

    channel_stats = []
    for channel, bucket in sorted(channel_map.items()):
        attempts = bucket["attempts"]
        success_rate = (bucket["sent"] / attempts) if attempts else 0.0
        channel_stats.append(
            {
                "channel": channel,
                "attempts": attempts,
                "sent": bucket["sent"],
                "failed": bucket["failed"],
                "deadLettered": bucket["dead_lettered"],
                "successRate": round(success_rate, 4),
            }
        )

    return NotificationDeliverabilityDashboardResponse(
        windowDays=safe_days,
        totals=totals,
        channelStats=channel_stats,
    )


@router.post("/notifications/provider-events")
def ingest_notification_provider_event(
    payload: NotificationProviderEventIngestRequest,
    principal: Principal = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    _ = principal
    settings = get_settings()
    now = datetime.now(timezone.utc)
    status = payload.status.strip().lower()

    job: NotificationJob | None = None
    if payload.job_id:
        job = db.get(NotificationJob, payload.job_id)

    delivery_match: NotificationDelivery | None = None
    if payload.provider_message_id:
        delivery_match = db.execute(
            select(NotificationDelivery)
            .where(NotificationDelivery.provider_message_id == payload.provider_message_id)
            .order_by(desc(NotificationDelivery.created_at))
            .limit(1)
        ).scalar_one_or_none()
        if delivery_match and job is None:
            job = db.get(NotificationJob, delivery_match.job_id)

    if job is None and payload.channel and payload.recipient:
        job = db.execute(
            select(NotificationJob)
            .where(NotificationJob.channel == payload.channel)
            .where(NotificationJob.recipient_email == payload.recipient)
            .order_by(desc(NotificationJob.created_at))
            .limit(1)
        ).scalar_one_or_none()

    resolved_channel = payload.channel or (job.channel if job else "email")
    attempt_number = 1
    if job is not None:
        attempt_number = max(1, int(job.retry_count or 0) + 1)

    normalized_delivery_status = "failed"
    if status in {"sent", "delivered", "success", "opened"}:
        normalized_delivery_status = "sent"
    elif status in {"dead_letter", "deadletter", "dead-letter"}:
        normalized_delivery_status = "dead_letter"

    provider_payload = {
        "provider": payload.provider,
        "event_type": payload.event_type,
        "status": status,
        **(payload.payload if isinstance(payload.payload, dict) else {}),
    }
    if job is not None:
        delivery_entry = NotificationDelivery(
            job_id=job.id,
            channel=str(resolved_channel),
            attempt_number=attempt_number,
            status=normalized_delivery_status,
            provider_message_id=payload.provider_message_id,
            provider_payload=provider_payload,
            error_message=payload.error_message,
            latency_ms=None,
        )
        db.add(delivery_entry)

        if normalized_delivery_status == "sent":
            job.status = "sent"
            job.sent_at = now
            job.last_error = None
        elif normalized_delivery_status == "dead_letter":
            job.status = "dead_letter"
            job.last_error = payload.error_message
        else:
            job.status = "failed"
            job.last_error = payload.error_message
            if (
                job.channel == "email"
                and settings.sms_notifications_enabled
                and job.fallback_channel is None
            ):
                job.fallback_channel = "sms"
        db.add(job)

    db.commit()
    return {"status": "acknowledged"}


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: UUID,
    user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    user_scope = _resolve_notification_user_scope(principal, user_id=user_id)
    updated = _notification_service.mark_in_app_notification_read(
        db,
        notification_id=notification_id,
        user_id=user_scope,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "read", "notificationId": str(notification_id)}


@router.delete("/notifications/{notification_id}")
def delete_notification(
    notification_id: UUID,
    user_id: str | None = None,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    user_scope = _resolve_notification_user_scope(principal, user_id=user_id)
    deleted = _notification_service.delete_in_app_notification(
        db,
        notification_id=notification_id,
        user_id=user_scope,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "deleted", "notificationId": str(notification_id)}


@router.post("/ai/bharat/enrich", response_model=BharatAIEnrichResponse)
def bharat_ai_enrich(
    payload: BharatAIEnrichRequest,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    services: ServiceRegistry = Depends(get_services),
) -> BharatAIEnrichResponse:
    _ = principal
    metadata = payload.metadata if isinstance(payload.metadata, dict) else {}
    result = services.bharat_ai.enrich_invoice_for_bharat(
        ocr_text=payload.ocr_text,
        metadata=metadata,
        target_language_code=payload.target_language_code,
        include_speech=payload.include_speech,
    )
    return BharatAIEnrichResponse(
        sourceLanguageCode=str(result.get("source_language_code") or "en"),
        targetLanguageCode=str(result.get("target_language_code") or payload.target_language_code),
        normalizedText=str(result.get("normalized_text") or ""),
        consumerSummary=str(result.get("consumer_summary") or ""),
        localizedSummary=str(result.get("localized_summary") or ""),
        gstFindings=[str(item) for item in (result.get("gst_findings") or [])],
        fraudSignals=[str(item) for item in (result.get("fraud_signals") or [])],
        claimSteps=[str(item) for item in (result.get("claim_steps") or [])],
        merchantNotes=[str(item) for item in (result.get("merchant_notes") or [])],
        paymentReferences=[str(item) for item in (result.get("payment_references") or [])],
        modelUsed=(str(result.get("model_used")) if result.get("model_used") else None),
        speechAudioBase64=(str(result.get("speech_audio_base64")) if result.get("speech_audio_base64") else None),
        speechContentType=(str(result.get("speech_content_type")) if result.get("speech_content_type") else None),
    )


@router.post("/ai/bharat/translate", response_model=BharatAITranslateResponse)
def bharat_ai_translate(
    payload: BharatAITranslateRequest,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    services: ServiceRegistry = Depends(get_services),
) -> BharatAITranslateResponse:
    _ = principal
    translated = services.bharat_ai.translate_text(
        payload.text,
        target_language_code=payload.target_language_code,
        source_language_code=payload.source_language_code,
    )
    source_language = payload.source_language_code
    if source_language == "auto":
        source_language = services.bharat_ai.detect_language(payload.text)
    return BharatAITranslateResponse(
        sourceLanguageCode=source_language,
        targetLanguageCode=payload.target_language_code,
        translatedText=translated,
    )


@router.post("/ai/bharat/translate-batch", response_model=BharatAITranslateBatchResponse)
def bharat_ai_translate_batch(
    payload: BharatAITranslateBatchRequest,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    services: ServiceRegistry = Depends(get_services),
) -> BharatAITranslateBatchResponse:
    _ = principal
    texts = [str(item or "") for item in payload.texts]
    translated = services.bharat_ai.translate_many(
        texts,
        target_language_code=payload.target_language_code,
        source_language_code=payload.source_language_code,
    )
    source_language = payload.source_language_code
    if source_language == "auto":
        joined = next((item for item in texts if item.strip()), "")
        source_language = services.bharat_ai.detect_language(joined) if joined else "en"
    return BharatAITranslateBatchResponse(
        sourceLanguageCode=source_language,
        targetLanguageCode=payload.target_language_code,
        translations=translated,
    )


@router.post("/ai/bharat/ask", response_model=BharatAIAskResponse)
def bharat_ai_ask(
    payload: BharatAIAskRequest,
    principal: Principal = Depends(require_roles("admin", "analyst", "auditor", "viewer", "consumer", "merchant")),
    services: ServiceRegistry = Depends(get_services),
) -> BharatAIAskResponse:
    _ = principal
    metadata = payload.metadata if isinstance(payload.metadata, dict) else {}
    result = services.bharat_ai.answer_invoice_question(
        question=payload.question,
        ocr_text=payload.ocr_text,
        metadata=metadata,
        target_language_code=payload.target_language_code,
    )
    return BharatAIAskResponse(
        sourceLanguageCode=str(result.get("source_language_code") or "en"),
        targetLanguageCode=str(result.get("target_language_code") or payload.target_language_code),
        normalizedQuestion=str(result.get("normalized_question") or payload.question),
        localizedQuestion=str(result.get("localized_question") or payload.question),
        answer=str(result.get("answer") or ""),
        supportPoints=[str(item) for item in (result.get("support_points") or [])],
        missingInformation=[str(item) for item in (result.get("missing_information") or [])],
        confidenceNote=str(result.get("confidence_note") or ""),
        modelUsed=(str(result.get("model_used")) if result.get("model_used") else None),
    )
