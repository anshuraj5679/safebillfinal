from __future__ import annotations

import base64
import io
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from dateutil import parser as date_parser

try:
    import pandas as pd
except Exception:  # pragma: no cover - optional runtime dependency
    pd = None

try:
    import pdfplumber
except Exception:  # pragma: no cover - optional runtime dependency
    pdfplumber = None

try:
    import pytesseract
except Exception:  # pragma: no cover - optional runtime dependency
    pytesseract = None

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

# NOTE: `unstructured` (and its inference stack) is heavy and can add minutes
# of import time on cold starts. Keep it lazily imported inside
# `_partition_sections()` so the API can bind its port quickly when
# `USE_UNSTRUCTURED_PARTITION=false` (the default).

from app.core.config import get_settings
from app.services.date_utils import add_months
from app.services.extraction_pipeline import sanitize_merchandise_name

NUMERIC_TOKEN_PATTERN = r"[0-9][0-9,]*(?:[.,]\d{1,3})*"
CURRENCY_RE = re.compile(rf"[-+]?{NUMERIC_TOKEN_PATTERN}")
DECIMAL_AMOUNT_RE = re.compile(rf"(?<!\d)({NUMERIC_TOKEN_PATTERN}[.,]\d{{2}})(?!\d)")


@dataclass
class TableRow:
    row_index: int
    values: dict[str, str]
    numeric_values: dict[str, float]
    raw: list[str]


@dataclass
class PageSection:
    page_number: int
    section_type: str
    text: str
    metadata: dict[str, Any]


@dataclass
class ParsedDocument:
    raw_text: str
    sections: list[PageSection]
    tables: list[dict[str, Any]]
    metadata: dict[str, Any]
    is_scanned: bool


def normalize_numeric_field(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value
    for token in ("INR", "Rs.", "Rs", "$", "\u20b9", "EUR", "USD"):
        cleaned = cleaned.replace(token, "")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    match = CURRENCY_RE.search(cleaned)
    if not match:
        return None
    token = match.group(0).strip(".,")
    if not token:
        return None
    sign = ""
    if token[0] in "+-":
        sign = token[0]
        token = token[1:]
    if not token:
        return None
    if "." in token or "," in token:
        last_dot = token.rfind(".")
        last_comma = token.rfind(",")
        decimal_index = max(last_dot, last_comma)
        decimal_part = token[decimal_index + 1 :]
        integer_part = token[:decimal_index]
        if integer_part and decimal_part.isdigit() and len(decimal_part) <= 2:
            normalized_int = re.sub(r"[.,]", "", integer_part)
            if normalized_int:
                try:
                    return float(f"{sign}{normalized_int}.{decimal_part}")
                except ValueError:
                    pass
        if token.count(".") + token.count(",") >= 1:
            normalized_int = re.sub(r"[.,]", "", token)
            if normalized_int:
                try:
                    return float(f"{sign}{normalized_int}")
                except ValueError:
                    pass
    try:
        return float(f"{sign}{token.replace(',', '')}")
    except ValueError:
        return None


def _safe_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date_parser.parse(value, dayfirst=True, fuzzy=True).date()
    except (ValueError, TypeError, OverflowError):
        return None


def _normalize_text(raw_text: str) -> str:
    return (
        (raw_text or "")
        .replace("\u20b9", " INR ")
        .replace("\u00a0", " ")
        .replace("\r", "\n")
    )


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_non_merchandise_label(value: str) -> bool:
    text = _clean_line(value).lower().strip(":- ")
    if not text:
        return False

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
        "phone",
        "mobile",
        "email",
        "address",
        "pincode",
        "postal code",
        "serial number",
        "serial numbers",
        "hsn code",
        "item number",
        "tax rate",
        "digitally signed",
        "authorized signatory",
        "reason: invoice",
    )
    if any(token in text for token in blocked_tokens):
        return True

    if re.fullmatch(r"(?:customer|invoice|document|order|po|gst|pan|hsn|item)\s*(?:no|number|id|code)", text):
        return True
    return False


def _looks_like_product_spec_amount(amount: float | None, context_text: str | None) -> bool:
    if amount is None:
        return False
    text = _clean_line(context_text or "")
    if not text:
        return False
    rounded = round(amount)
    if abs(amount - rounded) > 1e-6:
        return False
    size = int(rounded)
    if size <= 0 or size > 5000:
        return False
    spec_units = (
        "gb",
        "tb",
        "mb",
        "mah",
        "hz",
        "inch",
        "inches",
        "cm",
        "mm",
        "mp",
        "w",
        "kw",
    )
    joined_units = "|".join(spec_units)
    return bool(
        re.search(rf"(?i)\b{size}\s*(?:{joined_units})\b", text)
        or re.search(rf"(?i)\b(?:{joined_units})\s*{size}\b", text)
    )


def _extract_labeled_value(text: str, labels: list[str]) -> str | None:
    joined = "|".join(labels)
    match = re.search(rf"(?im)^\s*(?:{joined})\s*[:\-]\s*(.+?)\s*$", text)
    if not match:
        return None
    value = _clean_line(match.group(1))
    return value or None


