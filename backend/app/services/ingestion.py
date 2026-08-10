from __future__ import annotations

import io
import json
import time
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

try:
    import pandas as pd
except Exception:  # pragma: no cover - optional runtime dependency
    pd = None

from app.core.config import get_settings
from app.models import Chunk, Document
from app.parsers.pdf_parser import ParsedDocument, parse_pdf_document
from app.services.chunking import structure_aware_chunking
from app.services.date_utils import add_months
from app.services.metadata_generator import MetadataGenerator


class IngestionService:
    def __init__(
        self,
        metadata_generator: MetadataGenerator | None = None,
        embedding_service: object | None = None,
        vector_store: object | None = None,
    ) -> None:
        _ = vector_store  # Backward-compatible no-op; Pinecone path removed.
        _ = embedding_service  # Backward-compatible no-op; vectorless retrieval path.
        self.settings = get_settings()
        self.metadata_generator = metadata_generator or MetadataGenerator()

    @staticmethod
    def _coalesce(value: Any, fallback: Any) -> Any:
        return fallback if value is None or value == "" else value

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _filename_stem(filename: str) -> str:
        cleaned = str(filename or "").strip()
        if not cleaned:
            return ""
        stem, _, _suffix = cleaned.rpartition(".")
        return (stem or cleaned).strip()

    @classmethod
    def _should_apply_bill_id_hint(cls, current_value: Any, *, filename: str) -> bool:
        current = str(current_value or "").strip()
        if not current:
            return True
        stem = cls._filename_stem(filename)
        return bool(stem and current.lower() == stem.lower())

    @staticmethod
    def _should_apply_vendor_hint(current_value: Any) -> bool:
        current = str(current_value or "").strip()
        return not current or current.upper() == "UNKNOWN_VENDOR"

    @staticmethod
    def _should_apply_date_hint(current_value: Any) -> bool:
        return current_value in (None, "")

    @classmethod
    def _apply_request_hints(
        cls,
        *,
        filename: str,
        parsed_metadata: dict[str, Any],
        bill_id: str | None,
        vendor: str | None,
        document_date: date | None,
        total_amount: float | None,
    ) -> dict[str, Any]:
        resolved = dict(parsed_metadata)

        bill_id_hint = str(bill_id or "").strip()[:128]
        if bill_id_hint and cls._should_apply_bill_id_hint(resolved.get("bill_id"), filename=filename):
            resolved["bill_id"] = bill_id_hint

        vendor_hint = str(vendor or "").strip()[:255]
        if vendor_hint and cls._should_apply_vendor_hint(resolved.get("vendor")):
            resolved["vendor"] = vendor_hint

        if document_date and cls._should_apply_date_hint(resolved.get("date")):
            resolved["date"] = document_date

        current_total = cls._safe_float(resolved.get("total_amount"))
        if total_amount is not None and (current_total is None or current_total <= 0):
            resolved["total_amount"] = total_amount

        return resolved

    def _extract_line_items_from_tables(self, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        extracted: list[dict[str, Any]] = []
        ignored_names = {"total", "subtotal", "grand total", "tax", "gst", "cgst", "sgst", "igst"}

        for table in tables:
            rows = table.get("rows") if isinstance(table, dict) else None
            if not isinstance(rows, list):
                continue

            for row in rows:
                values = row.get("values") if isinstance(row, dict) else None
                numeric_values = row.get("numeric_values") if isinstance(row, dict) else None
                if not isinstance(values, dict):
                    continue
                if not isinstance(numeric_values, dict):
                    numeric_values = {}

                name_candidates = []
                for key, raw in values.items():
                    text = str(raw or "").strip()
                    if not text or len(text) > 180:
                        continue
                    if self._safe_float(text) is not None:
                        continue
                    lowered_key = str(key).lower()
                    if any(token in lowered_key for token in ("description", "item", "product", "name")):
                        name_candidates.insert(0, text)
                    else:
                        name_candidates.append(text)

                if not name_candidates:
                    continue
                name = name_candidates[0]
                if name.lower() in ignored_names:
                    continue

                amount = None
                quantity = None
                unit_price = None
                gst_amount = None
                for key, numeric in numeric_values.items():
                    key_lower = str(key).lower()
                    number = self._safe_float(numeric)
                    if number is None:
                        continue
                    if any(token in key_lower for token in ("qty", "quantity")):
                        quantity = number
                    elif any(token in key_lower for token in ("rate", "unit", "price", "mrp")):
                        unit_price = number
                    elif any(token in key_lower for token in ("gst", "tax")):
                        gst_amount = max(gst_amount or 0.0, number)
                    elif any(token in key_lower for token in ("amount", "total", "value", "net")):
                        amount = max(amount or 0.0, number)

                if amount is None:
                    numeric_candidates = [self._safe_float(v) for v in numeric_values.values()]
                    numeric_candidates = [value for value in numeric_candidates if value is not None]
                    if numeric_candidates:
                        amount = max(numeric_candidates)

                if amount is None:
                    continue

                extracted.append(
                    {
                        "name": name[:255],
                        "amount": round(amount, 2),
                        "quantity": round(quantity, 3) if quantity is not None else None,
                        "unit_price": round(unit_price, 2) if unit_price is not None else None,
                        "gst_amount": round(gst_amount, 2) if gst_amount is not None else None,
                    }
                )

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, float]] = set()
        for item in extracted:
            key = (str(item.get("name", "")).lower(), float(item.get("amount") or 0.0))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        return deduped[:50]

    def ingest_pdf(
        self,
        db: Session,
        file_bytes: bytes,
        filename: str,
        bill_id: str | None = None,
        vendor: str | None = None,
        document_date: date | None = None,
        total_amount: float | None = None,
        ocr_mode: str | None = None,
        version: int = 1,
        references: dict[str, Any] | None = None,
        parsed: ParsedDocument | None = None,
    ) -> tuple[Document, int]:
        parsed = parsed or parse_pdf_document(
            file_bytes=file_bytes,
            filename=filename,
            ocr_mode_override=ocr_mode,
        )
        chunks = structure_aware_chunking(parsed)
        if len(chunks) > self.settings.max_chunks_per_document:
            raise ValueError("Document exceeds configured chunk limit.")
        parsed_metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
        parsed_metadata = self._apply_request_hints(
            filename=filename,
            parsed_metadata=parsed_metadata,
            bill_id=bill_id,
            vendor=vendor,
            document_date=document_date,
            total_amount=total_amount,
        )

        raw_bill_id = str(parsed_metadata.get("bill_id") or "").strip()
        if not raw_bill_id or any(bad in raw_bill_id.lower() for bad in ("whatsapp image", "screenshot", "img_", "dsc_")):
            resolved_bill_id = f"DOC-{int(time.time() * 1000)}"
        else:
            resolved_bill_id = raw_bill_id[:128]
        requested_version = max(int(version or 1), 1)
        latest_version = db.execute(
            select(Document.version)
            .where(Document.bill_id == resolved_bill_id)
            .order_by(desc(Document.version))
            .limit(1)
        ).scalar_one_or_none()
        resolved_version = requested_version
        if latest_version is not None and latest_version >= requested_version:
            resolved_version = int(latest_version) + 1

        resolved_vendor = str(self._coalesce(parsed_metadata.get("vendor"), None) or "UNKNOWN_VENDOR")[:255]
        resolved_date = self._coalesce(parsed_metadata.get("date"), None)
        resolved_total_amount = self._coalesce(parsed_metadata.get("total_amount"), None)

        default_title = str(parsed_metadata.get("product_name") or filename.rsplit(".", 1)[0] or "Uploaded Document").strip()
        if not default_title:
            default_title = "Uploaded Document"

        references_payload: dict[str, Any] = dict(references or {})
        references_payload.setdefault("filename", filename)
        references_payload.setdefault("source", "pdf")
        references_payload.setdefault("is_scanned", parsed.is_scanned)
        references_payload.setdefault("is_verified", True)
        parsed_raw_text = str(getattr(parsed, "raw_text", "") or "")
        references_payload.setdefault("raw_text", parsed_raw_text[:50000])
        references_payload.setdefault("ocr_confidence", 0.7 if parsed.is_scanned else 1.0)

        incoming_title = str(references_payload.get("title") or "").strip()
        generic_titles = {filename.rsplit(".", 1)[0].strip().lower(), "uploaded document"}
        if not incoming_title or incoming_title.lower() in generic_titles:
            references_payload["title"] = default_title

        if not references_payload.get("product_name"):
            references_payload["product_name"] = default_title
        if not references_payload.get("brand"):
            references_payload["brand"] = str(parsed_metadata.get("brand") or resolved_vendor)

        extracted_category = str(parsed_metadata.get("category") or "Others").strip() or "Others"
        incoming_category = str(references_payload.get("category") or "").strip().lower()
        if not incoming_category or incoming_category == "others":
            references_payload["category"] = extracted_category

        if parsed_metadata.get("vendor_tax_id") and not references_payload.get("vendor_tax_id"):
            references_payload["vendor_tax_id"] = str(parsed_metadata["vendor_tax_id"])
        for tax_key in ("taxable_amount", "gst_amount", "gst_rate", "cgst_amount", "sgst_amount", "igst_amount"):
            if parsed_metadata.get(tax_key) is not None and references_payload.get(tax_key) is None:
                references_payload[tax_key] = parsed_metadata.get(tax_key)
        if parsed_metadata.get("serial_number") and not references_payload.get("serial_number"):
            references_payload["serial_number"] = str(parsed_metadata["serial_number"])

        parsed_tables = parsed.tables if isinstance(getattr(parsed, "tables", None), list) else []
        line_items_from_tables = self._extract_line_items_from_tables(parsed_tables)
        metadata_line_items = parsed_metadata.get("line_items")
        if not line_items_from_tables and isinstance(metadata_line_items, list):
            line_items_from_tables = [item for item in metadata_line_items if isinstance(item, dict)]
        if line_items_from_tables and not references_payload.get("line_items"):
            references_payload["line_items"] = line_items_from_tables

        extracted_warranty_months = parsed_metadata.get("warranty_months")
        if extracted_warranty_months is not None and not references_payload.get("warranty_months"):
            try:
                references_payload["warranty_months"] = int(extracted_warranty_months)
            except (TypeError, ValueError):
                pass
        if not references_payload.get("warranty_months"):
            references_payload["warranty_months"] = 12

        def _to_iso(value: Any) -> str | None:
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, str) and value.strip():
                return value.strip()[:10]
            return None

        warranty_start_iso = _to_iso(parsed_metadata.get("warranty_start")) or _to_iso(resolved_date)
        if warranty_start_iso and not references_payload.get("warranty_start"):
            references_payload["warranty_start"] = warranty_start_iso

        warranty_end_iso = _to_iso(parsed_metadata.get("warranty_end"))
        if not warranty_end_iso and warranty_start_iso:
            try:
                start_date = date.fromisoformat(warranty_start_iso)
                warranty_months = int(references_payload.get("warranty_months") or 12)
                warranty_end_iso = add_months(start_date, warranty_months).isoformat()
            except (TypeError, ValueError):
                warranty_end_iso = None
        if warranty_end_iso and not references_payload.get("warranty_end"):
            references_payload["warranty_end"] = warranty_end_iso

        doc = Document(
            bill_id=resolved_bill_id,
            vendor=resolved_vendor,
            date=resolved_date,
            total_amount=resolved_total_amount,
            version=resolved_version,
            references=references_payload,
        )
        db.add(doc)
        db.flush()

        chunk_records: list[Chunk] = []
        for draft in chunks:
            chunk_id = uuid.uuid4()
            metadata = self.metadata_generator.generate(
                content=draft.content,
                chunk_type=draft.chunk_type,
                document_id=str(doc.id),
                chunk_id=str(chunk_id),
            )
            chunk_records.append(
                Chunk(
                    id=chunk_id,
                    document_id=doc.id,
                    chunk_type=draft.chunk_type,
                    content=draft.content,
                    summary=metadata["summary"],
                    keywords=metadata["keywords"],
                    hypothetical_questions=metadata["hypothetical_questions"],
                    metadata_json=draft.metadata,
                )
            )

        for chunk in chunk_records:
            db.add(chunk)

        db.commit()
        db.refresh(doc)
        return doc, len(chunk_records)

    def ingest_vendor_table(
        self,
        db: Session,
        file_bytes: bytes,
        filename: str,
        version: int = 1,
        source_references: dict[str, Any] | None = None,
    ) -> tuple[list[Document], int]:
        if pd is None:
            raise ImportError("pandas is required for vendor table ingestion.")
        if filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(file_bytes))
        elif filename.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(file_bytes))
        else:
            raise ValueError("Unsupported tabular format. Use CSV/XLSX/XLS.")

        created_docs: list[Document] = []
        row_count = 0
        for index, row in df.fillna("").iterrows():
            row_count += 1
            row_dict = {str(k): (v.item() if hasattr(v, "item") else v) for k, v in row.to_dict().items()}
            bill_id = str(row_dict.get("bill_id") or row_dict.get("invoice_no") or f"{filename}-{index+1}")
            vendor = str(row_dict.get("vendor") or row_dict.get("merchant") or "UNKNOWN_VENDOR")
            total_amount = row_dict.get("amount") or row_dict.get("total_amount") or None
            doc_date_raw = row_dict.get("date") or row_dict.get("invoice_date")
            try:
                doc_date = pd.to_datetime(doc_date_raw).date() if doc_date_raw else None
            except Exception:
                doc_date = None

            doc = Document(
                bill_id=bill_id,
                vendor=vendor[:255],
                date=doc_date,
                total_amount=float(total_amount) if total_amount not in (None, "") else None,
                version=version,
                references={
                    "filename": filename,
                    "source": "vendor_table",
                    "row_number": index + 1,
                    **(source_references or {}),
                },
            )
            db.add(doc)
            db.flush()

            content = json.dumps(row_dict, ensure_ascii=True)
            metadata = self.metadata_generator.generate(
                content=content,
                chunk_type="vendor_table_row",
                document_id=str(doc.id),
                chunk_id=str(uuid.uuid4()),
            )
            chunk = Chunk(
                id=uuid.uuid4(),
                document_id=doc.id,
                chunk_type="vendor_table_row",
                content=content,
                summary=metadata["summary"],
                keywords=metadata["keywords"],
                hypothetical_questions=metadata["hypothetical_questions"],
                metadata_json={"row_number": index + 1},
            )
            db.add(chunk)
            created_docs.append(doc)

        db.commit()
        return created_docs, row_count
