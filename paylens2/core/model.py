"""
The v2 semantic model and the schema card sent to call 1.

The model is the SAME dataclass the classic pipeline uses, loaded from a different
YAML — which is what lets guard() and rbac() enforce v2 queries without a line of
security code being duplicated. See schema/views.yaml for why the anchor table
matters.

The schema card is v2's own, because the classic one is written for a pipeline
that compiles SQL rather than one that asks a model to write it. The difference
that matters: this card states the official metric expressions verbatim, because
check 10 asserts the model reproduced them exactly.
"""
from __future__ import annotations

import functools
import logging

from backend.core import db
from paylens2 import config
from semantic.loader import SemanticModel, load

log = logging.getLogger("paylens2.model")

# Columns a question is never about, kept out of the card so the model is not
# invited to group by an ingestion timestamp or a surrogate key.
_HIDDEN = {"ingested_at", "pm_id", "bank_id", "acquirer_bank_id", "settlement_id",
           "issuer_npci_bd_pct", "issuer_npci_td_pct", "failure_description"}

_INDICATORS = {
    "is_success": "status = 'SUCCESS'",
    "is_failed": "status = 'FAILED'",
    "is_pending": "status = 'PENDING'",
    "is_resolved": "status IN ('SUCCESS','FAILED')  -- PENDING excluded, deliberately",
    "is_business_decline": "FAILED and the customer's fault (no funds, wrong PIN, blocked card)",
    "is_technical_decline": "FAILED and our side (terminal, gateway, acquirer, issuer system)",
    "success_amount": "amount when SUCCESS, else 0",
    "failed_amount": "amount when FAILED, else 0",
}


@functools.lru_cache(maxsize=1)
def get_model() -> SemanticModel:
    return load(config.VIEWS_YAML)


@functools.lru_cache(maxsize=1)
def views_present() -> bool:
    """False when schema/build.py has not been run against this database."""
    try:
        cols = db.table_columns()
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot read the database catalog: %s", exc)
        return False
    return "v_attempts" in cols


@functools.lru_cache(maxsize=1)
def _enum_values() -> dict[str, list[str]]:
    """
    Distinct values for low-cardinality dimensions.

    This is schema, not data: a model cannot filter on UPI without knowing the
    literal is 'UPI' and not 'upi' or 'UPI_COLLECT'. Guessing the literal is the
    single most common cause of a syntactically perfect query returning zero rows
    and being narrated as "there were none".
    """
    out: dict[str, list[str]] = {}
    wanted = ["payment_method", "entry_mode", "status", "decline_type", "region",
              "merchant_tier", "card_network", "card_type", "terminal_health",
              "terminal_connectivity", "risk_band", "failure_owner",
              "journey_step_label", "merchant_category", "failure_category"]
    for col in wanted:
        try:
            rows = db.fetch_dicts(
                f"SELECT DISTINCT {col} AS v FROM v_attempts "
                f"WHERE {col} IS NOT NULL ORDER BY 1 LIMIT {config.MAX_ENUM_VALUES + 1}")
        except Exception:  # noqa: BLE001
            continue
        vals = [str(r["v"]) for r in rows]
        if 0 < len(vals) <= config.MAX_ENUM_VALUES:
            out[col] = vals
    return out


@functools.lru_cache(maxsize=1)
def _date_range() -> tuple[str, str]:
    try:
        r = db.fetch_dicts("SELECT min(txn_date) AS lo, max(txn_date) AS hi FROM v_attempts")
        return str(r[0]["lo"]), str(r[0]["hi"])
    except Exception:  # noqa: BLE001
        return "", ""


def metric_expressions() -> dict[str, str]:
    """metric id -> the one expression that computes it. Check 10 asserts these."""
    m = get_model()
    out: dict[str, str] = {}
    for mid, met in m.metrics.items():
        if met.is_ratio:
            out[mid] = f"({met.numerator})::DOUBLE / nullif({met.denominator}, 0)"
        else:
            out[mid] = met.expr or ""
    return out


