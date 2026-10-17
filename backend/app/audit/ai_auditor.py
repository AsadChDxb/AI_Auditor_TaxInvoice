"""The agentic audit service (Phase 5).

:class:`AiAuditor` is the only entry point an API layer needs:

* it is independent of FastAPI (no request objects, no HTTP errors),
* it runs the LangGraph audit engine for one stored document, and
* it stores the latest report per document number in memory.

The service never asserts fraud or definitive legal non-compliance: findings are
review aids, deterministic arithmetic is authoritative, and anything uncertain is
routed to a human. Secrets are never stored on a report.
"""

from threading import Lock
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver

from app.audit.graph import (
    RECURSION_LIMIT,
    build_audit_graph,
)
from app.audit.nodes import MAX_TOOL_ROUNDS
from app.audit.rules_config import get_rules_config
from app.audit.state import AuditState, initial_state
from app.config import LLMSettings, get_audit_settings, get_llm_settings
from app.models.audit import (
    AuditFinding,
    AuditReport,
    AuditTrace,
    RulesConfig,
    summarise_findings,
)
from app.services.llm import LLMExtractor
from app.storage.memory import DocumentRecord, document_registry

CARRIED_OVER_MESSAGE = (
    "This report is a review aid produced by deterministic checks and a language "
    "model. It does not assert fraud or definitive legal non-compliance; check the "
    "cited evidence and the current FTA guidance before acting on it."
)

NOT_AUDITED_MESSAGE = "No audit was performed for this document."


class AuditError(RuntimeError):
    """Base class for audit-service failures."""


class UnknownDocumentError(AuditError):
    """No document is stored for the requested document number."""


class DocumentNotAuditableError(AuditError):
    """The stored document cannot be audited (needs OCR, or has no extracted fields)."""


class AuditReportStore:
    """Thread-safe, process-local store of the latest report per document number.

    Repeated audits of the same document overwrite the previous report: the store
    always holds the most recent result, because an audit is a snapshot of the
    document, the ruleset version and the model that produced it.
    """

    def __init__(self) -> None:
        self._reports: dict[str, AuditReport] = {}
        self._lock = Lock()

    @staticmethod
    def _key(document_no: str) -> str:
        return document_no.strip().upper()

    def set(self, report: AuditReport) -> AuditReport:
        """Store `report` as the latest report for its document number."""

        with self._lock:
            self._reports[self._key(report.document_no)] = report
        return report

    def get(self, document_no: str) -> AuditReport | None:
        """The latest report for `document_no`, or None when it was never audited."""

        with self._lock:
            return self._reports.get(self._key(document_no))

    def clear(self) -> None:
        """Remove every stored report (tests and manual resets)."""

        with self._lock:
            self._reports.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._reports)


report_store = AuditReportStore()


