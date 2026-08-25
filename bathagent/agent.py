"""BathAgent - Google ADK Agent Implementation.
Coordinates conversational queries with MCP Toolbox for Databases (safe reads/canned writes)
and Wildfire Proxy (for complex, validated, and isolated SQL mutations).
Features exponential backoff on 429 rate limits, grounded tool execution, and clean error formatting.
"""

import json
import logging
import os
import random
import time
import traceback
from typing import Any, Dict, List, Optional

from bathagent.config import settings
from bathagent.tools.toolbox_client import ToolboxClient
from bathagent.tools.wildfire_client import WildfireClient

logger = logging.getLogger("bathagent")

# Import Google GenAI SDK
try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


class ResourceExhaustedError(Exception):
    """Raised when Gemini returns 429 after 5 retry attempts."""
    pass


SYSTEM_INSTRUCTION = """
You are BathAgent, the trusted AI assistant for BathStuff (the world's 5th largest health and beauty online retailer).
You serve BathStuff Customer Support representatives and FinOps Operations Analysts.
You operate on an AlloyDB PostgreSQL database (`bathstuff-prod`) via MCP Toolbox for Databases and Wildfire transaction safety.

STRICT GROUNDING & ZERO-HALLUCINATION POLICY:
1. You are strictly grounded in the database. You MUST ONLY state facts retrieved directly from the database tools.
2. NEVER guess, fabricate, hallucinate, or extrapolate customer details, order IDs, product names, SKUs, prices, or statuses.
3. Every fact, number, and status in your final answer MUST be supported by a tool observation in this turn.
4. If a customer for a new order does not exist in the database, use `ensure_customer_exists` to create them before placing or modifying the order.

TOOL ROLES:
- MCP TOOLBOX:
  * For order lookups, order details, customer creation, cancellations, and read-only analytical queries:
    - `lookup_customer_orders(customer_id)`
    - `get_order_details(order_id)`
    - `ensure_customer_exists(customer_id, full_name, email, shipping_country)`
    - `cancel_unshipped_order(order_id, reason)`
    - `create_customer_order(customer_id, items)`
    - `execute_sql_read_only(query)`
  * Note: `execute_sql_read_only` strictly rejects mutating statements (INSERT, UPDATE, DELETE, ALTER, DROP, etc.).

- WILDFIRE PROXY FOR MUTATIONS:
  * For ANY complex data mutation, bulk update, retroactive tax/tariff adjustment, or schema modification, submit it through Wildfire using `propose_sql_mutation(sql_statement, description, validation="sandbox")`.
  * Wildfire validates the change in an isolated sandbox clone, evaluates row-level diffs, verifies compliance policies, and parks the changeset for human approval if needed.
  * Always inform the user of the generated Changeset ID and status.

STYLE:
- Be clear, professional, concise, and transparent about which tools you invoke.
- When summarizing database results, use clean markdown tables.
"""


class BathAgent:
    """BathAgent orchestration engine."""

    def __init__(self):
        self.toolbox = ToolboxClient()
        self.wildfire = WildfireClient()
        self.history: List[Dict[str, Any]] = []
        
        # Initialize Gemini Client
        self.client = None
        if GENAI_AVAILABLE:
            api_key = settings.GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
            if api_key:
                self.client = genai.Client(api_key=api_key)
            else:
                project = os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("GCP_PROJECT", "andybrook-playground"))
                location = os.getenv("GEMINI_LOCATION", os.getenv("GOOGLE_CLOUD_REGION", "global"))
                try:
                    self.client = genai.Client(vertexai=True, project=project, location=location)
                except Exception as e:
                    logger.warning("Vertex AI initialization notice: %s", e)

    # -------------------------------------------------------------------------
    # Tool Bindings
    # -------------------------------------------------------------------------
    def lookup_customer_orders(self, customer_id: int) -> Dict[str, Any]:
        """Look up all orders for a specific customer by customer ID."""
        return self.toolbox.lookup_customer_orders(customer_id=customer_id)

    def get_order_details(self, order_id: int) -> Dict[str, Any]:
        """Retrieve comprehensive line item details for a specific order."""
        return self.toolbox.get_order_details(order_id=order_id)

    def ensure_customer_exists(
        self, customer_id: int, full_name: str, email: str, shipping_country: str = "US"
    ) -> Dict[str, Any]:
        """Ensures a customer record exists in the customers table, creating it if absent."""
        sql = (
            f"INSERT INTO customers (customer_id, full_name, email, shipping_country) "
            f"VALUES ({customer_id}, '{full_name}', '{email}', '{shipping_country}') "
            f"ON CONFLICT (customer_id) DO NOTHING;"
        )
        return self.wildfire.propose_sql(
            sql_statement=sql,
            description=f"Auto-create customer #{customer_id} ({full_name})",
            validation="sandbox",
        )

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
    # Execution & Conversational Turn with Exponential Backoff
    # -------------------------------------------------------------------------
    def run_prompt(self, user_prompt: str) -> Dict[str, Any]:
        """Runs a user prompt through the agent with 429 exponential backoff up to 5 attempts."""
        if not self.client:
            return {
                "text": "Gemini client is not initialized. Please ensure credentials or GEMINI_API_KEY are configured.",
                "tool_calls": [],
            }

        tools = [
            self.lookup_customer_orders,
            self.get_order_details,
            self.ensure_customer_exists,
            self.create_customer_order,
            self.cancel_unshipped_order,
            self.execute_sql_read_only,
            self.propose_sql_mutation,
            self.get_changeset_status,
            self.list_wildfire_changesets,
        ]

        max_retries = 5
        last_error = None

        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model=settings.GEMINI_MODEL,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        tools=tools,
                        temperature=0.0,
                    ),
                )
                return {
                    "text": response.text or "Done.",
                    "tool_calls": [
                        {"name": call.name, "args": call.args}
                        for call in (response.function_calls or [])
                    ],
                }
            except Exception as e:
                err_str = str(e)
                last_error = e
                # Check for 429 Resource Exhausted
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    delay = (2 ** attempt) + random.uniform(0.1, 0.4)
                    logger.warning(
                        "BathAgent 429 Resource Exhausted on attempt %d/%d. Backing off for %.2fs... (%s)",
                        attempt + 1,
                        max_retries,
                        delay,
                        err_str[:120],
                    )
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                        continue
                    else:
                        tb = traceback.format_exc()
                        logger.error("BathAgent 429 retry limit (5) exceeded:\n%s", tb)
                        return {
                            "text": "The AI reasoning service is currently at capacity (429: Resource Exhausted). Please try again shortly.",
                            "tool_calls": [],
                            "traceback": tb,
                        }
                else:
                    tb = traceback.format_exc()
                    logger.error("BathAgent execution error:\n%s", tb)
                    return {
                        "text": f"I encountered an error executing this request: {e}",
                        "tool_calls": [],
                        "traceback": tb,
                    }

        return {
            "text": "The AI reasoning service is currently at capacity (429: Resource Exhausted). Please try again shortly.",
            "tool_calls": [],
        }
