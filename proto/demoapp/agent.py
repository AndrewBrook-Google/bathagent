"""BathAgent enterprise AI assistant powered by Gemini 3.7 Flash & Wildfire.

Connects to Google Cloud Vertex AI using Application Default Credentials (ADC).
Operates strictly over AlloyDB PostgreSQL (bathstuff-prod) through Wildfire
transaction safety protocol (Path A: Propose-SQL over MCP).

Features robust JSON parsing, automatic exponential backoff on 429 errors up to 5 attempts,
and clean error emission with full traceback preservation in logs and JS console.
"""
import asyncio
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from typing import Any, Dict, List, Tuple

import httpx

# Ensure mcp from venv is accessible if running under standard python3
_VENV_SITE = pathlib.Path(__file__).resolve().parents[4] / "wildfire-demo-0816" / ".venv" / "lib" / "python3.13" / "site-packages"
if _VENV_SITE.exists() and str(_VENV_SITE) not in sys.path:
    sys.path.insert(0, str(_VENV_SITE))

import alloydb_client

WF_CONSOLE = os.environ.get("WF_URL", "http://127.0.0.1:8787")
WF_MCP_URL = os.environ.get("WF_MCP_URL", f"{WF_CONSOLE}/mcp")
WF_MCP_TOKEN = os.environ.get("WF_MCP_TOKEN", "wildfire-bathstuff-token")

PROJECT = os.environ.get("WF_PROJECT", "andybrook-playground")
LOCATION = os.environ.get("WF_LOCATION", "global")
MODEL = os.environ.get("WF_MODEL", "gemini-3.7-flash")
MAX_STEPS = int(os.environ.get("WF_MAX_STEPS", "25"))


class ResourceExhaustedError(Exception):
    """Raised when Gemini returns 429 after 5 retry attempts."""
    pass


# Watchlist for changesets pending human review
WATCH: List[Dict[str, Any]] = []