class AiAuditor:
    """Run the agentic audit engine and store the resulting report."""

    def __init__(
        self,
        *,
        extractor: Any = None,
        chat_model: Any = None,
        settings: LLMSettings | None = None,
        rules: RulesConfig | None = None,
        max_iterations: int | None = None,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
        store: AuditReportStore | None = None,
        provider_label: str | None = None,
        checkpointer: Any = None,
    ) -> None:
        """Create the auditor.

        Args:
            extractor: Invoice extractor used for the bounded re-extraction pass
                (defaults to :class:`~app.services.llm.LLMExtractor`).
            chat_model: Chat model for the audit review; built from settings when
                omitted. Tests inject a scripted fake, so no network call is made.
            settings: LLM settings; defaults to the cached `.env`/env settings.
            rules: Ruleset to audit against; defaults to the configured ruleset.
            max_iterations: Bound on model/re-extraction passes.
            max_tool_rounds: Bound on tool-calling rounds per model invocation.
            store: Report store; defaults to the process-wide store.
            provider_label: Label recorded on reports; defaults to ``LLM_PROVIDER_LABEL``.
            checkpointer: LangGraph checkpointer; defaults to an in-memory saver.
        """

        self._extractor = extractor
        self._chat_model = chat_model
        self._settings = settings
        self._rules = rules
        self._max_iterations = max_iterations
        self._max_tool_rounds = max_tool_rounds
        self._store = store or report_store
        self._provider_label = provider_label
        self._checkpointer = checkpointer

    # --- public API --------------------------------------------------------------

    def audit(self, document_no: str) -> AuditReport:
        """Audit a stored document and store the report.

        Raises:
            UnknownDocumentError: no document is stored for `document_no`.
            DocumentNotAuditableError: the document needs OCR, or its invoice fields
                have not been extracted yet.
        """

        record = document_registry.get(document_no)
        if record is None:
            raise UnknownDocumentError(
                f"No document is stored for '{document_no}'."
            )
        report = self.audit_record(record)
        return self._store.set(report)

    def latest(self, document_no: str) -> AuditReport | None:
        """The most recent stored report for `document_no`, or None."""

        return self._store.get(document_no)

    def remember(self, report: AuditReport) -> AuditReport:
        """Store `report` as the latest report for its document number."""

        return self._store.set(report)

    def audit_record(self, record: DocumentRecord) -> AuditReport:
        """Audit one stored record without storing the report."""

        self._ensure_auditable(record)
        rules = self._resolve_rules()
        settings = self._resolve_settings()
        graph = build_audit_graph(
            record,
            extractor=self._resolve_extractor(),
            chat_model=self._chat_model,
            settings=settings,
            rules=rules,
            checkpointer=self._checkpointer or MemorySaver(),
            max_iterations=self._max_iterations
            or get_audit_settings().agent_max_iterations,
            max_tool_rounds=self._max_tool_rounds,
        )
        invoice = record.invoice
        state = initial_state(
            document_no=record.document_no,
            page_count=record.page_count,
            invoice=invoice.model_dump(mode="json") if invoice else None,
            missing_fields=list(invoice.missing_fields) if invoice else [],
        )
        final: AuditState = graph.invoke(
            state,
            config={
                "configurable": {
                    "thread_id": f"{record.document_no}:{uuid4().hex}",
                },
                "recursion_limit": RECURSION_LIMIT,
            },
        )
        return self._build_report(
            record=record, state=final, rules=rules, settings=settings
        )

    # --- internals ---------------------------------------------------------------

    def _resolve_extractor(self) -> Any:
        if self._extractor is None:
            self._extractor = LLMExtractor(chat_model=self._chat_model)
        return self._extractor

    def _resolve_settings(self) -> LLMSettings:
        if self._settings is None:
            self._settings = get_llm_settings()
        return self._settings

    def _resolve_rules(self) -> RulesConfig:
        if self._rules is None:
            self._rules = get_rules_config()
        return self._rules

    def _ensure_auditable(self, record: DocumentRecord) -> None:
        """Refuse to audit a document that cannot produce findings."""

        if record.extraction_status != "extracted" or not any(
            page.text.strip() for page in record.pages
        ):
            raise DocumentNotAuditableError(
                "The document has no extractable text (it needs OCR), so it cannot be "
                "audited."
            )
        if record.invoice is None:
            raise DocumentNotAuditableError(
                "Structured invoice fields have not been extracted for this document, so "
                "it cannot be audited."
            )

    def _build_report(
        self,
        *,
        record: DocumentRecord,
        state: AuditState,
        rules: RulesConfig,
        settings: LLMSettings,
    ) -> AuditReport:
        """Turn the final graph state into a stored, secret-free report."""

        findings = [
            AuditFinding.model_validate(item) for item in state.get("findings", [])
        ]
        summary = summarise_findings(findings)
        summary.ruleset_version = rules.version
        summary.enabled_rule_count = len(rules.enabled_rules())

        status = str(state.get("audit_status") or "requires_review")
        if status not in (
            "completed",
            "completed_with_warnings",
            "requires_review",
            "cannot_audit",
        ):
            status = "requires_review"

        invoice_payload = state.get("invoice") or {}
        invoice_number = invoice_payload.get("invoice_number")
        if invoice_number is None and record.invoice is not None:
            invoice_number = record.invoice.invoice_number

        assessment = str(state.get("overall_assessment") or "").strip()
        if not assessment:
            assessment = (
                f"{summary.passed} rule(s) passed, {summary.failed} failed, "
                f"{summary.review_required} need review and "
                f"{summary.not_applicable} were not applicable."
            )
        if status == "cannot_audit":
            assessment = str(state.get("error") or NOT_AUDITED_MESSAGE)

        trace = AuditTrace(
            nodes=list(state.get("nodes", [])),
            iterations=int(state.get("iterations", 0) or 0),
            model_calls=int(state.get("model_calls", 0) or 0),
            tool_calls=list(state.get("tool_calls", [])),
            re_extraction_performed=bool(state.get("re_extraction_performed")),
            rules_evaluated=list(state.get("applicable_rule_ids", [])),
        )

        return AuditReport(
            document_no=record.document_no,
            invoice_number=invoice_number,
            audit_status=status,
            overall_assessment=f"{assessment} {CARRIED_OVER_MESSAGE}".strip(),
            overall_confidence_percent=float(
                state.get("overall_confidence_percent", 0.0) or 0.0
            ),
            confidence_explanation=str(state.get("confidence_explanation") or ""),
            provider=self._provider_label or settings.llm_provider_label,
            model=settings.llm_model or None,
            ruleset_version=rules.version,
            findings=findings,
            summary=summary,
            warnings=list(state.get("warnings", [])),
            review_reasons=list(state.get("review_reasons", [])),
            human_review_required=bool(state.get("human_review_required"))
            or any(finding.human_review_required for finding in findings),
            trace=trace,
        )


__all__ = [
    "AiAuditor",
    "AuditError",
    "AuditReportStore",
    "DocumentNotAuditableError",
    "UnknownDocumentError",
    "report_store",
]