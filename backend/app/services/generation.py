from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.config import get_settings
from app.services.planner import Plan
from app.services.retrieval import RetrievalHit

logger = logging.getLogger(__name__)


class GroundedAnswerGenerator:
    def __init__(self) -> None:
        settings = get_settings()
        self.aws_only_mode = settings.aws_only_mode

    @staticmethod
    def _context_block(hits: list[RetrievalHit]) -> str:
        lines: list[str] = []
        for hit in hits:
            lines.append(
                "\n".join(
                    [
                        f"chunk_id: {hit.chunk_id}",
                        f"bill_id: {hit.bill_id}",
                        f"vendor: {hit.vendor}",
                        f"chunk_type: {hit.chunk_type}",
                        f"score: {hit.score:.4f}",
                        f"content: {hit.content[:1400]}",
                    ]
                )
            )
        return "\n\n---\n\n".join(lines)

    @staticmethod
    def _fallback_answer(query: str, hits: list[RetrievalHit], calculations: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        snippets = [f"[{hit.chunk_id}] {hit.summary or hit.content[:180]}" for hit in hits[:5]]
        answer = "Grounded response based on retrieved chunks:\n" + "\n".join(snippets)
        if calculations.get("gst_anomalies"):
            answer += f"\nDetected GST anomalies: {len(calculations['gst_anomalies'])}"
        if policy.get("missing_vendor_tax_ids"):
            answer += f"\nMissing vendor tax IDs: {len(policy['missing_vendor_tax_ids'])}"

        claims = [{"text": snippet, "citations": [str(hits[idx].chunk_id)]} for idx, snippet in enumerate(snippets[: len(hits)])]
        return {
            "answer": answer,
            "claims": claims,
            "citation_chunk_ids": [str(hit.chunk_id) for hit in hits[:8]],
            "numeric_claims": [{"metric": "document_total_sum", "value": calculations.get("document_total_sum")}],
        }

    def generate(
        self,
        query: str,
        plan: Plan,
        hits: list[RetrievalHit],
        calculations: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        if not hits:
            return {
                "answer": "No relevant grounded records were found for the query.",
                "claims": [],
                "citation_chunk_ids": [],
                "numeric_claims": [],
            }

        # Try OpenRouter-based generation first
        try:
            return self._generate_with_openrouter(query, plan, hits, calculations, policy)
        except Exception as exc:
            logger.warning("OpenRouter generation failed (%s), using fallback.", exc)
            return self._fallback_answer(query, hits, calculations, policy)

    def _generate_with_openrouter(
        self,
        query: str,
        plan: Plan,
        hits: list[RetrievalHit],
        calculations: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        from app.services.gemini_client import gemini_generate

        system_instruction = (
            "You are SafeBill AI — an expert warranty and invoice assistant. "
            "Your job is to answer questions about invoices, warranties, purchases, and bills "
            "using ONLY the provided document context. "
            "Rules:\n"
            "1. Answer in a clear, professional, and helpful tone.\n"
            "2. Use the actual data from the document chunks provided below.\n"
            "3. If the user asks about warranty status, calculate days remaining from today.\n"
            "4. If the user asks about amounts, taxes (GST/CGST/SGST), totals — cite exact numbers.\n"
            "5. If the user asks a general question (like 'summarize'), give a concise overview.\n"
            "6. Never invent facts not present in the context.\n"
            "7. If information is missing, say so clearly.\n"
            "8. Format your answer nicely with bullet points where appropriate.\n\n"
            "Return your response as JSON with these keys:\n"
            '- "answer": string (your full helpful answer)\n'
            '- "claims": list of {"text": string, "citations": [chunk_id strings]}\n'
            '- "citation_chunk_ids": list of chunk_id strings you referenced\n'
            '- "numeric_claims": list of {"metric": string, "value": number or null}\n'
        )

        context_text = self._context_block(hits)

        user_message = (
            f"USER QUESTION: {query}\n\n"
            f"PLAN: complexity={plan.complexity}, steps={[step.name for step in plan.steps]}\n\n"
            f"CALCULATIONS: {json.dumps(calculations, default=str)}\n\n"
            f"POLICY FINDINGS: {json.dumps(policy, default=str)}\n\n"
            f"DOCUMENT CONTEXT:\n{context_text}"
        )

        raw_response = gemini_generate(
            user_message,
            system_instruction=system_instruction,
            temperature=0.15,
            max_tokens=1500,
            response_json=True,
        )

        if not raw_response:
            return self._fallback_answer(query, hits, calculations, policy)

        # Parse JSON response
        try:
            payload = json.loads(raw_response)
        except json.JSONDecodeError:
            # Try extracting JSON from markdown code block
            json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw_response, re.DOTALL)
            if json_match:
                try:
                    payload = json.loads(json_match.group(1))
                except json.JSONDecodeError:
                    payload = None
            else:
                # Try finding raw JSON object
                brace_start = raw_response.find("{")
                brace_end = raw_response.rfind("}")
                if brace_start >= 0 and brace_end > brace_start:
                    try:
                        payload = json.loads(raw_response[brace_start : brace_end + 1])
                    except json.JSONDecodeError:
                        payload = None
                else:
                    payload = None

        if not payload or not isinstance(payload, dict):
            # If JSON parsing completely fails, use raw text as the answer
            return {
                "answer": raw_response,
                "claims": [],
                "citation_chunk_ids": [str(hit.chunk_id) for hit in hits[:8]],
                "numeric_claims": [],
            }

        payload.setdefault("answer", "")
        payload.setdefault("claims", [])
        payload.setdefault("citation_chunk_ids", [str(hit.chunk_id) for hit in hits[:8]])
        payload.setdefault("numeric_claims", [])
        return payload

