# BathAgent System Architecture Specification

## 1. System Philosophy

In an enterprise database environment, unrestricted AI agent access introduces significant risks:
- Unintended bulk data modifications
- Subtly incorrect DML updates that violate business invariants
- Compliance and regulatory penalties from improper data changes

**BathAgent** solves this by enforcing strict separation between **canned reads / lookups** and **arbitrary mutations**:

```
+-------------------------------------------------------------------------+
|                        BathAgent (Google ADK)                          |
+-------------------------------------------------------------------------+
                    |                                 |
         [Reads & Canned Writes]             [Complex Mutations]
                    |                                 |
                    v                                 v
+-------------------------------------+   +-------------------------------+
|     MCP Toolbox for Databases       |   |    Wildfire Validator Proxy   |
|   (Auth: bathagent-sa / Read-Only)  |   | (Auth: wildfire-proxy-sa / DML) |
+-------------------------------------+   +-------------------------------+
                    |                                 |
                    v                                 v
+-------------------------------------------------------------------------+
|                   BathStuff AlloyDB for PostgreSQL                     |
+-------------------------------------------------------------------------+
```

---

## 2. Component Isolation Boundaries

### A. MCP Toolbox for Databases
* **Scope**: Serves standard customer service workflows.
* **Credentials**: Bound to `bathagent-sa`.
* **Guarantees**:
  - Exposes pre-validated parameterized tools for order lookups, order item details, and status cancellations.
  - Exposes `execute_sql_read_only` which verifies that incoming queries are strictly read-only (`SELECT`). Any statement with DML/DDL tokens is rejected before reaching the database.

### B. Wildfire Validator Proxy
* **Scope**: Serves all ad-hoc, bulk, or high-risk data corrections (e.g. retroactive tariff remediation).
* **Credentials**: Bound to `wildfire-proxy-sa`.
* **Guarantees**:
  - Intercepts agent proposals submitted via `propose_sql(sql_statement, description, validation="sandbox")`.
  - Executes the proposal inside an isolated, ephemeral AlloyDB clone (sandbox).
  - Calculates row-level before/after diffs.
  - Verifies policy rules (e.g. `POL-FIN-042`: Net financial invoice total invariance).
  - Queues changesets for human review in the Wildfire Console (`:8787`).
  - Only executes on production when approved by authorized personnel.

---

## 3. Data Model Summary (BathStuff)

The database schema (`scripts/db/schema.sql`) represents an e-commerce order management system:
* `suppliers`: Supplier directory and country of origin.
* `tax_codes`: Tax classifications and standard rates.
* `tax_eligibility_rules`: Special tariffs and trade rule modifications (e.g. 20% tariff on Elbonian toothpaste).
* `products`: Product catalog linked to suppliers and tax codes.
* `product_pricing_history`: Historical price points.
* `customers`: Customer accounts and shipping destinations.
* `orders`: Customer orders and lifecycle statuses (`PENDING`, `PROCESSING`, `SHIPPED`, `DELIVERED`, `CANCELLED`).
* `order_items`: Granular order line items, prices, and explanatory notes.
