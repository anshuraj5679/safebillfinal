from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic import model_validator

try:
    from dateutil import parser as date_parser
except Exception:  # pragma: no cover - optional runtime dependency
    date_parser = None  # type: ignore[assignment]


TEXT_FIELDS = {
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
NUMERIC_FIELDS = {
    "total_amount",
    "taxable_amount",
    "gst_amount",
    "gst_rate",
    "cgst_amount",
    "sgst_amount",
    "igst_amount",
}
ALL_FIELDS = [
    "bill_id",
    "vendor",
    "date",
    "total_amount",
    "vendor_tax_id",
    "taxable_amount",
    "gst_amount",
    "gst_rate",
    "cgst_amount",
    "sgst_amount",
    "igst_amount",
    "product_name",
    "brand",
    "serial_number",
    "warranty_months",
    "warranty_start",
    "warranty_end",
    "category",
    "line_items",
]
REVIEW_FIELDS = {
    "bill_id",
    "vendor",
    "date",
    "total_amount",
    "product_name",
    "warranty_end",
}
ENGINE_WEIGHTS = {
    "manual_override": 1.0,
    "aws_bedrock_vision": 0.94,
    "gemini_vision": 0.93,
    "aws_bedrock_text": 0.96,
    "aws_textract": 0.9,
    "aws_textract_proxy": 0.89,
    "google_vision": 0.88,
    "tesseract_regex": 0.72,
}
GROUNDED_OCR_FIELDS = {
    "bill_id",
    "vendor",
    "date",
    "total_amount",
    "vendor_tax_id",
    "taxable_amount",
    "gst_amount",
    "gst_rate",
    "cgst_amount",
    "sgst_amount",
    "igst_amount",
    "product_name",
    "serial_number",
    "line_items",
}

_DATE_LIKE_RE = re.compile(
    r"(?i)"
    r"(\b\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}\b"
    r"|\b\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2}\b"
    r"|\b\d{1,2}[\/\-.][A-Za-z]{3,9}[\/\-.]\d{2,4}\b"
    r"|\b[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}\b"
    r"|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}\b)"
)


def _looks_like_dimension_amount(amount: float | None, context_text: str | None) -> bool:
    if amount is None:
        return False
    text = (context_text or "").strip()
    if not text:
        return False
    if amount <= 0:
        return False
    rounded = round(amount)
    if abs(amount - rounded) > 1e-6:
        return False
    size = int(rounded)
    if size < 10 or size > 200:
        return False
    return bool(re.search(rf"(?i)\b{size}\s*(?:-?\s*(?:inch|inches|in\.|\"|cm|mm))\b", text))


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_date_iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
        if date_parser is None or not _DATE_LIKE_RE.search(text):
            return None
        try:
            # India-first parsing aligns with GST invoice conventions (e.g. 30.03.2018).
            parsed = date_parser.parse(text, dayfirst=True, fuzzy=True).date()
            return parsed.isoformat()
        except (ValueError, TypeError, OverflowError):
            return None
    return None


def _normalize_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _normalize_category(value: object) -> str | None:
    text = (_normalize_text(value) or "").lower()
    if not text:
        return None
    if text in {"gadget", "gadgets", "electronic", "electronics"}:
        return "Gadgets"
    if text in {"appliance", "appliances", "home appliance", "home appliances"}:
        return "Appliances"
    if text in {"vehicle", "vehicles", "automotive"}:
        return "Vehicle"
    if text in {"other", "others"}:
        return "Others"
    return None


def _looks_like_non_merchandise_line_item(name: str | None) -> bool:
    text = (name or "").strip().lower()
    if not text:
        return False
    compact = re.sub(r"\s+", " ", text)
    if _looks_like_domain_text(compact):
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

    if re.fullmatch(r"(?:customer|invoice|document|order|po|gst|pan|hsn|item)\s*(?:no|number|id|code)", compact):
        return True
    return False


def _looks_like_domain_text(value: str | None) -> bool:
    text = (value or "").strip().lower()
    if not text or " " in text:
        return False
    if len(text) > 64:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,62}\.[a-z]{2,6}", text):
        return True
    return False