SYSTEM_BATHAGENT = """You are BathAgent, BathStuff's enterprise AI assistant for customer service, order adjustments, catalog management, and billing inquiry.
You operate on an AlloyDB PostgreSQL database (`bathstuff-prod`) via the Wildfire transaction safety protocol (Path A: Propose-SQL).

STRICT GROUNDING & ANTI-HALLUCINATION POLICY:
1. You are strictly grounded in the database. You MUST ONLY respond with factual information retrieved directly from the database tools.
2. NEVER guess, fabricate, hallucinate, extrapolate, or invent customer details, order IDs, product names, SKUs, prices, stock quantities, tariffs, shipping costs, or order statuses.
3. Every single fact, number, date, and status in your final answer MUST be supported by a `query` observation in this turn.
4. If you do not know something or need information to answer the user's question, call `query` immediately to look it up in `bathstuff-prod`.
5. If a requested order, customer, product, or rule does not exist in the database (or a query returns empty results), state clearly and truthfully that the record was not found in the database. Do NOT make up hypothetical details.
6. When calculating totals, taxes, shipping, fees, or line items, strictly compute them based on the actual line items, rates, and records from the database.

DATABASE SCHEMA & COLUMN DETAILS:
- `customers`:
  - `customer_id` (INT PRIMARY KEY): Unique customer identifier.
  - `full_name` (TEXT): Customer's complete legal name.
  - `email` (TEXT): Customer contact email address.
  - `shipping_country` (TEXT): ISO country code/name for fulfillment (e.g., 'USA', 'CAN', 'GBR', 'FRA', 'DEU', 'AUS', 'JPN').
- `products`:
  - `product_id` (INT PRIMARY KEY): Unique catalog product ID.
  - `sku` (TEXT): Stock keeping unit identifier.
  - `name` (TEXT): Product title / name.
  - `supplier_id` (INT FK -> suppliers): Supplier who manufactures the product.
  - `tax_code_id` (INT FK -> tax_codes): Tax categorization code.
- `orders`:
  - `order_id` (INT PRIMARY KEY): Unique order number.
  - `customer_id` (INT FK -> customers): Customer who placed the order.
  - `order_date` (TIMESTAMPTZ): Timestamp when the order was placed.
  - `ship_date` (TIMESTAMPTZ, nullable): Timestamp when items were shipped (NULL if not yet shipped).
  - `status` (TEXT): Order lifecycle status (`PENDING`, `PROCESSING`, `DELIVERED`, `CANCELLED`).
  - `total_price` (NUMERIC): Grand total sum of all order items (products + taxes + shipping + fees - credits).
  - `amount_paid` (NUMERIC): Amount already collected/paid by customer.
  - `total_outstanding` (NUMERIC): Unpaid balance (`total_price - amount_paid`, min 0.00).
- `order_items`:
  - `order_item_id` (INT PRIMARY KEY): Unique line item ID.
  - `order_id` (INT FK -> orders): Order this item belongs to.
  - `product_id` (INT FK -> products, nullable): Catalog product ID (populated for `PRODUCT` items; NULL for `TAX`, `SHIPPING`, `FEE`, `CREDIT`).
  - `quantity` (INT): Quantity purchased (typically 1 for tax/shipping/fees/credits).
  - `unit_price` (NUMERIC): Per-unit price or base charge.
  - `line_total` (NUMERIC): Total line cost (`quantity * unit_price`).
  - `item_type` (TEXT): Line item classification (`PRODUCT`, `TAX`, `SHIPPING`, `FEE`, `CREDIT`).
  - `item_description` (TEXT, nullable): Explanatory text for non-product items (e.g. "Sales Tax (7.0%)", "Standard Shipping (CAN)", "Rush Handling", "$20 Appeasement Credit"; NULL for standard products).
- `suppliers`:
  - `supplier_id` (INT PRIMARY KEY): Unique supplier ID.
  - `name` (TEXT): Supplier vendor name.
  - `country_of_origin` (TEXT): Country where supplier manufactures goods (e.g., 'USA', 'CAN', 'MEX', 'CHN', 'VNM', 'IND', 'GBR', 'DEU', 'FRA', 'ITA', 'JPN').
- `product_pricing_history`:
  - `pricing_id` (INT PRIMARY KEY): Pricing record ID.
  - `product_id` (INT FK -> products): Product ID.
  - `unit_price` (NUMERIC): Historic or current unit price.
  - `effective_start` (TIMESTAMPTZ): Beginning of price window.
  - `effective_end` (TIMESTAMPTZ, nullable): Expiration of price window (NULL if currently active).
- `tax_codes`:
  - `tax_code_id` (INT PRIMARY KEY): Tax code ID.
  - `code_name` (TEXT): Code label (e.g. 'STANDARD_GOODS', 'LUXURY_GOODS', 'ESSENTIALS', 'DIGITAL_SERVICES', 'HAZMAT').
  - `category` (TEXT): Broad classification category.
  - `default_rate` (NUMERIC): Base percentage tax rate (e.g. 0.07 for 7%).
- `tax_eligibility_rules`:
  - `rule_id` (INT PRIMARY KEY): Tariff rule ID.
  - `country_of_origin` (TEXT): Source country targeted by trade rule.
  - `category` (TEXT): Product category targeted.
  - `additional_tariff_rate` (NUMERIC): Added tariff rate (e.g. 0.05 for 5%).
  - `effective_date` (TIMESTAMPTZ): Start timestamp of tariff applicability.
  - `note` (TEXT): Regulatory notes or policy reference.

BUSINESS LOGIC & DOMAIN RULES:

1. Order Status Lifecycle:
   - `PENDING`: Order has been placed and received by the system, awaiting warehouse processing. `ship_date` is NULL. Eligible for modifications, address changes, item additions/cancellations.
   - `PROCESSING`: Order is currently being picked, packed, or prepared for dispatch in the warehouse. `ship_date` is NULL. Address adjustments or cancellations may require expedited supervisor review.
   - `DELIVERED`: Order has been fulfilled and delivered to the recipient. `ship_date` contains the confirmed shipment timestamp. Cannot be cancelled directly; returns, refunds, or credits should be issued instead.
   - `CANCELLED`: Order was cancelled. `ship_date` is NULL. No items are shipped.

2. Line Item Types & Semantics (`order_items.item_type`):
   - `PRODUCT`: Physical item from the catalog. Must have a valid `product_id`. `item_description` is NULL. `line_total = quantity * unit_price`.
   - `TAX`: Sales tax computed from product line items based on `tax_codes.default_rate` plus applicable `tax_eligibility_rules` tariffs. `product_id` is NULL. `item_description` specifies the tax breakdown (e.g. "Sales Tax (7.0%)").
   - `SHIPPING`: Delivery fee calculated based on item count and destination country relative to US baseline. `product_id` is NULL. `item_description` specifies the shipping method/destination (e.g. "Standard Shipping (CAN)").
   - `FEE`: Surcharge for special handling or expedited services (e.g., $15.00 "Rush Handling"). `product_id` is NULL. `item_description` explains the fee.
   - `CREDIT`: Store credit, promotional discount, or appeasement adjustment. `product_id` is NULL. Subtracted when computing grand total. `item_description` explains the credit reason.

3. Financial Invariants & Calculations:
   - `total_price` = SUM(`line_total` for `PRODUCT`, `TAX`, `SHIPPING`, `FEE`) - SUM(`line_total` for `CREDIT`).
   - `total_outstanding` = `total_price - amount_paid` (if `amount_paid >= total_price`, `total_outstanding = 0.00`).
   - When modifying an order (e.g., adding/removing products, applying discounts/credits, waiving fees), you must recalculate taxes/shipping if applicable and update `total_price` and `total_outstanding` in `orders` so totals remain strictly consistent.

4. International Taxation & Tariff Rules:
   - Standard tax rate is determined by `tax_codes.default_rate` for the product's assigned `tax_code_id`.
   - If a product's supplier `country_of_origin` and `category` match an active `tax_eligibility_rules` entry as of the `order_date`, the `additional_tariff_rate` is added to the tax rate.

WORKFLOW RULES (Path A: Propose-SQL):
- Read queries: Call `query` with standard SQL. Use `CURRENT_TIMESTAMP` for date/time comparisons.
- Write modifications:
  1. Always `query` the target records first to verify exact current state.
  2. Formulate the required DML statement(s) (`INSERT`, `UPDATE`, `DELETE`).
  3. Call `propose_sql` with:
     - `statements`: a list of SQL DML strings.
     - `note`: a clear, specific description of what you are changing and the business reason. Reviewers evaluate the observed changes against this note.
- Interpret `propose_sql` outcomes truthfully:
  - `merged`: The change was approved and applied immediately to `bathstuff-prod`. Confirm completion to user.
  - `pending_human`: The change requires human supervisor review in Wildfire. Inform the user that it has been submitted for review.
  - `rejected` or `merge_failed`: Explain the conflict or policy rejection reason truthfully.
- Final answer: Call `done` with a concise, customer-friendly, 100% grounded response.

TOOL PROTOCOL:
Output ONE JSON object per step (valid RFC 8259 JSON, all quotes escaped):
{"thought": "reasoning grounded in facts...", "action": "query", "args": {"sql": "SELECT ..."}}
{"thought": "reasoning...", "action": "propose_sql", "args": {"statements": ["UPDATE orders SET ...", "INSERT INTO order_items ..."], "note": "Adjust order 160 with 20% tariff and goodwill credit"}}
{"thought": "reasoning...", "action": "get_changeset", "args": {"changeset_id": "cs_..."}}
{"thought": "reasoning...", "action": "cancel_changeset", "args": {"changeset_id": "cs_...", "reason": "..."}}
{"thought": "reasoning...", "action": "remember", "args": {"key": "...", "value": "..."}}
{"thought": "reasoning...", "action": "done", "args": {"text": "Factual response based on query observations."}}
"""

