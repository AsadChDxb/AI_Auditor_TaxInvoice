"""Agentic UAE VAT audit package (Phases 4-5, Revision 2).

* :mod:`app.audit.rules_config` (Phase 4) provides the authoritative, configurable
  ruleset that deterministic checks and the auditor consume.
* :mod:`app.audit.ai_auditor` (Phase 5) is the agentic audit service: it runs the
  LangGraph state machine, keeps the arithmetic deterministic, validates everything the
  model returns, and stores one report per document in memory.

No phase exposes an HTTP endpoint: that is Phase 6.
"""

from app.audit.ai_auditor import (
    AiAuditor,
    AuditError,
    AuditReportStore,
    DocumentNotAuditableError,
    UnknownDocumentError,
    report_store,
)
from app.audit.graph import (
    DEFAULT_MAX_AGENT_ITERATIONS,
    RECURSION_LIMIT,
    build_audit_graph,
)
from app.audit.nodes import MAX_TOOL_ROUNDS, overall_confidence
from app.audit.rules_config import (
    DEFAULT_RULE_IDS,
    HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
    LEGISLATION_REVIEW_NOTE,
    TRN_FORMAT_PATTERN,
    build_rules_config,
    default_rules,
    get_rules_config,
    resolve_expected_vat_rate,
    validate_rule_id,
)
from app.audit.state import AuditState, initial_state
from app.models.audit import (
    ALL_CLASSIFICATIONS,
    ZERO_TREATMENTS,
    AuditFinding,
    AuditReport,
    AuditRule,
    DisabledRuleError,
    RuleConfigError,
    RulesConfig,
    UnknownRuleError,
    summarise_findings,
)

__all__ = [
    "ALL_CLASSIFICATIONS",
    "DEFAULT_MAX_AGENT_ITERATIONS",
    "DEFAULT_RULE_IDS",
    "HUMAN_REVIEW_CONFIDENCE_THRESHOLD",
    "LEGISLATION_REVIEW_NOTE",
    "MAX_TOOL_ROUNDS",
    "RECURSION_LIMIT",
    "TRN_FORMAT_PATTERN",
    "ZERO_TREATMENTS",
    "AiAuditor",
    "AuditError",
    "AuditFinding",
    "AuditReport",
    "AuditReportStore",
    "AuditRule",
    "AuditState",
    "DisabledRuleError",
    "DocumentNotAuditableError",
    "RuleConfigError",
    "RulesConfig",
    "UnknownDocumentError",
    "UnknownRuleError",
    "build_audit_graph",
    "build_rules_config",
    "default_rules",
    "get_rules_config",
    "initial_state",
    "overall_confidence",
    "report_store",
    "resolve_expected_vat_rate",
    "summarise_findings",
    "validate_rule_id",
]