def _extract_labeled_date(text: str, labels: list[str]) -> date | None:
    joined = "|".join(labels)
    date_pattern = (
        r"("
        r"[0-3]?\d[\/\-.][01]?\d[\/\-.]\d{2,4}"
        r"|\d{4}[\/\-.][01]?\d[\/\-.][0-3]?\d"
        r"|[0-3]?\d[\/\-.][A-Za-z]{3,9}[\/\-.]\d{2,4}"
        r"|[A-Za-z]{3,9}[\/\-.][0-3]?\d[\/\-.]\d{2,4}"
        r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}"
        r"|[0-3]?\d\s+[A-Za-z]{3,9}\s+\d{2,4}"
        r")"
    )
    match = re.search(rf"(?im)(?:{joined})\s*[:\-]?\s*{date_pattern}", text)
    return _safe_date(match.group(1)) if match else None


def _extract_bill_id(text: str, filename: str) -> str:
    patterns: list[tuple[int, str]] = [
        (72, r"(?im)\btax\s*invoice\s*number\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-\/]{4,})"),
        (62, r"(?im)\b(?:apple\s*)?document\s*number\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-\/]{4,})"),
        (40, r"(?im)\b(?:invoice\s*\/\s*bill|bill\s*\/\s*invoice)\s*(?:no|number|#|id)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-\/]{2,})"),
        (40, r"(?im)\binvoice\s*(?:no\.?|number|#|id)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-\/]{2,})"),
        (35, r"(?im)\binv\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-\/]{2,})"),
        (30, r"(?im)\b(INV[\-\/]?[A-Z0-9]{3,})\b"),
        (10, r"(?im)\b(?:bill|receipt)\s*(?:no|number|#|id)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-\/]{2,})"),
        (5, r"(?im)\border\s*(?:no|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-\/]{5,})"),
    ]
    candidates: list[tuple[int, str]] = []
    for priority, pattern in patterns:
        for match in re.finditer(pattern, text):
            value = _clean_line(match.group(1)).strip(".,;:")
            if not value:
                continue
            if value.lower() in {"document", "invoice", "bill", "receipt", "original", "recipient"}:
                continue
            # Skip purchase-order references masquerading as invoice number.
            full_match = _clean_line(match.group(0)).lower()
            if "po bill" in full_match or "purchase order" in full_match or "original for recipient" in full_match:
                continue
            prefix = text[max(0, match.start() - 24): match.start()].lower()
            if "po " in prefix or "purchase order" in prefix:
                continue
            explicit_invoice_label = bool(
                re.search(r"(?i)\b(?:tax\s*)?invoice\s*(?:no\.?|number|#|id)\b", full_match)
            )
            if value.isdigit() and len(value) >= 15 and not explicit_invoice_label:
                continue
            score = priority
            if value.upper().startswith("INV"):
                score += 10
            if any(ch.isalpha() for ch in value):
                score += 3
            candidates.append((score, value[:128]))
    if candidates:
        candidates.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
        return candidates[0][1]
    return os.path.splitext(filename)[0][:128]


def _extract_vendor(text: str, lines: list[str]) -> str:
    multiline_match = re.search(
        r"(?im)^\s*(?:sold\s+by|seller|vendor|merchant|supplier|store(?:\s*name)?|shop(?:\s*name)?)\s*[:\-]\s*\n\s*([^\n]{2,120})",
        text,
    )
    if multiline_match:
        multiline_value = _clean_line(multiline_match.group(1))
        if multiline_value and not re.fullmatch(r"[\W_]+", multiline_value):
            return multiline_value[:255]

    labeled = _extract_labeled_value(
        text,
        [
            r"vendor",
            r"supplier",
            r"merchant",
            r"seller",
            r"sold\s+by",
            r"store(?:\s*name)?",
            r"shop(?:\s*name)?",
            r"company(?:\s*name)?",
            r"manufacturer",
            r"from",
        ],
    )
    if labeled:
        return labeled[:255]

    ignored_tokens = (
        "invoice",
        "bill",
        "receipt",
        "recipient",
        "tax invoice",
        "original for recipient",
        "bill to",
        "shipping to",
        "additional details",
        "gst",
        "tax",
        "date",
        "phone",
        "email",
        "address",
        "qty",
        "amount",
        "total",
        "hsn",
        "serial",
        "warranty",
    )
    for raw_line in lines[:12]:
        line = _clean_line(raw_line)
        lowered = line.lower()
        if len(line) < 3 or len(line) > 80:
            continue
        if any(token in lowered for token in ignored_tokens):
            continue
        if lowered in {"original for recipient", "tax invoice", "invoice"}:
            continue
        if re.search(r"\d{4,}", line):
            continue
        if re.fullmatch(r"[\W_]+", line):
            continue
        return line[:255]

    return "UNKNOWN_VENDOR"


def _extract_total_amount(text: str) -> float | None:
    line_priorities: list[tuple[int, tuple[str, ...]]] = [
        (7, ("total amount after tax",)),
        (6, ("amount after tax",)),
        (5, ("grand total",)),
        (4, ("invoice total", "final amount")),
        (3, ("total amount", "amount due", "amount paid")),
        (2, ("total price",)),
        (1, ("total",)),
    ]
    numeric_token_re = re.compile(r"(?i)(?:inr|rs\.?|\$|₹)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)")
    ranked: list[tuple[int, float]] = []

    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        lowered = line.lower()
        if not line:
            continue
        if any(token in lowered for token in ("taxable amount", "total tax", "gst amount", "cgst", "sgst", "igst")):
            continue
        tokens = [normalize_numeric_field(token) for token in numeric_token_re.findall(line)]
        amounts = [value for value in tokens if value is not None and 0 < value <= 10_000_000]
        if not amounts:
            continue
        for priority, labels in line_priorities:
            if not any(label in lowered for label in labels):
                continue
            ranked.append((priority, amounts[-1]))
            break

    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked[0][1]