_TOKEN_CACHE: Dict[str, Any] = {"token": None, "expiry": 0.0}


def _get_token() -> str:
    now = time.monotonic()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expiry"]:
        return _TOKEN_CACHE["token"]

    token = subprocess.run(
        ["gcloud", "auth", "application-default", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expiry"] = now + 1800.0
    return token


async def _async_mcp_call(tool: str, args: Dict[str, Any], timeout: float = 60.0) -> Any:
    from mcp import ClientSession
    try:
        from mcp.client.streamable_http import streamable_http_client as http_client
    except ImportError:
        from mcp.client.streamable_http import streamablehttp_client as http_client

    headers = {"Authorization": f"Bearer {WF_MCP_TOKEN}"}
    async with http_client(WF_MCP_URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await asyncio.wait_for(s.call_tool(tool, args), timeout=timeout)
    body = res.structuredContent
    if body is None:
        if res.content:
            first_item = res.content[0]
            txt = getattr(first_item, "text", "")
            if txt:
                try:
                    body = json.loads(txt)
                except Exception:
                    body = txt
            else:
                body = {}
        else:
            body = {}
    return body.get("result", body) if isinstance(body, dict) else body


def mcp_call(tool: str, args: Dict[str, Any], timeout: float = 60.0) -> Any:
    try:
        return asyncio.run(_async_mcp_call(tool, args, timeout=timeout))
    except Exception as e:
        return {"error": f"Wildfire MCP error ({tool}): {e}"}


def _api(path: str, body: Any = None) -> Any:
    req = urllib.request.Request(
        WF_CONSOLE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    return json.load(urllib.request.urlopen(req, timeout=120))


def _parse_json(raw_text: str) -> Dict[str, Any]:
    """Robustly extract and parse JSON from model output."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        extracted = text[first_brace : last_brace + 1]
        try:
            return json.loads(extracted)
        except Exception:
            pass

        # Try removing unescaped control characters
        try:
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", extracted)
            return json.loads(cleaned)
        except Exception:
            pass

        # Try fixing unescaped single backslashes in strings/paths
        try:
            cleaned_slashes = re.sub(r'(?<!\\)\\(?!["\\/bfnrtu])', r"\\\\", extracted)
            return json.loads(cleaned_slashes)
        except Exception:
            pass

    return json.loads(text)


def _gemini_call(system_prompt: str, prompt: str) -> Dict[str, Any]:
    """Invoke Gemini with JSON mode, retrying on 429 with exponential backoff up to 5 attempts."""
    if LOCATION == "global":
        url = f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}:generateContent"
    else:
        url = f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}:generateContent"

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ],
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.0,
        },
    }

    max_retries = 5
    for attempt in range(max_retries):
        try:
            token = _get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            resp = httpx.post(url, json=payload, headers=headers, timeout=35.0)

            if resp.status_code == 200:
                data = resp.json()
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                try:
                    return _parse_json(raw_text)
                except Exception as e:
                    print(f"[BathAgent ERROR] JSON decode error: {e}\nRaw model text:\n{raw_text}")
                    if attempt < max_retries - 1:
                        time.sleep(1.0)
                        continue
                    raise

            if resp.status_code == 429 or "RESOURCE_EXHAUSTED" in resp.text:
                delay = (2 ** attempt) + random.uniform(0.1, 0.4)
                print(f"[BathAgent] 429 Resource Exhausted on attempt {attempt + 1}/{max_retries}. Backing off for {delay:.2f}s... (Error: {resp.text[:120]})")
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    continue
                else:
                    print(f"[BathAgent ERROR] 429 Resource Exhausted retry limit ({max_retries}) exceeded: {resp.text}")
                    raise ResourceExhaustedError("The AI reasoning service is currently at capacity (429: Resource Exhausted). Please try again shortly.")

            # Other HTTP error
            err_msg = f"Vertex AI API error ({resp.status_code}): {resp.text[:200]}"
            print(f"[BathAgent ERROR] {err_msg}")
            raise RuntimeError(err_msg)

        except ResourceExhaustedError:
            raise
        except httpx.RequestError as e:
            delay = (2 ** attempt) + random.uniform(0.1, 0.4)
            print(f"[BathAgent] HTTP Request error on attempt {attempt + 1}/{max_retries}: {e}. Retrying in {delay:.2f}s...")
            if attempt < max_retries - 1:
                time.sleep(delay)
                continue
            raise RuntimeError(f"Connection to reasoning backend failed: {e}")
        except Exception as e:
            print(f"[BathAgent ERROR] Model invocation exception: {e}")
            if attempt < max_retries - 1:
                time.sleep(1.0)
                continue
            raise

    raise ResourceExhaustedError("The AI reasoning service is currently at capacity (429: Resource Exhausted). Please try again shortly.")


class ChatSession:
    def __init__(self, role: str = "bathagent", actor: str = "bathagent-chat"):
        self.role = "bathagent"
        self.actor = actor
        self.system = SYSTEM_BATHAGENT
        self.history: List[Tuple[str, str]] = []
        self.notes: Dict[str, Any] = {}
        self.trajectory: List[Dict[str, Any]] = []

    def tool(self, action: str, args: Dict[str, Any], task: str) -> Any:
        if action == "query":
            sql = args.get("sql", "").strip()
            try:
                rows = alloydb_client.execute_sql(sql)
                obs = {"columns": list(rows[0].keys()) if rows else [], "rows": rows, "count": len(rows)}
            except Exception as e:
                obs = {"error": f"Query execution failed: {e}"}
        elif action == "propose_sql":
            stmts = args.get("statements", [])
            if isinstance(stmts, str):
                stmts = [stmts]
            note = args.get("note", task[:200])
            policy = args.get("policy", "")
            obs = mcp_call("propose_sql", {"statements": stmts, "note": note, "policy": policy or None})
            if isinstance(obs, dict) and obs.get("status") == "pending_human":
                cid = obs.get("id") or obs.get("changeset_id")
                if cid:
                    WATCH.append(
                        {
                            "cid": cid,
                            "note": note[:80],
                            "actor": self.actor,
                            "session": self,
                        }
                    )
        elif action == "get_changeset":
            cid = args.get("changeset_id", "")
            obs = mcp_call("get_changeset", {"changeset_id": cid})
        elif action == "cancel_changeset":
            cid = args.get("changeset_id", "")
            reason = args.get("reason", "")
            obs = mcp_call("cancel_changeset", {"changeset_id": cid, "reason": reason})
        elif action == "remember":
            self.notes[args.get("key", "note")] = args.get("value", "")
            obs = {"ok": True, "notes": self.notes}
        elif action == "done":
            obs = {"ok": True}
        else:
            obs = {"error": f"unknown action: {action}"}

        self.trajectory.append(
            {
                "t": "tool",
                "name": action,
                "args": args,
                "obs": json.dumps(obs, default=str)[:400],
            }
        )
        return obs

    def run_turn_stream(self, user_msg: str):
        self.history.append(("user", user_msg))
        self.trajectory.append({"t": "user", "text": user_msg})
        steps: List[Dict[str, Any]] = []

        traj_lines: List[str] = []
        pending_cid = None

        for step_idx in range(1, MAX_STEPS + 1):
            history_text = "\n".join(f"{r.upper()}: {t}" for r, t in self.history[-6:])
            traj_text = "\n\n".join(traj_lines) if traj_lines else "(No tool actions taken yet in this turn)"

            prompt = (
                f"CURRENT NOTES: {json.dumps(self.notes)}\n\n"
                f"CONVERSATION HISTORY:\n{history_text}\n\n"
                f"CURRENT TURN ACTIONS & OBSERVATIONS:\n{traj_text}\n\n"
                f"Determine the next action for Step {step_idx}. Remember: ONLY state facts from query observations. Do NOT hallucinate. Respond with a single JSON object."
            )

            try:
                step = _gemini_call(self.system, prompt)
            except ResourceExhaustedError as e:
                tb = traceback.format_exc()
                print(f"[BathAgent App Log] Traceback for ResourceExhaustedError:\n{tb}")
                succinct_msg = str(e)
                err_step = {"action": "error", "observation": succinct_msg, "traceback": tb}
                steps.append(err_step)
                self.history.append(("agent", succinct_msg))
                self.trajectory.append({"t": "agent", "text": succinct_msg})
                yield {"type": "error", "step": err_step, "final": succinct_msg, "steps": steps}
                return
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[BathAgent App Log] Traceback for LLM Error:\n{tb}")
                succinct_msg = f"I encountered an error communicating with the reasoning backend: {e}"
                err_step = {"action": "error", "observation": succinct_msg, "traceback": tb}
                steps.append(err_step)
                self.history.append(("agent", succinct_msg))
                self.trajectory.append({"t": "agent", "text": succinct_msg})
                yield {"type": "error", "step": err_step, "final": succinct_msg, "steps": steps}
                return

            action = step.get("action", "done")
            args = step.get("args", {})
            thought = step.get("thought", "")

            if thought:
                self.trajectory.append({"t": "thought", "text": thought})

            if action == "done":
                done_step = {"thought": thought, "action": "done", "args": args}
                steps.append(done_step)
                final = args.get("text", "Done.")
                if pending_cid and not any(w in final.lower() for w in ("approval", "review", "pending", "approve")):
                    final += (
                        f" (Note: this change [{pending_cid[:8]}] is pending human reviewer approval; "
                        f"I will confirm once decided.)"
                    )
                self.history.append(("agent", final))
                self.trajectory.append({"t": "agent", "text": final})
                yield {"type": "done", "step": done_step, "final": final, "steps": steps}
                return

            # Yield step_start in real time so UI shows thought and action immediately
            yield {
                "type": "step_start",
                "step_idx": step_idx,
                "thought": thought,
                "action": action,
                "args": args,
            }

            obs = self.tool(action, args, user_msg)
            if action == "propose_sql" and isinstance(obs, dict):
                if obs.get("status") == "pending_human":
                    pending_cid = obs.get("id") or obs.get("changeset_id")
                elif obs.get("status") == "merged":
                    pending_cid = None

            rec = {
                "thought": thought,
                "action": action,
                "args": args,
                "observation": obs,
            }
            steps.append(rec)

            # Yield step_done in real time so UI updates with tool observation
            yield {
                "type": "step_done",
                "step_idx": step_idx,
                "thought": thought,
                "action": action,
                "args": args,
                "observation": obs,
            }

            obs_str = json.dumps(obs, default=str)
            if len(obs_str) > 800:
                obs_str = obs_str[:800] + "... (truncated)"
            traj_lines.append(
                f"Step {step_idx}:\n"
                f"Thought: {thought}\n"
                f"Action: {action}(args={json.dumps(args)})\n"
                f"Observation: {obs_str}"
            )

        final = "I reached the maximum reasoning steps for this turn. Please check the Wildfire console for the latest status."
        self.history.append(("agent", final))
        self.trajectory.append({"t": "agent", "text": final})
        yield {"type": "done", "final": final, "steps": steps}

    def run_turn(self, user_msg: str) -> Tuple[List[Dict[str, Any]], str]:
        steps = []
        final = ""
        for ev in self.run_turn_stream(user_msg):
            if ev.get("type") in ("done", "error"):
                steps = ev.get("steps", steps)
                final = ev.get("final", final)
        return steps, final
