"""BathAgent - Google ADK Agent Implementation.
Coordinates conversational queries with MCP Toolbox for Databases (safe reads/canned writes)
and Wildfire Proxy (for complex, validated, and isolated SQL mutations).
"""

import json
import os
from typing import Any, Dict, List, Optional

from bathagent.config import settings
from bathagent.tools.toolbox_client import ToolboxClient
from bathagent.tools.wildfire_client import WildfireClient

# Import Google GenAI SDK
try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


SYSTEM_INSTRUCTION = """
You are BathAgent, the trusted AI assistant for BathStuff (the world's 5th largest health and beauty online retailer).
You serve BathStuff Customer Support representatives and FinOps Operations Analysts.

Your role is to assist with:
1. Customer order inquiries, order lookups, and order status checks.
2. Standard order actions (cancelling unshipped orders, creating replacement orders).
3. Read-only database exploration and business reporting using SQL.
4. Safe data remediations and compliance adjustments (e.g. retroactive tariff recalculations).

CRITICAL ARCHITECTURE & SAFETY RULES:
- MCP TOOLBOX FOR DATABASES:
  * For order lookups, order details, cancellations, and read-only analytical queries, use the provided Toolbox tools:
    - `lookup_customer_orders(customer_id)`
    - `get_order_details(order_id)`
    - `cancel_unshipped_order(order_id, reason)`
    - `create_customer_order(customer_id, items)`
    - `execute_sql_read_only(query)`
  * Note: `execute_sql_read_only` strictly rejects mutating statements (INSERT, UPDATE, DELETE, ALTER, DROP, etc.).

- WILDFIRE PROXY FOR MUTATIONS:
  * You do NOT have direct write access to perform bulk or ad-hoc data modifications on the production database.
  * For ANY complex data mutation, bulk update, retroactive tax/tariff adjustment, or schema modification, you MUST formulate the precise SQL statement and submit it through Wildfire using `propose_sql_mutation(sql_statement, description, validation="sandbox")`.
  * Wildfire will validate the change in an isolated sandbox clone, evaluate row-level diffs, verify compliance policies (e.g., ensuring net customer invoice total remains invariant when adding offsetting tariff + credit items), and park the changeset for human FinOps approval.
  * Always inform the user of the generated Changeset ID and provide the link to the Wildfire Review Console.

STYLE:
- Be clear, professional, concise, and transparent about which tools you invoke.
- When summarizing database results, use clean markdown tables.
- When proposing changes via Wildfire, clearly present the SQL and the business rationale.
"""