def _looks_like_address_text(value: str | None) -> bool:
    text = (value or "").strip().lower()
    if len(text) < 12:
        return False

    location_tokens = (
        "plot no",
        "road",
        "rd",
        "street",
        "lane",
        "nagar",
        "estate",
        "industrial",
        "indl",
        "building",
        "tower",
        "floor",
        "mumbai",
        "maharashtra",
        "bengaluru",
        "bangalore",
        "karnataka",
        "kerala",
        "india",
    )
    has_location_signal = any(token in text for token in location_tokens)
    has_pincode = re.search(r"\b[1-9][0-9]{5}\b", text) is not None
    if has_location_signal and (has_pincode or text.count(",") >= 2):
        return True
    if text.count(",") >= 3 and has_location_signal:
        return True
    return False


def _is_inventory_code_token(token: str) -> bool:
    cleaned = token.strip().strip("()[]{}.,;:")
    if not cleaned:
        return False
    if re.fullmatch(r"\d{4,}", cleaned):
        return True
    if "/" in cleaned and any(ch.isdigit() for ch in cleaned):
        return True
    if re.fullmatch(r"[A-Z]{1,4}\d{3,}[A-Z0-9\-]*", cleaned):
        return True
    if re.fullmatch(r"[A-Z0-9\-]{6,}", cleaned) and any(ch.isdigit() for ch in cleaned):
        # Keep compact capacity tokens like 128GB.
        if re.fullmatch(r"\d{2,4}[A-Z]{1,4}", cleaned):
            return False
        return True
    return False


