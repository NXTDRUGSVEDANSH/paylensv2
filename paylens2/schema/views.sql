-- PayLens v2 analytical views.
--
-- These exist so an LLM writing SQL cannot get two things wrong that it would
-- otherwise get wrong on most attempts:
--
--   1. DE-DUPLICATION. fact_transactions contains deliberate duplicate txn_ids
--      (a planted double-ingest). Every honest query must apply the QUALIFY rule
--      below. Baked in here, it is no longer something the model must remember.
--
--   2. JOINS. Five dimension tables, each with its own key. Pre-joined here, so
--      the prompt contains one table and the model writes no JOIN at all.
--
-- The indicator columns (is_success, is_resolved, ...) carry the OFFICIAL metric
-- definitions. success_rate is sum(is_success) / sum(is_resolved) and is_resolved
-- deliberately excludes PENDING -- a pending attempt has not resolved, so counting
-- it as a failure understates the rate.
--
-- Column naming: dim_merchant and dim_terminal both carry city/state/region. The
-- merchant's are unprefixed (they are what "region" means to a user and what RBAC
-- scopes on); the terminal's are prefixed terminal_*.

CREATE OR REPLACE VIEW v_attempts AS
WITH deduped AS (
  SELECT *
  FROM fact_transactions
  QUALIFY row_number() OVER (PARTITION BY txn_id ORDER BY ingested_at DESC) = 1
)
SELECT
  -- identity and time
  t.txn_id,
  t.txn_ts,
  CAST(t.txn_ts AS DATE)                               AS txn_date,
  t.ingested_at,

  -- merchant (RBAC anchors here)
  t.merchant_id,
  m.merchant_name,
  m.mcc,
  m.category                                           AS merchant_category,
  m.city                                               AS city,
  m.state                                              AS state,
  m.region                                             AS region,
  m.tier                                               AS merchant_tier,
  m.risk_band,

  -- issuing and acquiring banks
  t.bank_id,
  ib.bank_name                                         AS issuer_bank,
  ib.bank_type                                         AS issuer_bank_type,
  ib.is_payments_bank                                  AS issuer_is_payments_bank,
  ib.npci_business_decline_pct                         AS issuer_npci_bd_pct,
  ib.npci_technical_decline_pct                        AS issuer_npci_td_pct,
  t.acquirer_bank_id,
  ab.bank_name                                         AS acquirer_bank,

  -- payment instrument
  t.pm_id,
  pm.method                                            AS payment_method,
  pm.card_network,
  pm.card_type,
  pm.is_tokenized,
  t.entry_mode,
  t.is_emi,

  -- terminal estate
  t.terminal_id,
  tm.health                                            AS terminal_health,
  tm.connectivity                                      AS terminal_connectivity,
  tm.is_software_stale                                 AS terminal_software_stale,
  tm.city                                              AS terminal_city,
  tm.state                                             AS terminal_state,
  tm.region                                            AS terminal_region,

  -- outcome
  t.status,
  t.amount,
  t.currency,
  t.latency_ms,
  t.is_retry,
  t.original_txn_id,
  t.settlement_id,

  -- failure attribution
  t.failure_code,
  fc.category                                          AS failure_category,
  fc.description                                       AS failure_description,
  fc.decline_type,
  fc.is_retriable,
  fc.owner                                             AS failure_owner,
  fc.journey_step,
  fc.journey_step_label,

  -- ── OFFICIAL METRIC COMPONENTS ──────────────────────────────────────────
  -- Use these. Do not re-derive them from status.
  (t.status = 'SUCCESS')                               AS is_success,
  (t.status = 'FAILED')                                AS is_failed,
  (t.status = 'PENDING')                               AS is_pending,
  (t.status IN ('SUCCESS','FAILED'))                   AS is_resolved,
  (t.status = 'FAILED' AND fc.decline_type = 'business')  AS is_business_decline,
  (t.status = 'FAILED' AND fc.decline_type = 'technical') AS is_technical_decline,
  CASE WHEN t.status = 'SUCCESS' THEN t.amount ELSE 0 END  AS success_amount,
  CASE WHEN t.status = 'FAILED'  THEN t.amount ELSE 0 END  AS failed_amount
FROM deduped t
  LEFT JOIN dim_merchant       m  ON m.merchant_id   = t.merchant_id
  LEFT JOIN dim_bank           ib ON ib.bank_id      = t.bank_id
  LEFT JOIN dim_bank           ab ON ab.bank_id      = t.acquirer_bank_id
  LEFT JOIN dim_payment_method pm ON pm.pm_id        = t.pm_id
  LEFT JOIN dim_terminal       tm ON tm.terminal_id  = t.terminal_id
  LEFT JOIN dim_failure_code   fc ON fc.failure_code = t.failure_code;


-- Settlements, with the merchant's region carried across so the same RBAC
-- predicate shape works on both views.
CREATE OR REPLACE VIEW v_settlements AS
SELECT
  s.settlement_id,
  s.merchant_id,
  m.merchant_name,
  m.region                                             AS region,
  m.state                                              AS state,
  s.settlement_date,
  s.txn_count                                          AS settled_txn_count,
  s.gross_amount,
  s.fees,
  s.tax,
  s.net_amount,
  s.status                                             AS settlement_status
FROM fact_settlements s
  LEFT JOIN dim_merchant m ON m.merchant_id = s.merchant_id;