def _extract_summary_amounts_from_lines(lines: list[str]) -> dict[str, float | None]:
    label_tokens = (
        "taxable amount",
        "total tax",
        "tax amount",
        "gst amount",
        "amount after tax",
        "total amount after",
        "grand total",
        "invoice total",
        "final amount",
    )
    label_indexes = [
        index
        for index, raw_line in enumerate(lines)
        if any(token in raw_line.lower() for token in label_tokens)
    ]
    if not label_indexes:
        return {}

    start = max(0, min(label_indexes) - 8)
    end = min(len(lines), max(label_indexes) + 8)
    block = lines[start:end]
    decimal_amounts: list[float] = []
    for line in block:
        for token in DECIMAL_AMOUNT_RE.findall(line):
            value = normalize_numeric_field(token)
            if value is None or value <= 0 or value > 1_000_000:
                continue
            decimal_amounts.append(round(value, 2))

    if not decimal_amounts:
        return {}

    unique_amounts = sorted(set(decimal_amounts))
    total_amount = max(unique_amounts)
    taxable_amount: float | None = None
    gst_amount: float | None = None

    best_pair: tuple[float, float, float] | None = None
    for first in unique_amounts:
        for second in unique_amounts:
            larger = max(first, second)
            smaller = min(first, second)
            if larger >= total_amount or smaller <= 0:
                continue
            delta = abs((larger + smaller) - total_amount)
            if delta > 1.0:
                continue
            candidate = (delta, -larger, -smaller)
            if best_pair is None or candidate < best_pair:
                best_pair = candidate
                taxable_amount = larger
                gst_amount = smaller

    if taxable_amount is None and len(unique_amounts) >= 2:
        taxable_amount = unique_amounts[-2]
        diff = round(total_amount - taxable_amount, 2)
        if diff > 0:
            gst_amount = diff

    gst_rate = None
    if taxable_amount and gst_amount and taxable_amount > 0:
        gst_rate = round((gst_amount / taxable_amount) * 100.0, 2)

    return {
        "total_amount": total_amount,
        "taxable_amount": taxable_amount,
        "gst_amount": gst_amount,
        "gst_rate": gst_rate,
    }


