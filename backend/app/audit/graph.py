"""LangGraph wiring for the agentic audit engine (Phase 5).

The graph is compiled per audit run and bound to one stored document: the document's
page text lives in the node closures rather than in the state, so no audit state or
checkpoint ever contains full invoice text.

Flow::

    START -> load_context -> select_rules -> deterministic_checks -> agent_review
          -> guardrail_validate -> (re_extract_fields -> select_rules | human_review
                                  | aggregate_report | cannot_audit)
          -> aggregate_report -> END

``re_extract_fields`` can only be entered while ``MAX_AGENT_ITERATIONS`` is not
exhausted, so the loop is bounded independently of LangGraph's recursion limit.
"""

from functools import partial
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import ConfigDict

from app.audit.nodes import (
    MAX_TOOL_ROUNDS,
    aggregate_report,
    cannot_audit,
    deterministic_checks,
    guardrail_validate,
    human_review,
    load_context,
    make_agent_node,
    make_re_extract_node,
    select_rules,
)
from app.audit.rules_config import get_rules_config
from app.audit.state import (
    MODEL_FAILURE_KINDS,
    AuditState,
    has_review_flag,
    initial_state,
)
from app.audit.tools import AuditToolbelt
from app.config import LLMSettings, get_llm_settings
from app.models.audit import RulesConfig
from app.models.invoice import InvoiceData
from app.services.llm import LLMExtractor, build_chat_model
from app.services.llm_errors import LLMError, configuration_error, map_exception
from app.storage.memory import DocumentRecord

RECURSION_LIMIT = 50
"""LangGraph step budget; the re-extraction loop is bounded separately."""

DEFAULT_MAX_AGENT_ITERATIONS = 2
"""Maximum passes through the model/re-extraction loop."""