def schema_card() -> str:
    """The exact text handed to call 1. Exposed at GET /v2/schema for debugging."""
    m = get_model()
    cols = db.table_columns() if views_present() else {}
    enums = _enum_values() if views_present() else {}
    lo, hi = _date_range() if views_present() else ("", "")

    lines: list[str] = []
    lines.append("# SCHEMA")
    lines.append("")
    lines.append("You may query these views and NOTHING else. The underlying "
                 "fact_* and dim_* tables are blocked by the query validator.")
    if lo:
        lines.append(f"Data covers {lo} to {hi}. The last 2 days are partially "
                     f"ingested and understate volume.")
    lines.append("")

    for ds_id, ds in m.datasets.items():
        lines.append(f"## {ds_id} — {ds.label}")
        lines.append(f"{' '.join(ds.description.split())}")
        lines.append("")
        present = sorted(cols.get(ds_id, ()))
        shown = [c for c in present if c not in _HIDDEN and c not in _INDICATORS]
        if shown:
            lines.append("Columns: " + ", ".join(shown))
        if ds_id == "v_attempts":
            lines.append("")
            lines.append("Indicator columns — USE THESE, do not re-derive them "
                         "from `status`:")
            for k, v in _INDICATORS.items():
                if k in present:
                    lines.append(f"  {k:22} {v}")
        lines.append("")

    lines.append("## METRIC DEFINITIONS — use the expression exactly as written")
    lines.append("")
    for mid, expr in metric_expressions().items():
        met = m.metrics[mid]
        lines.append(f"  {mid}")
        lines.append(f"      {expr}")
        if met.denominator_note:
            lines.append(f"      note: {' '.join(met.denominator_note.split())}")
    lines.append("")

    if enums:
        lines.append("## COLUMN VALUES — filter using these literals exactly")
        lines.append("")
        for col, vals in enums.items():
            lines.append(f"  {col}: {', '.join(vals)}")
        lines.append("")

    lines.append("## RULES")
    lines.append("")
    lines.append("- SELECT only. One statement. No CTE is required but they are allowed.")
    lines.append("- Do not add a LIMIT unless the user asked for a sample; the "
                 "validator attaches its own row cap.")
    lines.append("- Filter on whatever the QUESTION names — a merchant, a bank, "
                 "a rail, a region, a period. Omitting it answers a different "
                 "question.")
    lines.append("- Do not add an ACCESS-CONTROL filter restricting the result to "
                 "the viewer's own merchant or region; that is injected after you "
                 "write the query, from their verified identity.")
    lines.append("- Alias every computed column with the metric id when you compute "
                 "a known metric (e.g. `AS success_rate`).")
    lines.append("- Dates: txn_ts is a timestamp, txn_date is a DATE. Use "
                 "half-open ranges: txn_ts >= DATE 'x' AND txn_ts < DATE 'y'.")
    # Period conventions are pinned because the classic pipeline resolves these
    # phrases deterministically. A model reading "last week" as the trailing
    # seven days returns a different number for the same question than /ask
    # does, which is the same class of divergence check 10 exists to prevent —
    # just in the time dimension rather than the metric one.
    # A real answer this produced: "the 'customer' failure owner has a 0%
    # success rate on 19,784 attempts". True of the rows, and meaningless —
    # failure_owner is only ever set on a FAILED attempt, so every group it
    # forms has a success rate of zero by construction. The model cannot know
    # that from column names alone, so the card says it.
    lines.append("- FAILURE-ONLY COLUMNS: failure_code, failure_category, "
                 "decline_type, failure_owner, journey_step_label and "
                 "is_retriable are populated ONLY on failed attempts and are "
                 "NULL everywhere else.")
    lines.append("    Never compute success_rate or failure_rate grouped by one "
                 "of them — every group is 0% or 100% by construction and the "
                 "answer reads as a finding when it is an artefact.")
    lines.append("    To analyse failures, COUNT them by these columns "
                 "(count(*) filter (where is_failed)), or filter to "
                 "is_failed first.")
    lines.append("- Periods are resolved for you in the PERIODS section below. "
                 "Copy those dates rather than computing your own.")
    lines.append("- Amounts are in INDIAN RUPEES. Never write a dollar sign.")
    return "\n".join(lines)