def _extract_tax_breakdown(text: str) -> dict[str, float | None]:
    currency = r"(?:inr|rs\.?|usd|eur|\$|\u20b9)"

    def _extract_amount(patterns: list[str]) -> float | None:
        ranked: list[tuple[int, float]] = []
        for priority, pattern in enumerate(patterns, start=1):
            for match in re.finditer(pattern, text):
                value = normalize_numeric_field(match.group(1))
                if value is None:
                    continue
                ranked.append((priority, value))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return ranked[0][1]

    def _extract_rate() -> float | None:
        patterns = [
            r"(?im)(?:gst\s*rate|tax\s*rate)\s*[:\-]?\s*([0-9]{1,2}(?:\.[0-9]+)?)\s*%",
            r"(?im)(?:cgst|sgst|igst)\s*[:\-]?\s*([0-9]{1,2}(?:\.[0-9]+)?)\s*%",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            try:
                return float(match.group(1))
            except ValueError:
                continue
        return None

    taxable_amount = _extract_amount(
        [
            rf"(?im)(?:taxable\s*amount|subtotal|sub\s*total)\s*[:\-]?\s*{currency}?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)",
        ]
    )
    gst_amount = _extract_amount(
        [
            rf"(?im)(?:gst\s*amount|total\s*gst|tax\s*amount)\s*[:\-]?\s*{currency}?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)",
            rf"(?im)(?:total\s*tax)\s*[:\-]?\s*{currency}?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)",
        ]
    )
    cgst_amount = _extract_amount(
        [
            rf"(?im)(?:cgst)\s*[:\-]?\s*{currency}?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)",
        ]
    )
    sgst_amount = _extract_amount(
        [
            rf"(?im)(?:sgst)\s*[:\-]?\s*{currency}?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)",
        ]
    )
    igst_amount = _extract_amount(
        [
            rf"(?im)(?:igst)\s*[:\-]?\s*{currency}?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)",
        ]
    )

    if gst_amount is None:
        split_total = sum(part for part in [cgst_amount, sgst_amount, igst_amount] if part is not None)
        if split_total > 0:
            gst_amount = round(split_total, 2)

    gst_rate = _extract_rate()
    if gst_rate is None and taxable_amount and gst_amount and taxable_amount > 0:
        gst_rate = round((gst_amount / taxable_amount) * 100.0, 2)

    return {
        "taxable_amount": taxable_amount,
        "gst_amount": gst_amount,
        "gst_rate": gst_rate,
        "cgst_amount": cgst_amount,
        "sgst_amount": sgst_amount,
        "igst_amount": igst_amount,
    }


def _extract_line_items_from_text(lines: list[str]) -> list[dict[str, Any]]:
    recovered_rows: list[dict[str, Any]] = []
    seen_row_names: set[str] = set()
    trailing_value_re = re.compile(
        rf"\s+(?:\d+%|[A-Z]{{2,8}}|{NUMERIC_TOKEN_PATTERN})(?:\s+(?:\d+%|[A-Z]{{2,8}}|{NUMERIC_TOKEN_PATTERN})){{2,}}\s*$"
    )
    for index, raw_line in enumerate(lines):
        line = _clean_line(raw_line)
        lowered = line.lower()
        if not line or _looks_like_non_merchandise_label(line):
            continue
        if any(token in lowered for token in ("digitally signed", "authorized signatory", "reason: invoice")):
            continue
        decimal_amounts = [
            normalize_numeric_field(token)
            for token in DECIMAL_AMOUNT_RE.findall(line)
        ]
        decimal_amounts = [
            round(value, 2)
            for value in decimal_amounts
            if value is not None and 0 < value <= 1_000_000
        ]
        if len(decimal_amounts) < 2:
            continue
        candidate_source = line
        if index > 0:
            previous_line = _clean_line(lines[index - 1])
            previous_lowered = previous_line.lower()
            if (
                previous_line
                and not DECIMAL_AMOUNT_RE.findall(previous_line)
                and not _looks_like_non_merchandise_label(previous_line)
                and not any(token in previous_lowered for token in ("digitally signed", "authorized signatory", "reason: invoice"))
                and not any(token in previous_lowered for token in ("invoice", "bill", "date", "gst", "tax", "order"))
                and len(re.findall(r"[A-Za-z]{2,}", previous_line)) >= 2
            ):
                prev_clean = re.sub(r"^\d+\s+", "", previous_line).strip()
                candidate_source = f"{prev_clean} {line}"
        candidate = re.sub(r"^\d+\s+", "", candidate_source)
        candidate = trailing_value_re.sub("", candidate).strip(":- ")
        candidate = str(sanitize_merchandise_name(candidate) or candidate).strip(":- ")
        if len(re.findall(r"[A-Za-z]{2,}", candidate)) < 2:
            continue
        if _looks_like_non_merchandise_label(candidate):
            continue
        key = candidate.lower()
        if key in seen_row_names:
            continue
        amount = decimal_amounts[-1]
        if amount <= 100 and len(decimal_amounts) >= 2:
            amount = max(decimal_amounts[:-1])
        seen_row_names.add(key)
        recovered_rows.append({"name": candidate[:255], "amount": amount})
    if recovered_rows:
        return recovered_rows[:50]

    table_header_index = next(
        (
            index
            for index, raw_line in enumerate(lines)
            if "name of product/service" in raw_line.lower()
            or "name of product / service" in raw_line.lower()
            or ("name of product" in raw_line.lower() and "qty" in raw_line.lower())
        ),
        -1,
    )
    if table_header_index >= 0:
        section_end = len(lines)
        for index in range(table_header_index + 1, min(len(lines), table_header_index + 40)):
            lowered = lines[index].lower()
            if any(
                token in lowered
                for token in (
                    "total in words",
                    "terms and conditions",
                    "customer signature",
                    "authorised signatory",
                    "bank:",
                )
            ):
                section_end = index
                break

        section_lines = lines[table_header_index + 1 : section_end]
        table_names: list[str] = []
        table_amounts: list[float] = []
        seen_names: set[str] = set()
        for raw_line in section_lines:
            line = _clean_line(raw_line)
            lowered = line.lower()
            if not line:
                continue
            if any(
                token in lowered
                for token in (
                    "hsn/sac",
                    "hsn",
                    "sac",
                    "qty",
                    "rate",
                    "taxable value",
                    "igst",
                    "cgst",
                    "sgst",
                    "amount",
                    "total",
                    "name of product",
                )
            ):
                continue
            if lowered in {"sr.", "sr", "no.", "no", "qty", "rate", "amount", "igst", "%", "total"}:
                continue
            if re.fullmatch(r"\d+\s*(?:nos|pcs|pc|unit|units)?", lowered):
                continue
            if re.fullmatch(r"[a-z0-9/-]{6,}", lowered) and any(ch.isdigit() for ch in lowered):
                continue
            candidate = sanitize_merchandise_name(line)
            if candidate:
                alpha_tokens = re.findall(r"[A-Za-z]{2,}", candidate)
                if len(alpha_tokens) >= 2 and not _looks_like_non_merchandise_label(candidate):
                    key = candidate.lower()
                    if key not in seen_names:
                        seen_names.add(key)
                        table_names.append(candidate)
            for token in DECIMAL_AMOUNT_RE.findall(line):
                value = normalize_numeric_field(token)
                if value is None or value <= 0 or value > 1_000_000:
                    continue
                table_amounts.append(round(value, 2))

        if table_names:
            paired_amounts = table_amounts[-len(table_names) :] if len(table_amounts) >= len(table_names) else []
            if len(table_names) == 1 and table_amounts:
                paired_amounts = [max(table_amounts)]
            recovered: list[dict[str, Any]] = []
            for index, name in enumerate(table_names):
                entry: dict[str, Any] = {"name": name}
                if index < len(paired_amounts):
                    entry["amount"] = paired_amounts[index]
                recovered.append(entry)
            if recovered:
                return recovered[:50]

    items: list[dict[str, Any]] = []
    ignored_tokens = (
        "invoice",
        "inv",
        "bill",
        "receipt",
        "document number",
        "invoice number",
        "tax invoice number",
        "customer number",
        "bill to",
        "ship to",
        "shipping to",
        "shipping details",
        "place of supply",
        "account number",
        "ifsc",
        "bank details",
        "po bill",
        "purchase order",
        "date",
        "total",
        "subtotal",
        "tax",
        "gst",
        "cgst",
        "sgst",
        "igst",
        "amount due",
        "amount paid",
        "grand total",
        "warranty",
    )
    amount_re = re.compile(rf"(?i)(?:inr|rs\.?|\$)?\s*({NUMERIC_TOKEN_PATTERN})\s*$")
    numeric_re = re.compile(rf"(?i)(?:inr|rs\.?|\$|₹)?\s*({NUMERIC_TOKEN_PATTERN})")
    currency_re = re.compile(r"(?i)\b(?:inr|rs\.?)\b|[₹$]")

    for raw_line in lines:
        line = _clean_line(raw_line)
        if len(line) < 5 or len(line) > 120:
            continue
        lowered = line.lower()
        if _looks_like_non_merchandise_label(line):
            continue
        if any(token in lowered for token in ignored_tokens):
            continue

        numeric_tokens = numeric_re.findall(line)
        numeric_candidates = [normalize_numeric_field(token) for token in numeric_tokens]
        numeric_candidates = [value for value in numeric_candidates if value is not None and 0 < value <= 1_000_000]
        if not numeric_candidates:
            continue
        amount_match = amount_re.search(line)
        amount = normalize_numeric_field(amount_match.group(1)) if amount_match else None
        decimal_candidates = [
            normalize_numeric_field(token)
            for token in numeric_tokens
            if "." in token or "," in token
        ]
        decimal_candidates = [value for value in decimal_candidates if value is not None and 0 < value <= 1_000_000]
        has_currency = bool(currency_re.search(line))
        if decimal_candidates:
            amount = decimal_candidates[-1]
            if amount is not None and amount <= 100 and len(decimal_candidates) >= 2:
                amount = max(decimal_candidates[:-1])
        elif amount is None:
            if amount_match is None and not has_currency:
                continue
            amount = numeric_candidates[-1]
        if amount is None or amount <= 0:
            continue
        if _looks_like_product_spec_amount(amount, line) and not has_currency:
            continue
        if len(numeric_candidates) == 1 and not has_currency:
            if ":" in line:
                continue
            if amount >= 100_000:
                continue
        if re.fullmatch(r"\d+\s*(?:nos|pcs|pc|unit|units)", lowered):
            continue
        if amount > 1_000_000:
            continue

        name = _clean_line(
            re.sub(
                r"\s+[0-9][0-9,]*(?:\.\d{1,2})?(?:\s+[0-9][0-9,]*(?:\.\d{1,2})?){0,6}\s*$",
                "",
                line,
            )
        ).strip(":- ")
        if not name and amount_match:
            name = _clean_line(line[: amount_match.start()]).strip(":- ")
        if len(name) < 2:
            continue
        sanitized_name = sanitize_merchandise_name(name)
        if not sanitized_name:
            continue
        name = str(sanitized_name).strip(":- ")
        if re.fullmatch(r"[A-Z0-9\-/]{6,}", name) and any(ch.isdigit() for ch in name):
            continue
        if re.fullmatch(r"\d+\s*(?:NOS|PCS|PC|UNIT|UNITS)", name.upper()):
            continue
        if len(re.findall(r"[A-Za-z]{2,}", name)) < 2:
            continue
        if _looks_like_non_merchandise_label(name):
            continue
        if ":" in name and any(
            token in name.lower()
            for token in (
                "date",
                "account",
                "ifsc",
                "invoice",
                "bill",
                "order",
                "customer",
                "document",
                "ship to",
                "place of supply",
            )
        ):
            continue

        items.append(
            {
                "name": name[:255],
                "amount": round(amount, 2),
            }
        )

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for item in items:
        key = (str(item.get("name", "")).lower(), float(item.get("amount") or 0.0))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:12]


