"""OpenRouter AI client for text extraction, insights, generation, and multimodal vision OCR.

Provides a unified interface using OpenRouter's OpenAI-compatible API.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_last_gemini_error: str = ""


def get_last_gemini_error() -> str:
    return _last_gemini_error


def _get_api_key() -> str:
    settings = get_settings()
    key = settings.gemini_api_key.strip()
    if not key:
        raise RuntimeError("OpenRouter API key is not configured. Set GEMINI_API_KEY in .env")
    return key


def _get_model(model: str | None) -> str:
    settings = get_settings()
    resolved = model or settings.gemini_model or "openrouter/free"
    # Map legacy / paid model names to OpenRouter free router when using free key
    if resolved in ("gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro", "qwen/qwen2.5-vl-72b-instruct", "qwen/qwen2.5-vl-72b-instruct:free"):
        resolved = "openrouter/free"
    if resolved.endswith(":free") and "qwen" in resolved:
        resolved = "openrouter/free"
    return resolved


def gemini_generate(
    prompt: str,
    *,
    system_instruction: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1500,
    response_json: bool = False,
) -> str:
    """Send a text prompt to OpenRouter and return the generated text."""
    if httpx is None:
        raise RuntimeError("httpx is required for OpenRouter API calls")

    api_key = _get_api_key()
    resolved_model = _get_model(model)

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": resolved_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if response_json:
        body["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://safebill.in",
        "X-Title": "SafeBill",
    }

    try:
        response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body, timeout=90)
        if response.status_code == 400 and response_json and "response_format" in body:
            # Retry without response_format in case model doesn't support structured output
            body_fallback = body.copy()
            del body_fallback["response_format"]
            response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body_fallback, timeout=90)
        
        if response.status_code in (402, 403, 429) and resolved_model != "openrouter/free":
            logger.warning("Paid model %s failed with status %s. Falling back to openrouter/free.", resolved_model, response.status_code)
            body_free = body.copy()
            body_free["model"] = "openrouter/free"
            # Some free models might not support response_format: JSON
            if "response_format" in body_free:
                del body_free["response_format"]
            response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body_free, timeout=90)

        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content") or "").strip()
    except Exception as e:
        logger.error("OpenRouter generate failed: %s", str(e))
        raise


def gemini_extract_json(
    prompt: str,
    *,
    system_instruction: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Send a prompt to OpenRouter and parse the response as JSON."""
    raw = gemini_generate(
        prompt,
        system_instruction=system_instruction,
        model=model,
        temperature=0.1,
        max_tokens=1500,
        response_json=True,
    )

    if not raw:
        return {}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        try:
            return json.loads(raw[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse OpenRouter response as JSON: %s", raw[:200])
    return {}


def gemini_chat(
    messages: list[dict[str, str]],
    *,
    system_instruction: str | None = None,
    model: str | None = None,
    temperature: float = 0.4,
    max_tokens: int = 2000,
) -> str:
    """Multi-turn chat with OpenRouter. Messages should have 'role' and 'content' keys."""
    if httpx is None:
        raise RuntimeError("httpx is required for OpenRouter API calls")

    api_key = _get_api_key()
    resolved_model = _get_model(model)

    formatted_messages = []
    if system_instruction:
        formatted_messages.append({"role": "system", "content": system_instruction})

    for msg in messages:
        role = msg.get("role", "user")
        formatted_role = "assistant" if role in ("assistant", "model", "system") else "user"
        formatted_messages.append({"role": formatted_role, "content": msg.get("content", "")})

    body = {
        "model": resolved_model,
        "messages": formatted_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://safebill.in",
        "X-Title": "SafeBill",
    }

    try:
        response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body, timeout=90)
        
        if response.status_code in (402, 403, 429) and resolved_model != "openrouter/free":
            logger.warning("Paid chat model %s failed with status %s. Falling back to openrouter/free.", resolved_model, response.status_code)
            body_free = body.copy()
            body_free["model"] = "openrouter/free"
            response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body_free, timeout=90)

        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content") or "").strip()
    except Exception as e:
        logger.error("OpenRouter chat failed: %s", str(e))
        raise


