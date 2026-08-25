"""Review routing ladder: deterministic rules -> auto / LLM / human.

Role policies are admin-authored templates (never authored by the agent or
the task). They now live in the control plane store and are editable from
the console's Policies page; ROLES below are the defaults / fallback.

Config per role:
  tables         list  — allowlist of touchable tables
  hard_max_rows  int   — above this the changeset is REJECTED outright
  auto_max_rows  int   — above this (but ≤ hard) it escalates to HUMAN
  ddl            bool  — whether schema changes are allowed at all
  small_reviewer str   — lane for small rule-passing changes: auto|llm|human
DDL, when allowed, ALWAYS requires a human.

route(cs, role_name, cfg=None) ->
  ("reject", failed_checks) | ("auto"|"llm"|"human", checks)
"""

ROLES = {
    "bathagent": {"tables": ["orders", "order_items", "customers"], "hard_max_rows": 50,
                  "auto_max_rows": 20, "ddl": False, "small_reviewer": "llm"},
    "booking":   {"tables": ["orders", "order_items", "customers"], "hard_max_rows": 50,
                  "auto_max_rows": 20, "ddl": False, "small_reviewer": "llm"},
    "developer": {"tables": ["orders", "order_items", "products", "product_pricing_history", "suppliers", "tax_codes", "tax_eligibility_rules"],
                  "hard_max_rows": 10000, "auto_max_rows": 100, "ddl": True,
                  "small_reviewer": "llm"},
    "analytics": {"tables": ["orders", "products", "suppliers"], "hard_max_rows": 5000,
                  "auto_max_rows": 100, "ddl": False, "small_reviewer": "llm"},
}


def route(cs, role_name, cfg=None):
    role = cfg or ROLES[role_name]
    allowed = set(role["tables"])
    touched = {d["relid"] for d in cs.net_diff}
    has_ddl = cs.basis_fp != cs.final_fp
    rows = len(cs.net_diff)
    checks = {
        f"touched ⊆ {sorted(allowed)}": touched <= allowed,
        f"rows {rows} ≤ hard limit {role['hard_max_rows']}": rows <= role["hard_max_rows"],
        "ddl allowed for role" if has_ddl else "no ddl": role["ddl"] or not has_ddl,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        return "reject", failed
    if has_ddl:
        checks["ddl ⇒ human sign-off"] = True
        return "human", checks
    if rows > role["auto_max_rows"]:
        checks[f"rows > auto limit {role['auto_max_rows']} ⇒ human"] = True
        return "human", checks
    return role.get("small_reviewer", "llm"), checks