def _extract_vendor_tax_id(text: str) -> str | None:
    gstin_match = re.search(r"(?i)\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z])\b", text)
    if gstin_match:
        return gstin_match.group(1)
    generic_match = re.search(r"(?im)(?:gstin|tax\s*id|vat\s*id)\s*[:\-]?\s*([A-Z0-9\-]{6,32})", text)
    if generic_match:
        return _clean_line(generic_match.group(1))
    return None


def _extract_serial_number(text: str) -> str | None:
    match = re.search(
        r"(?im)(?:serial\s*(?:no|number)\b|s\/n\b|imei(?:\s*(?:no|number))?)\s*[:\-]?\s*([A-Z0-9\-\/]{4,64})",
        text,
    )
    if match:
        candidate = _clean_line(match.group(1))
        if candidate.lower() in {"number", "numbers", "item"}:
            return None
        return candidate
    return None


def _extract_product_name(text: str, lines: list[str], vendor: str) -> str | None:
    labeled = _extract_labeled_value(
        text,
        [
            r"product(?:\s*name)?",
            r"item(?:\s*name)?",
            r"description",
            r"model(?:\s*name)?",
            r"device",
        ],
    )
    if labeled:
        return labeled[:255]

    # Prefer table-like item rows (name + HSN/item code + qty + amounts).
    for raw_line in lines:
        line = _clean_line(raw_line)
        row_match = re.search(
            r"(?i)^(.+?)\s+\d{4,10}\s+\d+(?:\s+[A-Z]{1,5})?\s+[0-9][0-9,]*(?:\.\d+)?(?:\s+[0-9][0-9,]*(?:\.\d+)?){1,5}\s*$",
            line,
        )
        if not row_match:
            continue
        candidate = _clean_line(row_match.group(1)).strip(":- ")
        candidate = str(sanitize_merchandise_name(candidate) or candidate).strip(":- ")
        if len(candidate) < 4:
            continue
        if any(token in candidate.lower() for token in ("total", "tax", "gst", "shipping", "bank")):
            continue
        return candidate[:255]

    ignored_tokens = (
        "invoice",
        "inv",
        "bill",
        "receipt",
        "document number",
        "invoice number",
        "tax invoice number",
        "customer number",
        "order number",
        "purchase order",
        "po bill",
        "ship to",
        "account number",
        "ifsc",
        "bank details",
        "original for recipient",
        "bill to",
        "shipping to",
        "place of supply",
        "total",
        "subtotal",
        "tax",
        "gst",
        "date",
        "qty",
        "hsn",
        "warranty",
        "serial",
        "address",
        "phone",
        "email",
        "amount",
    )
    lowered_vendor = vendor.lower()
    for raw_line in lines[:24]:
        line = _clean_line(raw_line)
        lowered = line.lower()
        if len(line) < 4 or len(line) > 90:
            continue
        if lowered == lowered_vendor:
            continue
        if _looks_like_non_merchandise_label(line):
            continue
        if any(token in lowered for token in ("enterprise", "corporation", "limited", "ltd", "pvt", "private", "llp", "inc")):
            continue
        if any(token in lowered for token in ignored_tokens):
            continue
        if re.search(r"\b\d{6,}\b", line):
            continue
        if re.search(r"(?:inr|rs\.?|usd|\$)\s*[0-9]", lowered):
            continue
        if ":" in line and any(
            marker in lowered
            for marker in (
                "order",
                "invoice no",
                "bill no",
                "gstin",
                "account",
                "ifsc",
                "customer number",
                "document number",
                "ship to",
                "bill to",
                "place of supply",
            )
        ):
            continue
        if re.fullmatch(r"[A-Z0-9\-\/]{4,}", line):
            continue
        return line[:255]
    return None


