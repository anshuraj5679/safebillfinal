from __future__ import annotations

import base64
import json
import re
from typing import Any

try:
    import boto3
except Exception:  # pragma: no cover - optional runtime dependency
    boto3 = None  # type: ignore[assignment]

from app.core.config import get_settings
from app.services.bedrock_client import configure_bedrock_api_key


class BharatAIService:
    def __init__(self) -> None:
        settings = get_settings()
        self.region = settings.aws_region
        self.model = settings.bedrock_chat_model
        self.bedrock = None
        self.translate = None
        self.comprehend = None
        self.polly = None
        if boto3:
            try:
                configure_bedrock_api_key(settings)
                self.bedrock = boto3.client("bedrock-runtime", region_name=self.region)
            except Exception:
                self.bedrock = None
            try:
                self.translate = boto3.client("translate", region_name=self.region)
            except Exception:
                self.translate = None
            try:
                self.comprehend = boto3.client("comprehend", region_name=self.region)
            except Exception:
                self.comprehend = None
            try:
                self.polly = boto3.client("polly", region_name=self.region)
            except Exception:
                self.polly = None

    @staticmethod
    def extract_payment_references(text: str) -> list[str]:
        content = (text or "").strip()
        if not content:
            return []
        patterns = [
            r"(?i)\bUPI(?:\s*Ref(?:erence)?|Txn(?:\s*Id)?)?\s*[:\-]?\s*([A-Z0-9]{8,30})\b",
            r"(?i)\bUTR\s*[:\-]?\s*([A-Z0-9]{8,30})\b",
            r"(?i)\bRRN\s*[:\-]?\s*([0-9]{8,30})\b",
            r"(?i)\b(?:txn|transaction)\s*(?:id|no|number)\s*[:\-]?\s*([A-Z0-9]{8,40})\b",
        ]
        refs: list[str] = []
        for pattern in patterns:
            refs.extend(match.upper() for match in re.findall(pattern, content))
        deduped: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            if ref in seen:
                continue
            seen.add(ref)
            deduped.append(ref)
        return deduped[:25]

    @staticmethod
    def _normalize_list(value: Any) -> list[str]:
        if isinstance(value, list):
            result = [str(item).strip() for item in value if str(item).strip()]
            return result
        if isinstance(value, str):
            parts = re.split(r"[|\n;]", value)
            result = [part.strip() for part in parts if part.strip()]
            return result
        return []

    @staticmethod
    def _first_match(patterns: list[str], text: str) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = str(match.group(1) or "").strip()
                if value:
                    return value
        return None

    @staticmethod
    def _to_money(value: str) -> str:
        cleaned = re.sub(r"[^0-9.]", "", str(value or ""))
        if not cleaned:
            return ""
        try:
            numeric = float(cleaned)
        except Exception:
            return cleaned
        return f"{numeric:,.2f}"

    @staticmethod
    def _metadata_text(metadata: dict[str, Any], *keys: str) -> str:
        for key in keys:
            raw = metadata.get(key)
            if raw is None:
                continue
            value = str(raw).strip()
            if value:
                return value
        return ""

    def _fallback_insights(
        self,
        *,
        text: str,
        metadata: dict[str, Any],
        payment_references: list[str],
    ) -> dict[str, list[str]]:
        gstin = self._first_match(
            [r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9])\b"],
            text.upper(),
        )
        invoice_no = self._metadata_text(metadata, "bill_id", "invoice_no", "invoice_number")
        if not invoice_no:
            invoice_no = self._first_match(
                [
                    r"\b(?:invoice|bill|document)\s*(?:no|number|id|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-\/]{4,40})\b",
                    r"\b(INV[-\/]?[A-Z0-9\-]{3,30})\b",
                ],
                text,
            ) or ""
        vendor = self._metadata_text(metadata, "vendor", "seller", "store")
        amount = self._metadata_text(metadata, "total_amount", "amount", "purchase_price")
        if not amount:
            amount = self._first_match(
                [
                    r"\b(?:grand\s*total|total(?:\s*amount)?|amount)\s*[:\-]?\s*(?:rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b",
                ],
                text,
            ) or ""
        purchase_date = self._metadata_text(metadata, "purchase_date", "purchaseDate", "date")
        if not purchase_date:
            purchase_date = self._first_match(
                [
                    r"\b(?:invoice\s*date|bill\s*date|purchase\s*date|date)\s*[:\-]?\s*([0-3]?\d[\/\-.][01]?\d[\/\-.](?:19|20)\d{2})\b",
                    r"\b((?:19|20)\d{2}[\/\-.][01]?\d[\/\-.][0-3]?\d)\b",
                ],
                text,
            ) or ""

        gst_findings: list[str] = []
        if gstin:
            gst_findings.append(f"GSTIN detected: {gstin}.")
        if re.search(r"\bIRN\b", text, re.IGNORECASE):
            gst_findings.append("IRN/e-invoice reference detected in OCR text.")
        elif gstin:
            gst_findings.append("IRN not clearly detected in OCR text; verify e-invoice applicability.")
        if amount:
            gst_findings.append(f"Invoice amount captured: Rs {self._to_money(amount)}.")
        if not gst_findings:
            gst_findings.append("Run manual GST validation before claim filing.")

        fraud_signals: list[str] = []
        if len((text or "").strip()) < 180:
            fraud_signals.append("Low OCR text volume; compare mapped fields against original invoice.")
        if not invoice_no:
            fraud_signals.append("Invoice number missing in mapped fields.")
        if not vendor:
            fraud_signals.append("Seller/vendor missing in mapped fields.")
        if not amount:
            fraud_signals.append("Total amount missing in mapped fields.")

        claim_steps: list[str] = []
        if invoice_no or vendor:
            claim_steps.append(
                f"Verify invoice details ({invoice_no or 'invoice no'}, {vendor or 'seller'}) against the original bill."
            )
        if purchase_date:
            claim_steps.append(f"Confirm purchase date ({purchase_date}) for warranty timeline.")
        if payment_references:
            claim_steps.append(f"Attach payment proof reference: {payment_references[0]}.")
        else:
            claim_steps.append("Attach payment proof (UPI/SMS/bank statement) in claim packet.")
        claim_steps.append("Keep invoice PDF, serial number photo, and issue photos ready before service visit.")

        merchant_notes: list[str] = []
        if vendor:
            merchant_notes.append(f"Seller mapped as {vendor}.")
        if invoice_no:
            merchant_notes.append(f"Invoice number mapped as {invoice_no}.")
        if purchase_date:
            merchant_notes.append(f"Purchase date captured as {purchase_date}.")
        if not merchant_notes:
            merchant_notes.append("Mapped fields are limited; add manual verification notes.")

        return {
            "gst_findings": gst_findings[:8],
            "fraud_signals": fraud_signals[:8],
            "claim_steps": claim_steps[:8],
            "merchant_notes": merchant_notes[:8],
        }

    def _build_consumer_summary(
        self,
        *,
        metadata: dict[str, Any],
        insights: dict[str, list[str]],
    ) -> str:
        product = self._metadata_text(metadata, "product_name", "title")
        vendor = self._metadata_text(metadata, "vendor", "seller", "store")
        bill_id = self._metadata_text(metadata, "bill_id", "invoice_no", "invoice_number")
        amount = self._metadata_text(metadata, "total_amount", "amount", "purchase_price")
        purchase_date = self._metadata_text(metadata, "purchase_date", "purchaseDate", "date")

        leading_bits = [bit for bit in [product, vendor] if bit]
        if leading_bits:
            summary = f"This invoice appears to be for {' from '.join(leading_bits[:2])}."
        elif bill_id:
            summary = f"Invoice {bill_id} was captured from OCR."
        else:
            summary = "This invoice was captured from OCR."

        detail_bits: list[str] = []
        if bill_id:
            detail_bits.append(f"Invoice no: {bill_id}")
        if amount:
            detail_bits.append(f"Amount: Rs {self._to_money(amount)}")
        if purchase_date:
            detail_bits.append(f"Purchase date: {purchase_date}")
        if detail_bits:
            summary = f"{summary} {' | '.join(detail_bits)}."

        if insights.get("fraud_signals"):
            summary = f"{summary} Review extracted details before filing a claim."
        elif insights.get("claim_steps"):
            summary = f"{summary} Claim guidance is ready."

        return summary.strip()

    def detect_language(self, text: str) -> str:
        content = (text or "").strip()
        if not content:
            return "en"
        if self.comprehend:
            try:
                response = self.comprehend.detect_dominant_language(Text=content[:5000])
                languages = response.get("Languages", [])
                if isinstance(languages, list) and languages:
                    top = max(
                        [entry for entry in languages if isinstance(entry, dict)],
                        key=lambda entry: float(entry.get("Score") or 0.0),
                    )
                    code = str(top.get("LanguageCode") or "").strip().lower()
                    if code:
                        return code
            except Exception:
                pass
        # Lightweight fallback for non-Latin scripts.
        if re.search(r"[\u0900-\u097f]", content):
            return "hi"
        return "en"

    def translate_text(
        self,
        text: str,
        *,
        target_language_code: str,
        source_language_code: str = "auto",
    ) -> str:
        content = (text or "").strip()
        target = str(target_language_code or "").strip().lower()
        source = str(source_language_code or "auto").strip().lower()
        if not content or not target:
            return content
        if source != "auto" and source == target:
            return content
        if self.translate:
            try:
                response = self.translate.translate_text(
                    Text=content[:5000],
                    SourceLanguageCode=source,
                    TargetLanguageCode=target,
                )
                translated = str(response.get("TranslatedText") or "").strip()
                if translated:
                    return translated
            except Exception:
                pass
        settings = get_settings()
        if settings.ai_provider == "gemini" or (self.bedrock is not None and self.model):
            fallback = self._converse_json(
                system_prompt=(
                    "You are a precise translation engine for Indian languages. "
                    "Return strict JSON with key translated_text only. "
                    "Preserve numbers, product IDs, invoice IDs, and currency values exactly."
                ),
                payload={
                    "text": content[:5000],
                    "source_language_code": source,
                    "target_language_code": target,
                },
                max_tokens=1200,
            )
            translated = str(fallback.get("translated_text") or "").strip()
            if translated:
                return translated
        return content

    def translate_many(
        self,
        texts: list[str],
        *,
        target_language_code: str,
        source_language_code: str = "auto",
    ) -> list[str]:
        cleaned_texts = [str(text or "").strip() for text in texts]
        target = str(target_language_code or "").strip().lower()
        source = str(source_language_code or "auto").strip().lower()
        if not cleaned_texts or not target:
            return cleaned_texts
        if source != "auto" and source == target:
            return cleaned_texts

        translated_cache: dict[str, str] = {}
        results: list[str] = []
        for text in cleaned_texts:
            if not text:
                results.append("")
                continue
            if text not in translated_cache:
                translated_cache[text] = self.translate_text(
                    text,
                    target_language_code=target,
                    source_language_code=source,
                )
            results.append(translated_cache[text])
        return results

    def _converse_json(
        self,
        *,
        system_prompt: str,
        payload: dict[str, Any],
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.ai_provider == "gemini" or self.bedrock is None:
            try:
                from app.services.gemini_client import gemini_extract_json
                prompt = json.dumps(payload, default=str)
                return gemini_extract_json(
                    prompt=prompt,
                    system_instruction=system_prompt,
                )
            except Exception:
                return {}

        if self.bedrock is None or not self.model:
            return {}
        try:
            response = self.bedrock.converse(
                modelId=self.model,
                system=[{"text": system_prompt}],
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": json.dumps(payload, default=str)}],
                    }
                ],
                inferenceConfig={"temperature": 0.0, "maxTokens": max_tokens},
            )
            content_blocks = (
                response.get("output", {})
                .get("message", {})
                .get("content", [])
            )
            response_text = "".join(
                str(block.get("text", ""))
                for block in content_blocks
                if isinstance(block, dict)
            ).strip()
            if response_text.startswith("```"):
                response_text = response_text.strip("`")
                response_text = response_text.replace("json\n", "", 1).strip()
            parsed = json.loads(response_text or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def synthesize_speech(
        self,
        *,
        text: str,
        language_code: str = "en-IN",
        voice_id: str = "Aditi",
    ) -> tuple[str | None, str | None]:
        content = (text or "").strip()
        if not content or self.polly is None:
            return None, None
        try:
            response = self.polly.synthesize_speech(
                Text=content[:1500],
                OutputFormat="mp3",
                VoiceId=voice_id,
                LanguageCode=language_code,
                Engine="neural",
            )
            stream = response.get("AudioStream")
            if stream is None:
                return None, None
            audio_bytes = stream.read()
            if not audio_bytes:
                return None, None
            return base64.b64encode(audio_bytes).decode("ascii"), "audio/mpeg"
        except Exception:
            return None, None

    def enrich_invoice_for_bharat(
        self,
        *,
        ocr_text: str,
        metadata: dict[str, Any] | None = None,
        target_language_code: str = "en",
        include_speech: bool = False,
    ) -> dict[str, Any]:
        source_language_code = self.detect_language(ocr_text)
        normalized_text = (ocr_text or "").strip()
        english_text = normalized_text
        if source_language_code not in {"en", "en-us", "en-in"}:
            english_text = self.translate_text(
                normalized_text,
                target_language_code="en",
                source_language_code=source_language_code,
            )

        prompt = (
            "You are an India-focused invoice intelligence copilot. "
            "Return strict JSON with keys: consumer_summary, gst_findings, fraud_signals, claim_steps, merchant_notes. "
            "consumer_summary must be a plain-language user-facing summary in 1 or 2 short sentences. "
            "Each list should be concise and grounded in provided text. "
            "Do not invent details. If missing, return empty list or empty string."
        )
        ai_payload = self._converse_json(
            system_prompt=prompt,
            payload={
                "ocr_text": english_text[:18000],
                "metadata": metadata or {},
            },
        )

        consumer_summary = str(ai_payload.get("consumer_summary") or "").strip()
        gst_findings = self._normalize_list(ai_payload.get("gst_findings"))[:8]
        fraud_signals = self._normalize_list(ai_payload.get("fraud_signals"))[:8]
        claim_steps = self._normalize_list(ai_payload.get("claim_steps"))[:8]
        merchant_notes = self._normalize_list(ai_payload.get("merchant_notes"))[:8]
        payment_references = self.extract_payment_references(ocr_text)

        fallback_insights = self._fallback_insights(
            text=english_text,
            metadata=metadata or {},
            payment_references=payment_references,
        )
        if not gst_findings:
            gst_findings = fallback_insights["gst_findings"]
        if not fraud_signals:
            fraud_signals = fallback_insights["fraud_signals"]
        if not claim_steps:
            claim_steps = fallback_insights["claim_steps"]
        if not merchant_notes:
            merchant_notes = fallback_insights["merchant_notes"]
        if not consumer_summary:
            consumer_summary = self._build_consumer_summary(
                metadata=metadata or {},
                insights={
                    "fraud_signals": fraud_signals,
                    "claim_steps": claim_steps,
                },
            )

        localized_summary = consumer_summary
        target = str(target_language_code or "en").strip().lower()
        if target and target not in {"en", "en-us", "en-in"}:
            localized_summary = self.translate_text(
                consumer_summary,
                target_language_code=target,
                source_language_code="en",
            )
            gst_findings = self.translate_many(
                gst_findings,
                target_language_code=target,
                source_language_code="en",
            )
            fraud_signals = self.translate_many(
                fraud_signals,
                target_language_code=target,
                source_language_code="en",
            )
            claim_steps = self.translate_many(
                claim_steps,
                target_language_code=target,
                source_language_code="en",
            )
            merchant_notes = self.translate_many(
                merchant_notes,
                target_language_code=target,
                source_language_code="en",
            )

        speech_audio_base64: str | None = None
        speech_content_type: str | None = None
        if include_speech:
            language_map = {
                "hi": ("hi-IN", "Aditi"),
                "ta": ("ta-IN", "Aditi"),
                "te": ("te-IN", "Aditi"),
                "bn": ("en-IN", "Aditi"),
                "mr": ("en-IN", "Aditi"),
                "gu": ("en-IN", "Aditi"),
                "kn": ("kn-IN", "Kajal"),
                "ml": ("en-IN", "Aditi"),
            }
            speech_lang, voice = language_map.get(target, ("en-IN", "Aditi"))
            speech_audio_base64, speech_content_type = self.synthesize_speech(
                text=localized_summary,
                language_code=speech_lang,
                voice_id=voice,
            )

        return {
            "source_language_code": source_language_code,
            "target_language_code": target_language_code,
            "normalized_text": english_text,
            "localized_summary": localized_summary,
            "consumer_summary": consumer_summary,
            "gst_findings": gst_findings,
            "fraud_signals": fraud_signals,
            "claim_steps": claim_steps,
            "merchant_notes": merchant_notes,
            "payment_references": payment_references,
            "model_used": self.model or None,
            "speech_audio_base64": speech_audio_base64,
            "speech_content_type": speech_content_type,
        }

    def answer_invoice_question(
        self,
        *,
        question: str,
        ocr_text: str,
        metadata: dict[str, Any] | None = None,
        target_language_code: str = "en",
    ) -> dict[str, Any]:
        raw_question = str(question or "").strip()
        normalized_text = str(ocr_text or "").strip()
        details = metadata or {}
        question_language_code = self.detect_language(raw_question)
        english_question = raw_question
        if question_language_code not in {"en", "en-us", "en-in"}:
            english_question = self.translate_text(
                raw_question,
                target_language_code="en",
                source_language_code=question_language_code,
            )

        ocr_language_code = self.detect_language(normalized_text)
        english_text = normalized_text
        if normalized_text and ocr_language_code not in {"en", "en-us", "en-in"}:
            english_text = self.translate_text(
                normalized_text,
                target_language_code="en",
                source_language_code=ocr_language_code,
            )

        payload = self._converse_json(
            system_prompt=(
                "You are an invoice and warranty assistant for Indian consumers. "
                "Answer ONLY from the OCR text and metadata provided. "
                "Do not infer facts that are not visible in the invoice. "
                "If the answer is missing from the invoice, state that clearly. "
                "Return strict JSON with keys: answer, support_points, missing_information, confidence_note. "
                "answer must be concise, user-facing, and factual. "
                "support_points and missing_information must be short bullet-ready strings."
            ),
            payload={
                "question": english_question[:3000],
                "ocr_text": english_text[:18000],
                "metadata": details,
            },
            max_tokens=1400,
        )

        answer = str(payload.get("answer") or "").strip()
        support_points = self._normalize_list(payload.get("support_points"))[:6]
        missing_information = self._normalize_list(payload.get("missing_information"))[:6]
        confidence_note = str(payload.get("confidence_note") or "").strip()

        if not answer:
            product = self._metadata_text(details, "product_name", "title")
            vendor = self._metadata_text(details, "vendor", "seller", "store")
            bill_id = self._metadata_text(details, "bill_id", "invoice_no", "invoice_number")
            amount = self._metadata_text(details, "total_amount", "amount", "purchase_price")
            purchase_date = self._metadata_text(details, "purchase_date", "purchaseDate", "date")
            warranty_end = self._metadata_text(details, "warranty_end", "warrantyEnd")
            summary_bits = [bit for bit in [product, vendor] if bit]
            if summary_bits:
                answer = f"This invoice is for {' from '.join(summary_bits[:2])}."
            elif bill_id:
                answer = f"This invoice record is grounded on bill {bill_id}."
            else:
                answer = "I could only confirm details that are present in this invoice."

            fallback_support = [
                f"Invoice number: {bill_id}" if bill_id else "",
                f"Purchase date: {purchase_date}" if purchase_date else "",
                f"Amount: Rs {self._to_money(amount)}" if amount else "",
                f"Warranty end: {warranty_end}" if warranty_end else "",
            ]
            support_points = [item for item in fallback_support if item][:4]
            if not missing_information:
                missing_information = ["Serial number or issue details are not clearly visible in this invoice."]
            if not confidence_note:
                confidence_note = "This answer is limited to the fields visible in the uploaded invoice."

        target = str(target_language_code or "en").strip().lower()
        localized_question = raw_question
        localized_answer = answer
        localized_support_points = support_points
        localized_missing_information = missing_information
        localized_confidence_note = confidence_note
        if target and target not in {"en", "en-us", "en-in"}:
            localized_answer = self.translate_text(
                answer,
                target_language_code=target,
                source_language_code="en",
            )
            localized_support_points = self.translate_many(
                support_points,
                target_language_code=target,
                source_language_code="en",
            )
            localized_missing_information = self.translate_many(
                missing_information,
                target_language_code=target,
                source_language_code="en",
            )
            localized_confidence_note = self.translate_text(
                confidence_note,
                target_language_code=target,
                source_language_code="en",
            )
            if question_language_code == "en":
                localized_question = self.translate_text(
                    raw_question,
                    target_language_code=target,
                    source_language_code="en",
                )

        return {
            "source_language_code": question_language_code,
            "target_language_code": target_language_code,
            "normalized_question": english_question,
            "localized_question": localized_question,
            "answer": localized_answer,
            "support_points": localized_support_points,
            "missing_information": localized_missing_information,
            "confidence_note": localized_confidence_note,
            "model_used": self.model or None,
        }
