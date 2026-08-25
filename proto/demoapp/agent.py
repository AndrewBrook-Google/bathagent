"""BathAgent enterprise AI assistant powered by Gemini 3.7 Flash & Wildfire.

Connects to Google Cloud Vertex AI using Application Default Credentials (ADC).
Operates strictly over AlloyDB PostgreSQL (bathstuff-prod) through isolated Wildfire sandbox
branches, policy evaluations, and automatic writeback merges.

Features robust JSON parsing, automatic exponential backoff on 429 errors up to 5 attempts,
and clean error emission with full traceback preservation in logs and JS console.
"""
import json
import os
import random
import re
import subprocess
import time
import traceback
import urllib.request
from typing import Any, Dict, List, Tuple

import httpx

CONSOLE = "http://127.0.0.1:8777"
PROJECT = os.environ.get("WF_PROJECT", "andybrook-playground")
LOCATION = os.environ.get("WF_LOCATION", "global")
MODEL = os.environ.get("WF_MODEL", "gemini-3.7-flash")
MAX_STEPS = 10


class ResourceExhaustedError(Exception):
    """Raised when Gemini returns 429 after 5 retry attempts."""
    pass


# Watchlist for changesets pending human review
WATCH: List[Dict[str, Any]] = []

SYSTEM_BATHAGENT = """You are BathAgent, BathStuff's enterprise AI assistant for customer service, order adjustments, and catalog management.
You operate on an AlloyDB PostgreSQL database (`bathstuff-prod`) via the Wildfire transaction safety protocol.

STRICT GROUNDING & ANTI-HALLUCINATION POLICY:
1. You are strictly grounded in the database. You MUST ONLY respond with factual information retrieved directly from the database tools.
2. NEVER guess, fabricate, hallucinate, extrapolate, or invent customer details, order IDs, product names, SKUs, prices, stock quantities, tariffs, or order statuses.
3. Every single fact, number, and status in your final answer MUST be supported by a `query` observation in this turn.
4. If you do not know something or need information to answer the user's question, call `query` immediately to look it up in `bathstuff-prod`.
5. If a requested order, customer, product, or rule does not exist in the database (or a query returns empty results), state clearly and truthfully that the record was not found in the database. Do NOT make up hypothetical details.
6. When calculating totals, discounts, or line items, strictly compute them based on the actual unit prices and quantities from the database.

DATABASE SCHEMA:
- `customers` (customer_id INT PRIMARY KEY, full_name TEXT, email TEXT, shipping_country TEXT)
- `products` (product_id INT PRIMARY KEY, sku TEXT, name TEXT, supplier_id INT, tax_code_id INT)
- `orders` (order_id INT PRIMARY KEY, customer_id INT, order_date TIMESTAMPTZ, ship_date TIMESTAMPTZ, status TEXT, total_price NUMERIC, amount_paid NUMERIC, total_outstanding NUMERIC)
- `order_items` (order_item_id INT PRIMARY KEY, order_id INT, product_id INT, quantity INT, unit_price NUMERIC, line_total NUMERIC)
- `suppliers` (supplier_id INT PRIMARY KEY, name TEXT, country_of_origin TEXT)
- `product_pricing_history` (pricing_id INT PRIMARY KEY, product_id INT, unit_price NUMERIC, effective_start TIMESTAMPTZ, effective_end TIMESTAMPTZ)
- `tax_codes` (tax_code_id INT PRIMARY KEY, code_name TEXT, category TEXT, default_rate NUMERIC)
- `tax_eligibility_rules` (rule_id INT PRIMARY KEY, country_of_origin TEXT, category TEXT, additional_tariff_rate NUMERIC, effective_date TIMESTAMPTZ, note TEXT)

WORKFLOW RULES:
- Read queries: Call `query` with standard SQL. Use `CURRENT_TIMESTAMP` for date/time comparisons.
- Write modifications:
  1. Always `query` the target records first to verify exact current state.
  2. Call `start_write_session` to detach into an isolated sandbox branch.
  3. Execute `write` with your DML SQL (`UPDATE`, `INSERT`, `DELETE`).
  4. Call `submit` to propose the changeset to the Wildfire policy engine.
- Interpret `submit` outcomes truthfully:
  - `auto-merged`: The change was approved and applied to `bathstuff-prod`. You may confirm completion.
  - `pending_human`: The change requires human approval. Inform the user that it has been submitted for review.
  - `rejected` or `merge_failed`: Explain the conflict or policy rejection.
- Final answer: Call `done` with a concise, customer-friendly, 100% grounded response.

TOOL PROTOCOL:
Output ONE JSON object per step (valid RFC 8259 JSON, all quotes escaped):
{"thought": "reasoning grounded in facts...", "action": "query", "args": {"sql": "SELECT ..."}}
{"thought": "reasoning...", "action": "start_write_session", "args": {}}
{"thought": "reasoning...", "action": "write", "args": {"sql": "UPDATE/INSERT ..."}}
{"thought": "reasoning...", "action": "submit", "args": {}}
{"thought": "reasoning...", "action": "resync", "args": {}}
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


def _api(path: str, body: Any = None) -> Any:
    req = urllib.request.Request(
        CONSOLE + path,
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
        self.reader_id: Any = None

    def _ensure_reader(self, task: str):
        if self.reader_id:
            st = _api("/api/state")
            live = {b["id"]: b["status"] for b in st.get("branches", [])}
            if live.get(self.reader_id) in ("attached", "sandbox", "proposed"):
                return
        r = _api("/api/branches", {"actor": self.actor, "role": self.role})
        self.reader_id = r["reader_id"]

    def tool(self, action: str, args: Dict[str, Any], task: str) -> Any:
        self._ensure_reader(task)
        rid = self.reader_id

        if action == "query":
            r = _api(f"/api/branches/{rid}/exec", {"sql": args.get("sql", "")})
            obs = r.get("rows", r)
        elif action == "start_write_session":
            obs = _api(f"/api/branches/{rid}/detach", {})
        elif action == "write":
            obs = _api(f"/api/branches/{rid}/exec", {"sql": args.get("sql", "")})
        elif action == "submit":
            obs = _api(
                f"/api/branches/{rid}/propose",
                {"task": task[:200], "trajectory": self.trajectory[-80:]},
            )
            if obs.get("status") == "pending_human" and obs.get("changeset_id"):
                WATCH.append(
                    {
                        "cid": obs["changeset_id"],
                        "note": task[:80],
                        "actor": self.actor,
                        "session": self,
                    }
                )
        elif action == "resync":
            obs = _api(f"/api/branches/{rid}/resync", {})
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

    def run_turn(self, user_msg: str) -> Tuple[List[Dict[str, Any]], str]:
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
                steps.append({"action": "error", "observation": succinct_msg, "traceback": tb})
                self.history.append(("agent", succinct_msg))
                self.trajectory.append({"t": "agent", "text": succinct_msg})
                return steps, succinct_msg
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[BathAgent App Log] Traceback for LLM Error:\n{tb}")
                succinct_msg = f"I encountered an error communicating with the reasoning backend: {e}"
                steps.append({"action": "error", "observation": succinct_msg, "traceback": tb})
                self.history.append(("agent", succinct_msg))
                self.trajectory.append({"t": "agent", "text": succinct_msg})
                return steps, succinct_msg

            action = step.get("action", "done")
            args = step.get("args", {})
            thought = step.get("thought", "")

            if thought:
                self.trajectory.append({"t": "thought", "text": thought})

            if action == "done":
                steps.append({"thought": thought, "action": "done", "args": args})
                final = args.get("text", "Done.")
                if pending_cid and not any(w in final.lower() for w in ("approval", "review", "pending", "approve")):
                    final += (
                        f" (Note: this change [{pending_cid[:8]}] is pending human reviewer approval; "
                        f"I will confirm once decided.)"
                    )
                self.history.append(("agent", final))
                self.trajectory.append({"t": "agent", "text": final})
                return steps, final

            obs = self.tool(action, args, user_msg)
            if action == "submit" and isinstance(obs, dict):
                if obs.get("status") == "pending_human":
                    pending_cid = obs.get("changeset_id")
                elif obs.get("status") == "auto-merged":
                    pending_cid = None

            rec = {
                "thought": thought,
                "action": action,
                "args": args,
                "observation": obs,
            }
            steps.append(rec)

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
        return steps, final