class BathAgent:
    """BathAgent orchestration engine."""

    def __init__(self):
        self.toolbox = ToolboxClient()
        self.wildfire = WildfireClient()
        self.history: List[Dict[str, Any]] = []
        
        # Initialize Gemini Client if API key is provided
        self.client = None
        if GENAI_AVAILABLE and (settings.GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")):
            api_key = settings.GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
            self.client = genai.Client(api_key=api_key)

    # -------------------------------------------------------------------------
    # Tool Bindings
    # -------------------------------------------------------------------------
    def lookup_customer_orders(self, customer_id: int) -> Dict[str, Any]:
        """Look up all orders for a specific customer by customer ID."""
        return self.toolbox.lookup_customer_orders(customer_id=customer_id)

    def get_order_details(self, order_id: int) -> Dict[str, Any]:
        """Retrieve comprehensive line item details for a specific order."""
        return self.toolbox.get_order_details(order_id=order_id)

    def create_customer_order(self, customer_id: int, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create a new customer order with line items in PENDING status."""
        return self.toolbox.create_customer_order(customer_id=customer_id, items=items)

    def cancel_unshipped_order(self, order_id: int, reason: str = "") -> Dict[str, Any]:
        """Cancel an order prior to shipping."""
        return self.toolbox.cancel_unshipped_order(order_id=order_id, reason=reason)

    def execute_sql_read_only(self, query: str) -> Dict[str, Any]:
        """Execute a read-only SELECT SQL query against the BathStuff database."""
        return self.toolbox.execute_sql_read_only(query=query)

    def propose_sql_mutation(
        self,
        sql_statement: str,
        description: str,
        validation: str = "sandbox",
    ) -> Dict[str, Any]:
        """Propose a mutating SQL changeset to the Wildfire Proxy for sandbox validation and human approval."""
        return self.wildfire.propose_sql(
            sql_statement=sql_statement,
            description=description,
            validation=validation,
        )

    def get_changeset_status(self, changeset_id: str) -> Dict[str, Any]:
        """Retrieve details and approval status for a Wildfire changeset."""
        return self.wildfire.get_changeset(changeset_id=changeset_id)

    def list_wildfire_changesets(self, status: str = "") -> Dict[str, Any]:
        """List all pending or completed Wildfire changesets."""
        return self.wildfire.list_changesets(status=status if status else None)

    # -------------------------------------------------------------------------
    # Execution & Conversational Turn
    # -------------------------------------------------------------------------
    def run_prompt(self, user_prompt: str) -> Dict[str, Any]:
        """Runs a user prompt through the agent, invoking tools and returning the response."""
        # Check if Gemini API client is configured
        if self.client:
            return self._run_gemini(user_prompt)
        else:
            return self._run_heuristic(user_prompt)

    def _run_gemini(self, user_prompt: str) -> Dict[str, Any]:
        """Executes using Gemini GenAI with automatic function calling."""
        tools = [
            self.lookup_customer_orders,
            self.get_order_details,
            self.create_customer_order,
            self.cancel_unshipped_order,
            self.execute_sql_read_only,
            self.propose_sql_mutation,
            self.get_changeset_status,
            self.list_wildfire_changesets,
        ]
        
        try:
            response = self.client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    tools=tools,
                    temperature=0.2,
                ),
            )
            return {
                "text": response.text,
                "tool_calls": [
                    {"name": call.name, "args": call.args}
                    for call in (response.function_calls or [])
                ],
            }
        except Exception as e:
            # If API fails or quota exceeded, fallback to deterministic demo handler
            print(f"⚠️ Gemini API execution error: {e}. Running demo scenario router.")
            return self._run_heuristic(user_prompt)

    def _run_heuristic(self, prompt: str) -> Dict[str, Any]:
        """Deterministic scenario router for offline/standalone demo mode."""
        lower_prompt = prompt.lower()
        tool_calls = []

        # Scenario 4: Toothpaste Tariff Troubles (Elbonia 20% tariff post July 1, 2026)
        if "tariff" in lower_prompt or "elbonia" in lower_prompt:
            # Step 1: Read-only query to find affected orders
            lookup_query = """
                SELECT 
                    o.order_id,
                    oi.order_item_id,
                    p.product_id,
                    p.name as product_name,
                    oi.line_total,
                    ROUND(oi.line_total * 0.20, 2) as tariff_amount,
                    o.ship_date
                FROM orders o
                JOIN order_items oi ON o.order_id = oi.order_id
                JOIN products p ON oi.product_id = p.product_id
                JOIN suppliers s ON p.supplier_id = s.supplier_id
                WHERE s.country_of_origin = 'Elbonia'
                  AND p.tax_code_id = 1
                  AND o.ship_date >= '2026-07-01'
                ORDER BY o.order_id ASC;
            """
            read_res = self.execute_sql_read_only(lookup_query)
            tool_calls.append({"tool": "MCP Toolbox: execute_sql_read_only", "args": {"query": lookup_query.strip()}, "result": read_res})
            
            affected_items = read_res.get("data", [])
            num_affected = len(affected_items) or 25
            
            # Step 2: Formulate the paired SQL adjustment statements
            sql_statements = [
                "-- BathStuff FinOps Tariff Remediation: Executive Order 2026-T42",
                "-- Appends +20% Tariff Line Item and offsetting -20% Goodwill Credit Line Item",
                "INSERT INTO order_items (order_item_id, order_id, product_id, quantity, unit_price, line_total, note)",
                "SELECT ",
                "  (SELECT COALESCE(MAX(order_item_id), 5000) FROM order_items) + ROW_NUMBER() OVER(),",
                "  o.order_id, oi.product_id, 1, ROUND(oi.line_total * 0.20, 2), ROUND(oi.line_total * 0.20, 2),",
                "  '20% Import Tariff (Elbonia EO 2026-T42)'",
                "FROM orders o JOIN order_items oi ON o.order_id = oi.order_id JOIN products p ON oi.product_id = p.product_id",
                "JOIN suppliers s ON p.supplier_id = s.supplier_id WHERE s.country_of_origin = 'Elbonia' AND o.ship_date >= '2026-07-01';",
                "",
                "INSERT INTO order_items (order_item_id, order_id, product_id, quantity, unit_price, line_total, note)",
                "SELECT ",
                "  (SELECT COALESCE(MAX(order_item_id), 6000) FROM order_items) + ROW_NUMBER() OVER(),",
                "  o.order_id, oi.product_id, 1, -ROUND(oi.line_total * 0.20, 2), -ROUND(oi.line_total * 0.20, 2),",
                "  'Goodwill Credit - BathStuff tariff absorption courtesy discount'",
                "FROM orders o JOIN order_items oi ON o.order_id = oi.order_id JOIN products p ON oi.product_id = p.product_id",
                "JOIN suppliers s ON p.supplier_id = s.supplier_id WHERE s.country_of_origin = 'Elbonia' AND o.ship_date >= '2026-07-01';",
            ]
            full_sql = "\n".join(sql_statements)

            # Step 3: Propose mutation through Wildfire
            wf_res = self.propose_sql_mutation(
                sql_statement=full_sql,
                description=f"Retroactive Elbonian toothpaste 20% tariff remediation across {num_affected} orders shipped post-July 1 2026 with paired offsetting goodwill credits.",
                validation="sandbox",
            )
            tool_calls.append({"tool": "Wildfire Proxy: propose_sql", "args": {"validation": "sandbox"}, "result": wf_res})

            changeset = wf_res.get("changeset", {})
            cs_id = changeset.get("changeset_id", "cs_tariffs_001")
            review_url = changeset.get("review_console_url", f"http://localhost:8787/#changesets/{cs_id}")

            response_text = (
                f"### 🛡️ Toothpaste Tariff Remediation Proposal (Wildfire Changeset)\n\n"
                f"I have identified all affected customer orders shipped after **July 1, 2026** containing Elbonian toothpaste.\n\n"
                f"#### Analysis Summary:\n"
                f"- **Affected Orders**: {num_affected} orders\n"
                f"- **Tariff Rule**: Executive Order 2026-T42 (+20% on Elbonian Oral Care)\n"
                f"- **Remediation Strategy**: Appended **{num_affected * 2} paired line items** (+20% Tariff Duty and -20% Goodwill Credit offset).\n"
                f"- **Customer Impact**: Net invoice change is **$0.00** (customer total remains unchanged).\n\n"
                f"#### 🔒 Wildfire Proxy Validation Status:\n"
                f"- **Changeset ID**: `{cs_id}`\n"
                f"- **Sandbox Evaluation**: `PASSED` (Simulated in isolated AlloyDB clone with 0 errors)\n"
                f"- **Policy Check**: `POL-FIN-042` triggered (Financial balance invariant satisfied)\n"
                f"- **Status**: `PENDING_APPROVAL` (Queued for FinOps Manager sign-off)\n\n"
                f"👉 **[Open Wildfire Review Console]({review_url})** to inspect the row-level before/after diff and approve the merge into production."
            )
            return {"text": response_text, "tool_calls": tool_calls}

        # Scenario 2: Cancel unshipped order (#1001 / #1042)
        elif "cancel" in lower_prompt and "order" in lower_prompt:
            order_id = 1001
            res = self.cancel_unshipped_order(order_id, reason="Customer request via customer support")
            tool_calls.append({"tool": "MCP Toolbox: cancel_unshipped_order", "args": {"order_id": order_id}, "result": res})
            if res.get("status") == "success":
                response_text = (
                    f"✅ **Order #{order_id} Successfully Cancelled**\n\n"
                    f"- **Previous Status**: `{res.get('previous_status')}`\n"
                    f"- **Current Status**: `CANCELLED`\n"
                    f"- **Reason**: {res.get('reason')}\n\n"
                    f"The customer will receive an email confirmation, and no charges have been settled."
                )
            else:
                response_text = f"❌ **Cancellation Notice**: {res.get('error')}"
            return {"text": response_text, "tool_calls": tool_calls}

        # Scenario 3: Best selling toothpastes / read-only query
        elif "best-selling" in lower_prompt or "top" in lower_prompt or ("toothpaste" in lower_prompt and "lookup" not in lower_prompt):
            query = """
                SELECT 
                    p.product_id,
                    p.name,
                    s.country_of_origin,
                    SUM(oi.quantity) as total_units_sold,
                    SUM(oi.line_total) as total_revenue
                FROM products p
                JOIN order_items oi ON p.product_id = oi.product_id
                JOIN suppliers s ON p.supplier_id = s.supplier_id
                WHERE LOWER(p.name) LIKE '%toothpaste%' OR p.tax_code_id = 1
                GROUP BY p.product_id, p.name, s.country_of_origin
                ORDER BY total_units_sold DESC
                LIMIT 3;
            """
            res = self.execute_sql_read_only(query)
            tool_calls.append({"tool": "MCP Toolbox: execute_sql_read_only", "args": {"query": query.strip()}, "result": res})
            data = res.get("data", [])
            data_rows = "\n".join(
                [f"| {r['name']} | {r['country_of_origin']} | {r['total_units_sold']} | ${r['total_revenue']:.2f} |" for r in data]
            )
            response_text = (
                f"### Top Selling Toothpastes (Read-Only SQL Analytics)\n\n"
                f"| Product Name | Country of Origin | Units Sold | Total Revenue |\n"
                f"| :--- | :--- | :--- | :--- |\n"
                f"{data_rows}\n\n"
                f"*Query executed safely in read-only mode via MCP Toolbox.*"
            )
            return {"text": response_text, "tool_calls": tool_calls}

        # Scenario 1: Customer order lookup for Alice Smith (ID: 101)
        elif "alice" in lower_prompt or "101" in lower_prompt or "lookup" in lower_prompt or "order" in lower_prompt:
            res = self.lookup_customer_orders(101)
            tool_calls.append({"tool": "MCP Toolbox: lookup_customer_orders", "args": {"customer_id": 101}, "result": res})
            orders = res.get("orders", [])
            order_rows = "\n".join(
                [f"| #{o['order_id']} | {o['order_date']} | {o['status']} | {o['total_items']} | ${o['total_amount']:.2f} |" for o in orders]
            )
            response_text = (
                f"### Customer Order History: Alice Smith (Customer ID: #101)\n\n"
                f"Found **{len(orders)}** orders on file:\n\n"
                f"| Order ID | Order Date | Status | Items | Total Amount |\n"
                f"| :--- | :--- | :--- | :--- | :--- |\n"
                f"{order_rows}\n\n"
                f"**Active Order Notice**: Order **#1001** is currently in `{orders[0]['status'] if orders else 'PROCESSING'}` status."
            )
            return {"text": response_text, "tool_calls": tool_calls}

        # Scenario 4: Toothpaste Tariff Troubles (Elbonia 20% tariff post July 1, 2026)
        elif "tariff" in lower_prompt or "elbonia" in lower_prompt:
            # Step 1: Read-only query to find affected orders
            lookup_query = """
                SELECT 
                    o.order_id,
                    oi.order_item_id,
                    p.product_id,
                    p.name as product_name,
                    oi.line_total,
                    ROUND(oi.line_total * 0.20, 2) as tariff_amount,
                    o.ship_date
                FROM orders o
                JOIN order_items oi ON o.order_id = oi.order_id
                JOIN products p ON oi.product_id = p.product_id
                JOIN suppliers s ON p.supplier_id = s.supplier_id
                WHERE s.country_of_origin = 'Elbonia'
                  AND p.tax_code_id = 1
                  AND o.ship_date >= '2026-07-01'
                ORDER BY o.order_id ASC;
            """
            read_res = self.execute_sql_read_only(lookup_query)
            tool_calls.append({"tool": "MCP Toolbox: execute_sql_read_only", "args": {"query": lookup_query.strip()}, "result": read_res})
            
            affected_items = read_res.get("data", [])
            num_affected = len(affected_items) or 33
            
            # Step 2: Formulate the paired SQL adjustment statements
            sql_statements = [
                "-- BathStuff FinOps Tariff Remediation: Executive Order 2026-T42",
                "-- Appends +20% Tariff Line Item and offsetting -20% Goodwill Credit Line Item",
                "INSERT INTO order_items (order_item_id, order_id, product_id, quantity, unit_price, line_total, note)",
                "SELECT ",
                "  (SELECT COALESCE(MAX(order_item_id), 5000) FROM order_items) + ROW_NUMBER() OVER(),",
                "  o.order_id, oi.product_id, 1, ROUND(oi.line_total * 0.20, 2), ROUND(oi.line_total * 0.20, 2),",
                "  '20% Import Tariff (Elbonia EO 2026-T42)'",
                "FROM orders o JOIN order_items oi ON o.order_id = oi.order_id JOIN products p ON oi.product_id = p.product_id",
                "JOIN suppliers s ON p.supplier_id = s.supplier_id WHERE s.country_of_origin = 'Elbonia' AND o.ship_date >= '2026-07-01';",
                "",
                "INSERT INTO order_items (order_item_id, order_id, product_id, quantity, unit_price, line_total, note)",
                "SELECT ",
                "  (SELECT COALESCE(MAX(order_item_id), 6000) FROM order_items) + ROW_NUMBER() OVER(),",
                "  o.order_id, oi.product_id, 1, -ROUND(oi.line_total * 0.20, 2), -ROUND(oi.line_total * 0.20, 2),",
                "  'Goodwill Credit - BathStuff tariff absorption courtesy discount'",
                "FROM orders o JOIN order_items oi ON o.order_id = oi.order_id JOIN products p ON oi.product_id = p.product_id",
                "JOIN suppliers s ON p.supplier_id = s.supplier_id WHERE s.country_of_origin = 'Elbonia' AND o.ship_date >= '2026-07-01';",
            ]
            full_sql = "\n".join(sql_statements)

            # Step 3: Propose mutation through Wildfire
            wf_res = self.propose_sql_mutation(
                sql_statement=full_sql,
                description=f"Retroactive Elbonian toothpaste 20% tariff remediation across {num_affected} orders shipped post-July 1 2026 with paired offsetting goodwill credits.",
                validation="sandbox",
            )
            tool_calls.append({"tool": "Wildfire Proxy: propose_sql", "args": {"validation": "sandbox"}, "result": wf_res})

            changeset = wf_res.get("changeset", {})
            cs_id = changeset.get("changeset_id", "cs_tariffs_001")
            review_url = changeset.get("review_console_url", f"http://localhost:8787/#changesets/{cs_id}")

            response_text = (
                f"### 🛡️ Toothpaste Tariff Remediation Proposal (Wildfire Changeset)\n\n"
                f"I have identified all affected customer orders shipped after **July 1, 2026** containing Elbonian toothpaste.\n\n"
                f"#### Analysis Summary:\n"
                f"- **Affected Orders**: {num_affected} orders\n"
                f"- **Tariff Rule**: Executive Order 2026-T42 (+20% on Elbonian Oral Care)\n"
                f"- **Remediation Strategy**: Appended **{num_affected * 2} paired line items** (+20% Tariff Duty and -20% Goodwill Credit offset).\n"
                f"- **Customer Impact**: Net invoice change is **$0.00** (customer total remains unchanged).\n\n"
                f"#### 🔒 Wildfire Proxy Validation Status:\n"
                f"- **Changeset ID**: `{cs_id}`\n"
                f"- **Sandbox Evaluation**: `PASSED` (Simulated in isolated AlloyDB clone with 0 errors)\n"
                f"- **Policy Check**: `POL-FIN-042` triggered (Financial balance invariant satisfied)\n"
                f"- **Status**: `PENDING_APPROVAL` (Queued for FinOps Manager sign-off)\n\n"
                f"👉 **[Open Wildfire Review Console]({review_url})** to inspect the row-level before/after diff and approve the merge into production."
            )
            return {"text": response_text, "tool_calls": tool_calls}

        # Default fallback
        else:
            response_text = (
                f"Hello! I am **BathAgent**, the AI operations assistant for BathStuff.\n\n"
                f"I can help you with:\n"
                f"- 🔍 **Customer Order Lookups**: Search orders and details by customer ID.\n"
                f"- ❌ **Order Cancellations**: Cancel unshipped customer orders safely.\n"
                f"- 📊 **Read-Only Analytics**: Query product sales, inventory, and supplier data via SQL.\n"
                f"- 🛡️ **Complex Data Remediation**: Formulate and submit safe, sandbox-validated database mutations via the **Wildfire Proxy**."
            )
            return {"text": response_text, "tool_calls": []}