def _extract_brand(text: str, product_name: str | None, vendor: str) -> str | None:
    labeled = _extract_labeled_value(
        text,
        [
            r"brand",
            r"make",
            r"manufacturer",
            r"company",
        ],
    )
    if labeled:
        return labeled[:255]

    if product_name:
        first_token = product_name.split(" ", 1)[0].strip()
        if first_token and first_token.isalpha() and len(first_token) >= 2:
            return first_token[:64]

    if vendor and vendor != "UNKNOWN_VENDOR":
        return vendor[:255]
    return None


def _extract_warranty_months(text: str) -> int | None:
    match = re.search(
        r"(?im)(?:warranty(?:\s*(?:period|tenure|duration))?|guarantee(?:\s*period)?)\s*[:\-]?\s*(\d{1,3})\s*(month|months|year|years)",
        text,
    )
    if not match:
        match = re.search(
            r"(?im)(\d{1,3})\s*(month|months|year|years)\s*(?:manufacturer\s*)?(?:warranty|guarantee)",
            text,
        )
    if not match:
        return None
    raw_value = int(match.group(1))
    unit = match.group(2).lower()
    return raw_value * 12 if "year" in unit else raw_value


def _derive_category(product_name: str | None, brand: str | None, vendor: str) -> str:
    combined = " ".join([product_name or "", brand or "", vendor or ""]).lower()
    if any(token in combined for token in ("bike", "car", "scooter", "motorcycle", "vehicle", "tyre", "helmet")):
        return "Vehicle"
    if any(
        token in combined
        for token in (
            "refrigerator",
            "fridge",
            "washing machine",
            "microwave",
            "oven",
            "air conditioner",
            "geyser",
            "dishwasher",
            "appliance",
            "television",
            "tv",
        )
    ):
        return "Appliances"
    if any(
        token in combined
        for token in (
            "laptop",
            "phone",
            "mobile",
            "tablet",
            "watch",
            "camera",
            "headphone",
            "earbud",
            "monitor",
            "printer",
            "gadget",
        )
    ):
        return "Gadgets"
    return "Others"