def sanitize_merchandise_name(value: object) -> str | None:
    text = _normalize_text(value)
    if not text:
        return None

    cleaned = re.sub(r"\s+", " ", text).strip(":- ")
    cleaned = re.sub(
        r"(?i)^(?:item|product|description|material|model)\s*(?:no|number|name|code)?\s*[:\-]\s*",
        "",
        cleaned,
    ).strip(":- ")

    # Drop trailing tabular numeric columns (qty/rate/amount/tax).
    cleaned = re.sub(
        r"\s+[0-9][0-9,]*(?:\.\d{1,2})?(?:\s+[0-9][0-9,]*(?:\.\d{1,2})?){1,6}\s*$",
        "",
        cleaned,
    ).strip(":- ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(":- ")
    if not cleaned:
        return None

    tokens = cleaned.split()
    removed = 0
    while len(tokens) >= 3 and removed < 2 and re.fullmatch(r"\d{1,2}", tokens[0]):
        tokens.pop(0)
        removed += 1
    while len(tokens) >= 3 and removed < 2 and _is_inventory_code_token(tokens[0]):
        tokens.pop(0)
        removed += 1
    while len(tokens) >= 4 and re.fullmatch(r"\d{1,3}", tokens[-2]) and tokens[-1].upper() in {"NOS", "PCS", "PC", "UNIT", "UNITS"}:
        tokens = tokens[:-2]
    while len(tokens) >= 3 and tokens[-1].upper() in {"NOS", "PCS", "PC", "UNIT", "UNITS"}:
        tokens.pop()
    while len(tokens) >= 3 and _is_inventory_code_token(tokens[-1]):
        tokens.pop()
    while len(tokens) >= 3 and re.fullmatch(r"\d{5,10}", tokens[-1]):
        tokens.pop()
    if len(tokens) >= 3 and tokens[-1].upper() in {"CN", "IN", "US", "EU", "UK"}:
        tokens.pop()

    normalized = " ".join(tokens).strip(":- ")
    if not normalized:
        return None
    if _looks_like_domain_text(normalized):
        return None
    if re.fullmatch(r"\d+\s*(?:NOS|PCS|PC|UNIT|UNITS)", normalized.upper()):
        return None
    if len(tokens) <= 2 and any(_is_inventory_code_token(token) for token in tokens):
        return None
    if _looks_like_non_merchandise_line_item(normalized):
        return None
    if _looks_like_address_text(normalized):
        return None
    if _safe_date_iso(normalized) is not None and len(normalized) <= 24:
        return None
    if len(re.findall(r"[A-Za-z]", normalized)) < 2:
        return None
    return normalized[:255]


class StrictLineItem(BaseModel):
    name: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None
    gst_amount: float | None = None

    @field_validator("quantity", "unit_price", "amount", "gst_amount", mode="before")
    @classmethod
    def _coerce_numeric(cls, value: object) -> float | None:
        return _safe_float(value)

    @field_validator("name", mode="before")
    @classmethod
    def _coerce_name(cls, value: object) -> str | None:
        cleaned = sanitize_merchandise_name(value)
        if cleaned:
            return cleaned
        text = _normalize_text(value)
        return text[:255] if text else None


class StrictInvoiceExtraction(BaseModel):
    bill_id: str | None = None
    vendor: str | None = None
    date: str | None = None
    total_amount: float | None = None
    vendor_tax_id: str | None = None
    taxable_amount: float | None = None
    gst_amount: float | None = None
    gst_rate: float | None = None
    cgst_amount: float | None = None
    sgst_amount: float | None = None
    igst_amount: float | None = None
    product_name: str | None = None
    brand: str | None = None
    serial_number: str | None = None
    warranty_months: int | None = None
    warranty_start: str | None = None
    warranty_end: str | None = None
    category: str | None = None
    line_items: list[StrictLineItem] = Field(default_factory=list)

    @field_validator(
        "bill_id",
        "vendor",
        "vendor_tax_id",
        "product_name",
        "brand",
        "serial_number",
        mode="before",
    )
    @classmethod
    def _normalize_strings(cls, value: object) -> str | None:
        text = _normalize_text(value)
        return text[:255] if text else None

    @field_validator("date", "warranty_start", "warranty_end", mode="before")
    @classmethod
    def _normalize_dates(cls, value: object) -> str | None:
        return _safe_date_iso(value)

    @field_validator(
        "total_amount",
        "taxable_amount",
        "gst_amount",
        "gst_rate",
        "cgst_amount",
        "sgst_amount",
        "igst_amount",
        mode="before",
    )
    @classmethod
    def _normalize_amounts(cls, value: object) -> float | None:
        return _safe_float(value)

    @field_validator("warranty_months", mode="before")
    @classmethod
    def _normalize_warranty_months(cls, value: object) -> int | None:
        months = _safe_int(value)
        if months is None:
            return None
        if months <= 0:
            return None
        return min(months, 240)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: object) -> str | None:
        normalized = _normalize_category(value)
        return normalized or (_normalize_text(value) if value else None)

    @model_validator(mode="after")
    def _sanitize_dimension_amounts(self) -> "StrictInvoiceExtraction":
        # Prevent common false-positive totals like `42` coming from a product dimension
        # (e.g. "42-inch TV") when the doc is a warranty card / certificate.
        if self.total_amount is not None:
            context_parts: list[str] = []
            if self.product_name:
                context_parts.append(self.product_name)
            if self.brand:
                context_parts.append(self.brand)
            for item in (self.line_items or [])[:10]:
                if item and item.name:
                    context_parts.append(item.name)
            context = " ".join(context_parts)
            if _looks_like_dimension_amount(self.total_amount, context):
                self.total_amount = None

        sanitized_items: list[StrictLineItem] = []
        for item in self.line_items:
            item.name = sanitize_merchandise_name(item.name)
            name = (item.name or "").strip()
            if _looks_like_non_merchandise_line_item(name):
                continue
            amount = _safe_float(item.amount)
            if amount is not None and (amount <= 0 or amount > 10_000_000):
                item.amount = None
                amount = None
            if not name:
                continue
            sanitized_items.append(item)
        self.line_items = sanitized_items[:50]

        self.product_name = sanitize_merchandise_name(self.product_name)
        if not self.product_name:
            for item in self.line_items:
                candidate = sanitize_merchandise_name(item.name)
                if candidate:
                    self.product_name = candidate
                    break

        taxable_amount = _safe_float(self.taxable_amount)
        gst_amount = _safe_float(self.gst_amount)
        if gst_amount is None:
            split_gst = sum(
                part
                for part in (
                    _safe_float(self.cgst_amount),
                    _safe_float(self.sgst_amount),
                    _safe_float(self.igst_amount),
                )
                if part is not None and part > 0
            )
            if split_gst > 0:
                gst_amount = round(split_gst, 2)
                self.gst_amount = gst_amount

        computed_total: float | None = None
        if taxable_amount is not None and taxable_amount > 0 and gst_amount is not None and gst_amount >= 0:
            computed_total = round(taxable_amount + gst_amount, 2)
        elif gst_amount is not None and gst_amount > 0 and self.line_items:
            line_total = sum(
                amount
                for amount in (_safe_float(item.amount) for item in self.line_items)
                if amount is not None and amount > 0
            )
            if line_total > 0:
                computed_total = round(line_total + gst_amount, 2)

        if computed_total is not None:
            total_amount = _safe_float(self.total_amount)
            if total_amount is None:
                self.total_amount = computed_total
            elif taxable_amount is not None and abs(total_amount - taxable_amount) <= max(1.0, taxable_amount * 0.02):
                # Replace taxable-value captures with invoice total when GST is available.
                self.total_amount = computed_total
            elif computed_total > 0 and (
                total_amount > computed_total * 2.5 or total_amount < computed_total * 0.4
            ):
                # Replace obvious outliers like pincodes or partial amounts when tax math is stronger.
                self.total_amount = computed_total
        return self