def gemini_extract_image_metadata(image_bytes: bytes, filename: str) -> dict[str, Any]:
    """Extract structured invoice metadata and full text from image/PDF using OpenRouter."""
    if not image_bytes or httpx is None:
        return {}
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {}

    try:
        api_key = _get_api_key()
    except Exception:
        return {}

    resolved_model = _get_model(None)
    mime_type = mimetypes.guess_type(filename)[0] or "image/png"
    encoded_image = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{encoded_image}"

    system_instruction = (
        "You are an expert Indian invoice and bill data extraction engine. "
        "Your task is to extract structured data from scanned invoices, receipts, bills, and warranty cards. "
        "Return ONLY valid JSON. Do NOT guess or hallucinate values — use null for genuinely missing fields.\n\n"
        "CRITICAL EXTRACTION RULES:\n"
        "1. bill_id: This is the INVOICE NUMBER / BILL NUMBER / RECEIPT NUMBER. "
        "Look at the top for labels like 'Invoice Number # LIAEJYP270000717', 'Invoice No:', 'Bill No:', "
        "'Tax Invoice Number:', 'Order ID:'. Extract the exact alphanumeric code (e.g., 'LIAEJYP270000717'). "
        "NEVER return file names (e.g. 'WhatsApp Image...') or generic text as bill_id.\n"
        "2. vendor: The SELLER / MERCHANT name (e.g., 'MPS Telecom Retail Private Limited', 'Reliance Digital'). "
        "Look for 'Sold By:', 'Seller:', 'From:'. Return ONLY the seller business name. Strip out trailing commas, addresses, and state names.\n"
        "3. date: The INVOICE DATE / BILL DATE in ISO 8601 format (YYYY-MM-DD). "
        "Look for 'Invoice Date:', 'Bill Date:', 'Date:'. Convert any date format to YYYY-MM-DD.\n"
        "4. total_amount: The FINAL GRAND TOTAL / NET PAYABLE AMOUNT AFTER DISCOUNTS AND TAXES (e.g. 1649.00). "
        "Look for 'Grand Total', 'Total', 'Invoice Value'. Do NOT use pre-discount Gross Amount (e.g. 1749.00) or Taxable Value (1397.46).\n"
        "5. vendor_tax_id: The seller's GSTIN (15-character alphanumeric e.g. '10AAJCM4219P1ZC'). "
        "Look for 'GSTIN', 'GST No'.\n"
        "6. taxable_amount: Pre-tax subtotal amount before GST.\n"
        "7. gst_amount: Total GST amount (CGST + SGST or IGST).\n"
        "8. gst_rate: The GST percentage (e.g., 18, 12, 5, 28).\n"
        "9. cgst_amount, sgst_amount, igst_amount: Individual GST component amounts.\n"
        "10. product_name: The PRIMARY product name from the invoice (most expensive or first item). "
        "Include model number if visible (e.g., 'realme Buds T310 with 12.4mm Driver').\n"
        "11. brand: The product brand (e.g., 'Realme', 'Samsung', 'Apple', 'LG').\n"
        "12. serial_number: IMEI, Serial No, or S/N of the product (e.g., '251225922303002372').\n"
        "13. warranty_months: Warranty duration in months (e.g., 12 for 1 year).\n"
        "14. warranty_start, warranty_end: Warranty period dates in YYYY-MM-DD.\n"
        "15. category: Product category (e.g., 'Electronics', 'Audio', 'Mobile').\n"
        "16. line_items: Array of objects with keys: name, quantity, unit_price, amount (amount should be line total after discount).\n"
        "17. full_text: Extract ALL visible text from the document verbatim, preserving layout.\n\n"
        "Return these exact JSON keys: bill_id, vendor, date, total_amount, vendor_tax_id, "
        "taxable_amount, gst_amount, gst_rate, cgst_amount, sgst_amount, igst_amount, "
        "product_name, brand, serial_number, warranty_months, warranty_start, warranty_end, "
        "category, line_items, full_text."
    )

    user_prompt = (
        "Extract all invoice/bill fields and the complete readable text from this document image. "
        "Pay special attention to: (1) the Invoice Number / Bill Number — it is usually near the top, "
        "(2) the Grand Total amount, (3) the seller GSTIN, (4) product details with model numbers. "
        "Keep original formatting for invoice numbers and serial numbers. Use correct decimal amounts."
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://safebill.in",
        "X-Title": "SafeBill",
    }

    body = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]
            }
        ],
        "temperature": 0.1,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"}
    }

    try:
        response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body, timeout=90)
        if response.status_code == 400 and "response_format" in body:
            body_fallback = body.copy()
            del body_fallback["response_format"]
            response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body_fallback, timeout=90)
        
        if response.status_code in (400, 402, 403, 404, 429) and resolved_model != "openrouter/free":
            logger.warning("Paid image extraction model failed with %s. Falling back to openrouter/free.", response.status_code)
            body_free = body.copy()
            body_free["model"] = "openrouter/free"
            if "response_format" in body_free:
                del body_free["response_format"]
            response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body_free, timeout=90)

        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return {}
        raw_text = str(choices[0].get("message", {}).get("content") or "").strip()
        if not raw_text:
            return {}
        try:
            parsed = json.loads(raw_text)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw_text, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group(1))
                except Exception:
                    pass
            return {}
    except Exception as e:
        global _last_gemini_error
        _last_gemini_error = str(e)
        logger.error("OpenRouter image metadata extraction failed: %s", str(e))
        return {}