def extract_invoice_metadata(text: str, filename: str) -> dict[str, Any]:
    normalized = _normalize_text(text)
    lines = [_clean_line(line) for line in normalized.splitlines() if _clean_line(line)]

    bill_id = _extract_bill_id(normalized, filename)
    vendor = _extract_vendor(normalized, lines)
    purchase_date = _extract_labeled_date(
        normalized,
        [
            r"invoice\s*date",
            r"bill\s*date",
            r"purchase\s*date",
            r"date\s*of\s*purchase",
            r"date",
        ],
    )
    warranty_start = _extract_labeled_date(
        normalized,
        [
            r"warranty\s*start(?:\s*date)?",
            r"coverage\s*start(?:\s*date)?",
            r"start\s*date",
        ],
    )
    warranty_end = _extract_labeled_date(
        normalized,
        [
            r"warranty\s*end(?:\s*date)?",
            r"warranty\s*expiry(?:\s*date)?",
            r"valid\s*(?:upto|until|till)",
            r"expires?\s*on",
            r"end\s*date",
        ],
    )

    total_amount = _extract_total_amount(normalized)
    vendor_tax_id = _extract_vendor_tax_id(normalized)
    tax_breakdown = _extract_tax_breakdown(normalized)
    serial_number = _extract_serial_number(normalized)
    warranty_months = _extract_warranty_months(normalized)

    line_items = _extract_line_items_from_text(lines)
    summary_amounts = _extract_summary_amounts_from_lines(lines)
    if summary_amounts.get("total_amount") is not None:
        total_amount = summary_amounts["total_amount"]
    if summary_amounts.get("taxable_amount") is not None:
        tax_breakdown["taxable_amount"] = summary_amounts["taxable_amount"]
    if summary_amounts.get("gst_amount") is not None:
        tax_breakdown["gst_amount"] = summary_amounts["gst_amount"]
    if summary_amounts.get("gst_rate") is not None:
        tax_breakdown["gst_rate"] = summary_amounts["gst_rate"]

    line_item_amounts = [
        round(amount, 2)
        for amount in (
            normalize_numeric_field(str(item.get("amount")))
            for item in line_items
            if isinstance(item, dict) and item.get("amount") is not None
        )
        if amount is not None and amount > 0
    ]
    if line_item_amounts:
        inferred_total = round(sum(line_item_amounts), 2) if len(line_item_amounts) > 1 else line_item_amounts[0]
        if total_amount is None or total_amount <= 0 or total_amount < inferred_total * 0.6:
            total_amount = inferred_total

    taxable_amount = normalize_numeric_field(str(tax_breakdown.get("taxable_amount")))
    gst_amount = normalize_numeric_field(str(tax_breakdown.get("gst_amount")))
    if total_amount is not None:
        for key in ("taxable_amount", "gst_amount", "cgst_amount", "sgst_amount", "igst_amount"):
            value = normalize_numeric_field(str(tax_breakdown.get(key)))
            if value is not None and value > total_amount * 1.05:
                tax_breakdown[key] = None
        taxable_amount = normalize_numeric_field(str(tax_breakdown.get("taxable_amount")))
        gst_amount = normalize_numeric_field(str(tax_breakdown.get("gst_amount")))
        if taxable_amount is not None and 0 < taxable_amount < total_amount:
            derived_gst = round(total_amount - taxable_amount, 2)
            if derived_gst > 0 and (gst_amount is None or abs((taxable_amount + gst_amount) - total_amount) > 2.0):
                tax_breakdown["gst_amount"] = derived_gst
                tax_breakdown["gst_rate"] = round((derived_gst / taxable_amount) * 100.0, 2)
                lowered = normalized.lower()
                if "igst" in lowered and "cgst" not in lowered and "sgst" not in lowered:
                    tax_breakdown["igst_amount"] = derived_gst

    product_name = _extract_product_name(normalized, lines, vendor)
    if line_items:
        first_item_name = str(line_items[0].get("name") or "").strip()
        if first_item_name and str(product_name or "").strip().lower() != first_item_name.lower():
            product_name = first_item_name
    brand = _extract_brand(normalized, product_name, vendor)
    category = _derive_category(product_name, brand, vendor)

    if warranty_start is None:
        warranty_start = purchase_date
    if warranty_end is None and warranty_start and warranty_months:
        warranty_end = add_months(warranty_start, warranty_months)

    return {
        "bill_id": bill_id,
        "vendor": vendor,
        "date": purchase_date,
        "total_amount": total_amount,
        "vendor_tax_id": vendor_tax_id,
        "taxable_amount": tax_breakdown.get("taxable_amount"),
        "gst_amount": tax_breakdown.get("gst_amount"),
        "gst_rate": tax_breakdown.get("gst_rate"),
        "cgst_amount": tax_breakdown.get("cgst_amount"),
        "sgst_amount": tax_breakdown.get("sgst_amount"),
        "igst_amount": tax_breakdown.get("igst_amount"),
        "product_name": product_name,
        "brand": brand,
        "serial_number": serial_number,
        "warranty_months": warranty_months,
        "warranty_start": warranty_start,
        "warranty_end": warranty_end,
        "category": category,
        "line_items": line_items,
    }


def _table_to_rows(table_data: list[list[str | None]]) -> list[TableRow]:
    if not table_data:
        return []
    normalized = [[(cell or "").strip() for cell in row] for row in table_data if any(cell for cell in row)]
    if len(normalized) < 2:
        return []

    header = [col if col else f"col_{idx+1}" for idx, col in enumerate(normalized[0])]
    rows: list[TableRow] = []
    for idx, row in enumerate(normalized[1:]):
        padded = row + [""] * max(0, len(header) - len(row))
        values = {header[col_idx]: padded[col_idx] for col_idx in range(len(header))}
        numeric_values = {}
        for key, value in values.items():
            numeric = normalize_numeric_field(value)
            if numeric is not None:
                numeric_values[key] = numeric
        rows.append(TableRow(row_index=idx, values=values, numeric_values=numeric_values, raw=padded))
    return rows


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


def _extract_text_with_google_vision(image_bytes: bytes) -> str:
    settings = get_settings()
    if not image_bytes or httpx is None:
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
        with httpx.Client(timeout=12.0) as client:
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


def _ocr_page_text(
    page: pdfplumber.page.Page,
    *,
    allow_google: bool = True,
    allow_local_fallback: bool = False,
) -> str:
    settings = get_settings()
    try:
        image = page.to_image(resolution=250).original
        with io.BytesIO() as buffer:
            image.save(buffer, format="PNG")
            image_bytes = buffer.getvalue()

        if allow_google:
            google_text = _extract_text_with_google_vision(image_bytes).strip()
            if google_text:
                return google_text

        # Try Gemini OCR as primary cloud fallback
        try:
            from app.services.gemini_client import gemini_ocr_image
            gemini_text = gemini_ocr_image(image_bytes, "ocr_page.png").strip()
            if gemini_text:
                return gemini_text
        except Exception:
            pass

        if allow_local_fallback and pytesseract is not None:
            if settings.tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
            return pytesseract.image_to_string(image)
        return ""
    except Exception:
        # OCR is best-effort; ingestion should continue even if engines are unavailable.
        return ""