def ensure_strict_extraction(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload or {}
    try:
        parsed = StrictInvoiceExtraction(**raw)
    except ValidationError as exc:
        # Preserve schema shape even for invalid inputs.
        parsed = StrictInvoiceExtraction()
        parsed_line = {"validation_error": str(exc)}
        normalized = parsed.model_dump(mode="json")
        normalized["_schema_error"] = parsed_line
        return normalized
    return parsed.model_dump(mode="json")


def _is_meaningful(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _normalized_vote_key(field: str, value: object) -> str:
    if field in NUMERIC_FIELDS:
        numeric = _safe_float(value)
        return f"{numeric:.2f}" if numeric is not None else ""
    if field in {"warranty_months"}:
        val = _safe_int(value)
        return str(val) if val is not None else ""
    text = _normalize_text(value)
    return text.lower() if text else ""


def estimate_text_quality(ocr_text: str) -> float:
    text = (ocr_text or "").strip()
    if not text:
        return 0.0
    score = 0.25
    if len(text) >= 120:
        score += 0.2
    if len(text) >= 300:
        score += 0.15
    lowered = text.lower()
    invoice_tokens = ("invoice", "bill", "total", "amount", "date", "vendor")
    hits = sum(1 for token in invoice_tokens if token in lowered)
    score += min(hits * 0.08, 0.3)
    gibberish_ratio = len(re.findall(r"[A-Za-z0-9]", text)) / max(len(text), 1)
    if gibberish_ratio > 0.35:
        score += 0.1
    return max(0.0, min(score, 1.0))


def compute_field_confidences(
    *,
    metadata: dict[str, Any],
    engine: str,
    text_quality: float,
) -> dict[str, float]:
    weight = ENGINE_WEIGHTS.get(engine, 0.6)
    confidences: dict[str, float] = {}
    for field in ALL_FIELDS:
        value = metadata.get(field)
        if not _is_meaningful(value):
            confidences[field] = 0.0
            continue
        base = 0.35 + (weight * 0.45) + (text_quality * 0.2)
        if field in {"date", "warranty_start", "warranty_end"} and _safe_date_iso(value):
            base += 0.05
        if field in NUMERIC_FIELDS and _safe_float(value) is not None:
            base += 0.05
        if field == "bill_id":
            token = str(value).strip()
            if re.search(r"[A-Z0-9]{3,}", token):
                base += 0.05
        if field == "line_items" and isinstance(value, list):
            if value:
                base += min(len(value), 5) * 0.01
        confidences[field] = max(0.0, min(base, 1.0))
    return confidences


def merge_engine_results(
    engine_results: list[dict[str, Any]],
    *,
    manual_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, float], dict[str, str]]:
    merged: dict[str, Any] = {field: None for field in ALL_FIELDS}
    merged["line_items"] = []
    confidence_map: dict[str, float] = {field: 0.0 for field in ALL_FIELDS}
    source_map: dict[str, str] = {field: "none" for field in ALL_FIELDS}

    for field in ALL_FIELDS:
        votes: dict[str, dict[str, Any]] = {}
        for result in engine_results:
            metadata = result.get("metadata")
            if not isinstance(metadata, dict):
                continue
            value = metadata.get(field)
            if not _is_meaningful(value):
                continue
            field_confidences = result.get("field_confidences")
            confidence = 0.0
            if isinstance(field_confidences, dict):
                confidence = _safe_float(field_confidences.get(field)) or 0.0
            confidence = max(0.0, min(confidence, 1.0))
            vote_key = _normalized_vote_key(field, value)
            if not vote_key:
                continue
            existing = votes.get(vote_key)
            if existing is None:
                votes[vote_key] = {
                    "value": value,
                    "support": confidence,
                    "best_confidence": confidence,
                    "source": str(result.get("engine") or "unknown"),
                }
            else:
                existing["support"] = float(existing["support"]) + confidence
                if confidence > float(existing["best_confidence"]):
                    existing["best_confidence"] = confidence
                    existing["value"] = value
                    existing["source"] = str(result.get("engine") or "unknown")
        if votes:
            winner = sorted(
                votes.values(),
                key=lambda item: (float(item["support"]), float(item["best_confidence"])),
                reverse=True,
            )[0]
            merged[field] = winner["value"]
            confidence_map[field] = max(
                0.0,
                min((float(winner["support"]) / max(len(engine_results), 1)), 1.0),
            )
            source_map[field] = str(winner["source"])

    if manual_overrides:
        for field, value in manual_overrides.items():
            if field not in ALL_FIELDS:
                continue
            if not _is_meaningful(value):
                continue
            merged[field] = value
            confidence_map[field] = 1.0
            source_map[field] = "manual_override"

    strict = ensure_strict_extraction(merged)
    if strict.get("_schema_error"):
        # Reset confidences for fields that failed schema coercion.
        for field in ALL_FIELDS:
            if field not in strict:
                confidence_map[field] = 0.0
                source_map[field] = "schema_reject"
    return strict, confidence_map, source_map


def prefer_grounded_ocr_fields(
    merged_metadata: dict[str, Any],
    grounded_metadata: dict[str, Any],
    *,
    confidence_map: dict[str, float] | None = None,
    source_map: dict[str, str] | None = None,
    grounded_confidence_map: dict[str, float] | None = None,
    grounded_source_map: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, float], dict[str, str]]:
    stabilized = ensure_strict_extraction(merged_metadata)
    grounded = ensure_strict_extraction(grounded_metadata)
    merged_confidences = dict(confidence_map or {})
    merged_sources = dict(source_map or {})
    grounded_confidences = dict(grounded_confidence_map or {})
    grounded_sources = dict(grounded_source_map or {})

    for field in GROUNDED_OCR_FIELDS:
        value = grounded.get(field)
        if not _is_meaningful(value):
            continue
        stabilized[field] = value
        grounded_confidence = _safe_float(grounded_confidences.get(field))
        if grounded_confidence is not None:
            merged_confidences[field] = max(0.0, min(grounded_confidence, 1.0))
        grounded_source = str(grounded_sources.get(field) or "").strip()
        if grounded_source:
            merged_sources[field] = grounded_source

    stabilized = ensure_strict_extraction(stabilized)
    return stabilized, merged_confidences, merged_sources