def gemini_classify_image(image_bytes: bytes, filename: str) -> dict[str, Any]:
    """Classify document type using OpenRouter."""
    if not image_bytes or httpx is None:
        return {}
    if os.getenv("PYTEST_CURRENT_TEST"):
        return {}

    try:
        api_key = _get_api_key()
    except Exception:
        return {}

    resolved_model = _get_model(None)
    mime_type = mimetypes.guess_type(filename)[0] or "image/png"
    encoded_image = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{encoded_image}"

    system_instruction = (
        "You are a document classifier for SafeBill. "
        "Decide whether this image is a bill/invoice/receipt or a warranty/guarantee card. "
        "If the image is a selfie, person, object photo, app screenshot, dashboard, or anything that is not a real document, "
        "set is_invoice to false and document_type to other. "
        "Return only JSON with keys: is_invoice (boolean), document_type (string), confidence (0-1), reason (short). "
        "Acceptable document_type values: invoice, receipt, warranty_card, guarantee_card, other."
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://safebill.in",
        "X-Title": "SafeBill",
    }

    body = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Classify the document type for this image."},
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]
            }
        ],
        "temperature": 0.0,
        "max_tokens": 300,
        "response_format": {"type": "json_object"}
    }

    try:
        response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body, timeout=30)
        if response.status_code == 400 and "response_format" in body:
            body_fallback = body.copy()
            del body_fallback["response_format"]
            response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body_fallback, timeout=30)
            
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return {}
        raw_text = str(choices[0].get("message", {}).get("content") or "").strip()
        if not raw_text:
            return {}
        try:
            parsed = json.loads(raw_text)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    except Exception as e:
        logger.error("OpenRouter image classification failed: %s", str(e))
        return {}


def gemini_ocr_image(image_bytes: bytes, filename: str) -> str:
    """Extract only the raw text from the image/PDF using OpenRouter OCR."""
    if not image_bytes or httpx is None:
        return ""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return ""

    try:
        api_key = _get_api_key()
    except Exception:
        return ""

    resolved_model = _get_model(None)
    mime_type = mimetypes.guess_type(filename)[0] or "image/png"
    encoded_image = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{encoded_image}"

    user_prompt = (
        "Perform OCR on this document. "
        "Extract and return all visible text from this document. "
        "Do not structure or format it as JSON or metadata. Just output the raw text."
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://safebill.in",
        "X-Title": "SafeBill",
    }

    body = {
        "model": resolved_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]
            }
        ],
        "temperature": 0.1,
        "max_tokens": 1500,
    }

    try:
        response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body, timeout=90)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content") or "").strip()
    except Exception as e:
        logger.error("OpenRouter OCR text extraction failed: %s", str(e))
        return ""