def _partition_sections(file_bytes: bytes) -> list[PageSection]:
    try:
        # Lazily import to avoid heavy startup costs unless enabled via config.
        from unstructured.partition.pdf import partition_pdf as _partition_pdf
    except Exception:
        return []
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        elements = _partition_pdf(filename=tmp_path, strategy="hi_res", infer_table_structure=True)
    except Exception:
        return []
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    sections: list[PageSection] = []
    for element in elements:
        text = getattr(element, "text", "") or ""
        if not text.strip():
            continue
        metadata = getattr(element, "metadata", None)
        page_number = getattr(metadata, "page_number", 1) if metadata else 1
        section_type = element.__class__.__name__.lower()
        sections.append(
            PageSection(
                page_number=int(page_number),
                section_type=section_type,
                text=text.strip(),
                metadata={"source": "unstructured"},
            )
        )
    return sections


def parse_pdf_document(file_bytes: bytes, filename: str, ocr_mode_override: str | None = None) -> ParsedDocument:
    if pdfplumber is None:
        raise ImportError("pdfplumber is required for PDF ingestion.")

    sections: list[PageSection] = []
    tables: list[dict[str, Any]] = []
    raw_text_parts: list[str] = []
    top_lines: list[str] = []
    bottom_lines: list[str] = []
    page_lines_map: dict[int, list[str]] = {}

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(layout=True) or ""
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            page_lines_map[page_index] = lines

            if lines:
                top_lines.append(lines[0])
                bottom_lines.append(lines[-1])
            raw_text_parts.append("\n".join(lines))

            for table in page.extract_tables() or []:
                rows = _table_to_rows(table)
                if not rows:
                    continue
                tables.append(
                    {
                        "page_number": page_index,
                        "row_count": len(rows),
                        "columns": list(rows[0].values.keys()),
                        "rows": [
                            {
                                "row_index": row.row_index,
                                "values": row.values,
                                "numeric_values": row.numeric_values,
                            }
                            for row in rows
                        ],
                    }
                )

    raw_text = "\n".join(part for part in raw_text_parts if part.strip())
    requested_mode = str(ocr_mode_override or "").strip().lower()
    force_cloud_ocr = requested_mode in {"auto", "cloud", "cloud_only", "google", "google_only", "vision", "vision_only"}
    force_local_ocr = requested_mode in {"local", "local_only", "tesseract", "tesseractjs"}
    is_scanned = len(raw_text.replace("\n", "").strip()) < 80

    top_counter = Counter(top_lines)
    bottom_counter = Counter(bottom_lines)
    header_set = {line for line, count in top_counter.items() if count > 1}
    footer_set = {line for line, count in bottom_counter.items() if count > 1}

    for page_number, lines in page_lines_map.items():
        if not lines:
            continue
        page_header = lines[0] if lines and lines[0] in header_set else ""
        page_footer = lines[-1] if lines and lines[-1] in footer_set else ""
        body_lines = [line for line in lines if line not in {page_header, page_footer}]

        if page_header:
            sections.append(
                PageSection(
                    page_number=page_number,
                    section_type="header",
                    text=page_header,
                    metadata={"detected": "repeated"},
                )
            )
        if body_lines:
            sections.append(
                PageSection(
                    page_number=page_number,
                    section_type="body",
                    text="\n".join(body_lines),
                    metadata={},
                )
            )
        if page_footer:
            sections.append(
                PageSection(
                    page_number=page_number,
                    section_type="footer",
                    text=page_footer,
                    metadata={"detected": "repeated"},
                )
            )

    should_run_ocr = get_settings().ocr_enabled and (is_scanned or force_cloud_ocr or force_local_ocr)
    ocr_segments: list[str] = []
    if should_run_ocr:
        ocr_source = "google_vision" if not force_local_ocr else "google_vision_or_tesseract"
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                ocr_text = _ocr_page_text(
                    page,
                    allow_google=not force_local_ocr,
                    allow_local_fallback=force_local_ocr,
                ).strip()
                if ocr_text:
                    ocr_segments.append(ocr_text)
                    sections.append(
                        PageSection(
                            page_number=page_number,
                            section_type="ocr_body",
                            text=ocr_text,
                            metadata={"source": ocr_source},
                        )
                    )
    if ocr_segments:
        ocr_text = "\n".join(ocr_segments).strip()
        # In cloud OCR mode, prefer Google OCR text over potentially noisy hidden PDF text layers.
        if force_cloud_ocr:
            raw_text = ocr_text
        elif raw_text:
            raw_text += f"\n{ocr_text}"
        else:
            raw_text = ocr_text

    if get_settings().use_unstructured_partition:
        for element in _partition_sections(file_bytes):
            sections.append(element)

    metadata = extract_invoice_metadata(raw_text, filename)

    return ParsedDocument(
        raw_text=raw_text,
        sections=sections,
        tables=tables,
        metadata=metadata,
        is_scanned=is_scanned,
    )