class _UnavailableChatModel(BaseChatModel):
    """Chat model that always fails with a sanitised :class:`LLMError`.

    A deployment with no ``LLM_API_KEY``/``LLM_MODEL`` (or a provider that cannot even be
    constructed) must still be able to audit: the deterministic checks run, and the
    report asks for human review with a clear configuration message, instead of the
    request failing or a clean pass being claimed.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    error: LLMError

    @property
    def _llm_type(self) -> str:
        return "unavailable-model"

    def invoke(self, *args: Any, **kwargs: Any) -> BaseMessage:
        """Always fail with the stored, already-sanitised error."""

        raise self.error

    def _generate(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        """Kept for the abstract base class; ``invoke`` is what the nodes call."""

        raise self.error


def resolve_audit_chat_model(
    chat_model: BaseChatModel | None, settings: LLMSettings
) -> BaseChatModel:
    """Return the chat model to audit with, never raising for a missing provider.

    When the provider is not configured, or cannot be constructed for any reason, an
    :class:`_UnavailableChatModel` is returned so the audit still completes with its
    deterministic findings and a review request.
    """

    if chat_model is not None:
        return chat_model
    missing = settings.missing_settings()
    if missing:
        return _UnavailableChatModel(error=configuration_error(missing))
    try:
        return build_chat_model(settings)
    except Exception as exc:  # noqa: BLE001 - any failure means "model unavailable"
        return _UnavailableChatModel(
            error=map_exception(exc, (getattr(settings, "api_key", None),))
        )

ROUTE_AFTER_CONTEXT = {
    "continue": "select_rules",
    "cannot_audit": "cannot_audit",
}

ROUTE_AFTER_RULES = {
    "continue": "deterministic_checks",
    "cannot_audit": "cannot_audit",
}
ROUTE_AFTER_GUARDRAIL = {
    "re_extract_fields": "re_extract_fields",
    "human_review": "human_review",
    "aggregate_report": "aggregate_report",
    "cannot_audit": "cannot_audit",
}


def route_after_context(state: AuditState) -> str:
    """``cannot_audit`` when the context or the ruleset makes the audit impossible."""

    if state.get("audit_status") == "cannot_audit":
        return "cannot_audit"
    return "continue"


def route_after_guardrail(state: AuditState, max_iterations: int) -> str:
    """Decide between one more bounded pass, human review and the final report."""

    if state.get("audit_status") == "cannot_audit":
        return "cannot_audit"
    iterations = int(state.get("iterations", 0) or 0)
    if iterations >= max(1, max_iterations):
        return "human_review" if has_review_flag(state) else "aggregate_report"
    if state.get("failure_kind") in MODEL_FAILURE_KINDS:
        return "re_extract_fields"
    if state.get("missing_fields"):
        return "re_extract_fields"
    return "human_review" if has_review_flag(state) else "aggregate_report"


def make_toolbelt_factory(record: DocumentRecord, rules: RulesConfig) -> Any:
    """Build a factory that creates the toolbelt for the current invoice."""

    pages = [
        (page.page_number, page.text) for page in record.pages if page.text.strip()
    ]

    def factory(invoice: InvoiceData, missing_fields: Any = ()) -> AuditToolbelt:
        return AuditToolbelt(
            invoice=invoice,
            pages=pages,
            rules=rules,
            missing_fields=list(missing_fields or []),
        )

    return factory


def build_audit_graph(
    record: DocumentRecord,
    *,
    extractor: Any = None,
    chat_model: BaseChatModel | None = None,
    settings: LLMSettings | None = None,
    rules: RulesConfig | None = None,
    checkpointer: Any = None,
    max_iterations: int = DEFAULT_MAX_AGENT_ITERATIONS,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> Any:
    """Build and compile the audit graph for one stored document.

    Args:
        record: Stored document (page text and, ideally, extracted invoice fields).
        extractor: Invoice extractor used for the bounded re-extraction pass; defaults
            to the provider-agnostic :class:`~app.services.llm.LLMExtractor`.
        chat_model: Chat model used for the audit review; built from settings when
            omitted. A missing provider configuration is reported as a review state
            rather than an error, and tests inject a scripted fake so no network call is
            made.
        settings: LLM settings (prompt-size bound and, for redaction, the API key).
        rules: Ruleset to audit against; defaults to the configured ruleset.
        checkpointer: LangGraph checkpointer; defaults to an in-memory saver.
        max_iterations: Maximum passes through the model/re-extraction loop.
        max_tool_rounds: Maximum tool-calling rounds per model invocation.
    """

    resolved_rules = rules or get_rules_config()
    resolved_settings = settings or get_llm_settings()
    resolved_extractor = extractor if extractor is not None else LLMExtractor()
    resolved_model = resolve_audit_chat_model(chat_model, resolved_settings)

    builder = StateGraph(AuditState)
    builder.add_node("load_context", partial(load_context, record=record))
    builder.add_node("select_rules", partial(select_rules, rules=resolved_rules))
    builder.add_node(
        "deterministic_checks", partial(deterministic_checks, rules=resolved_rules)
    )
    builder.add_node(
        "agent_review",
        make_agent_node(
            rules=resolved_rules,
            chat_model=resolved_model,
            settings=resolved_settings,
            toolbelt_factory=make_toolbelt_factory(record, resolved_rules),
            max_tool_rounds=max_tool_rounds,
        ),
    )
    builder.add_node(
        "guardrail_validate", partial(guardrail_validate, rules=resolved_rules)
    )
    builder.add_node(
        "re_extract_fields",
        make_re_extract_node(
            record=record,
            extractor=resolved_extractor,
            api_key=resolved_settings.api_key,
        ),
    )
    builder.add_node("human_review", human_review)
    builder.add_node("cannot_audit", cannot_audit)
    builder.add_node(
        "aggregate_report", partial(aggregate_report, rules=resolved_rules)
    )

    builder.add_edge(START, "load_context")
    builder.add_conditional_edges(
        "load_context", route_after_context, ROUTE_AFTER_CONTEXT
    )
    builder.add_conditional_edges("select_rules", route_after_context, ROUTE_AFTER_RULES)
    builder.add_edge("deterministic_checks", "agent_review")
    builder.add_edge("agent_review", "guardrail_validate")
    builder.add_conditional_edges(
        "guardrail_validate",
        partial(route_after_guardrail, max_iterations=max_iterations),
        ROUTE_AFTER_GUARDRAIL,
    )
    builder.add_edge("re_extract_fields", "select_rules")
    builder.add_edge("human_review", "aggregate_report")
    builder.add_edge("cannot_audit", END)
    builder.add_edge("aggregate_report", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())


__all__ = [
    "DEFAULT_MAX_AGENT_ITERATIONS",
    "RECURSION_LIMIT",
    "build_audit_graph",
    "initial_state",
    "make_toolbelt_factory",
    "route_after_context",
    "route_after_guardrail",
]