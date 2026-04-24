-- =============================================================================
-- operations.msl_change_decisions
-- -----------------------------------------------------------------------------
-- Append-only log of MSL (Minimum Stock Level) change decisions made by
-- leadership/admin users on the Validation → MSL Changes page.
--
-- Natural key: (manufacturer_sku, product_modified_on). A decision pins a
-- specific MSL change (identified by its REX modification timestamp) so that
-- when the MSL changes again, a new `product_modified_on` is emitted by the
-- Dataform source view and the row reappears in the approval queue.
--
-- Writes: one row per decision, via
--   purchase_orders_service.client.insert_rows_json(...)
-- Reads:  LEFT JOIN from dataform.rex_ballarat_msl_changes on the natural key
--         to filter out already-decided rows from the pending queue.
-- =============================================================================

CREATE TABLE IF NOT EXISTS `chainsawspares-385722.operations.msl_change_decisions` (
  decision_id          STRING     NOT NULL  OPTIONS(description="UUID for the decision row."),
  manufacturer_sku     STRING     NOT NULL  OPTIONS(description="REX manufacturer SKU (natural key with product_modified_on)."),
  product_modified_on  TIMESTAMP  NOT NULL  OPTIONS(description="REX product modification timestamp; identifies the specific MSL change."),
  previous_msl         INT64                OPTIONS(description="MSL value before the change, snapshot at decision time."),
  new_msl              INT64                OPTIONS(description="MSL value after the change, snapshot at decision time."),
  short_description    STRING               OPTIONS(description="Product description snapshot at decision time (for audit display when the source row ages out)."),
  supplier_code        STRING               OPTIONS(description="Supplier code snapshot at decision time."),
  decision             STRING     NOT NULL  OPTIONS(description="'approved' or 'declined'."),
  decided_by           STRING     NOT NULL  OPTIONS(description="Username from the local app (users.db)."),
  decided_at           TIMESTAMP  NOT NULL  OPTIONS(description="When the decision was made (UTC)."),
  comment              STRING               OPTIONS(description="Optional free-text reason. Reserved for the Phase 2 decline workflow.")
)
CLUSTER BY manufacturer_sku
OPTIONS(
  description="Append-only log of MSL change decisions (approve/decline) made in chainsaw-ops."
);