def build_review_fields(
    field_confidences: dict[str, float],
    *,
    threshold: float,
) -> list[str]:
    low: list[str] = []
    for field in REVIEW_FIELDS:
        confidence = _safe_float(field_confidences.get(field))
        if confidence is None or confidence < threshold:
            low.append(field)
    return sorted(low)


def extraction_fingerprint(metadata: dict[str, Any], raw_text: str) -> str:
    canonical = {
        "bill_id": metadata.get("bill_id"),
        "vendor": metadata.get("vendor"),
        "date": metadata.get("date"),
        "total_amount": metadata.get("total_amount"),
        "product_name": metadata.get("product_name"),
    }
    raw = f"{canonical}|{(raw_text or '')[:2500]}"
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def estimate_claim_readiness(
    *,
    warranty_end: date | None,
    now: date,
    has_invoice_number: bool,
    has_vendor: bool,
    has_purchase_date: bool,
    has_amount: bool,
    has_serial: bool,
    has_service_centers: bool,
) -> dict[str, Any]:
    factors: dict[str, float] = {
        "document_completeness": 0.0,
        "time_buffer": 0.0,
        "serviceability": 0.0,
    }

    completeness_signals = [has_invoice_number, has_vendor, has_purchase_date, has_amount, has_serial]
    completeness_ratio = sum(1 for signal in completeness_signals if signal) / len(completeness_signals)
    factors["document_completeness"] = round(completeness_ratio, 3)

    days_left = 0
    if warranty_end is not None:
        days_left = (warranty_end - now).days
    if days_left <= 0:
        factors["time_buffer"] = 0.0
    elif days_left <= 7:
        factors["time_buffer"] = 0.3
    elif days_left <= 30:
        factors["time_buffer"] = 0.65
    else:
        factors["time_buffer"] = 1.0

    factors["serviceability"] = 1.0 if has_service_centers else 0.4

    score = (
        factors["document_completeness"] * 0.45
        + factors["time_buffer"] * 0.35
        + factors["serviceability"] * 0.20
    )
    score = round(max(0.0, min(score, 1.0)), 3)

    missing = []
    if not has_invoice_number:
        missing.append("invoice_number")
    if not has_vendor:
        missing.append("vendor")
    if not has_purchase_date:
        missing.append("purchase_date")
    if not has_amount:
        missing.append("amount")
    if not has_serial:
        missing.append("serial_number")
    if not has_service_centers:
        missing.append("service_center")

    if score < 0.45:
        label = "needs_attention"
        summary = "Claim data is incomplete and deadline risk is elevated."
    elif score < 0.75:
        label = "progressing"
        summary = "Claim prep is on track but still needs a few checks."
    else:
        label = "ready"
        summary = "Claim packet quality is strong with healthy deadline buffer."

    if days_left <= 0:
        deadline_risk = "expired"
    elif days_left <= 7:
        deadline_risk = "critical"
    elif days_left <= 30:
        deadline_risk = "watch"
    else:
        deadline_risk = "stable"

    recommended_actions: list[str] = []
    if "invoice_number" in missing:
        recommended_actions.append("Upload or verify invoice number before raising claim.")
    if "serial_number" in missing:
        recommended_actions.append("Add product serial number photo to strengthen claim validation.")
    if "service_center" in missing:
        recommended_actions.append("Find nearby authorized service center and attach service reference.")
    if deadline_risk in {"critical", "watch"}:
        recommended_actions.append("Raise claim request today to avoid missing warranty deadline.")
    if not recommended_actions:
        recommended_actions.append("Claim packet looks ready. Submit issue summary with attachments.")

    return {
        "score": score,
        "label": label,
        "summary": summary,
        "factors": factors,
        "missing": missing,
        "days_left": days_left,
        "deadline_risk": deadline_risk,
        "recommended_actions": recommended_actions[:5],
    }
