"""LLM reviewer — real Gemini 3.7 Flash call via Vertex AI / Gemini Enterprise API.

Modes (env WF_LLM):
  unset / "vertex" / "gemini" -> calls Gemini 3.7 Flash on Google Cloud.
  "canned" -> forces deterministic offline templates (only if explicitly set).

Fail-Safe Policy:
  If the LLM reviewer encounters any error, network timeout, or execution failure,
  the changeset is strictly REJECTED / ESCALATED TO HUMAN (verdict: 'escalate'),
  preventing unverified modifications from merging.
"""
import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional
import urllib.request

MODEL = os.environ.get("WF_MODEL", "gemini-3.7-flash")
PROJECT = os.environ.get("WF_PROJECT", "andybrook-playground")
LOCATION = os.environ.get("WF_LOCATION", "global")

_TOKEN_CACHE: Dict[str, Any] = {"token": None, "expiry": 0.0}


def _get_access_token() -> str:
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


def _evidence(cs, task):
    sample = cs.net_diff[:8] if hasattr(cs, "net_diff") and cs.net_diff else []
    sql_log = cs.sql_log if hasattr(cs, "sql_log") and cs.sql_log else []
    summary_dict = cs.summary() if hasattr(cs, "summary") else {}
    return (
        f"TASK GIVEN TO THE AGENT: {task}\n\n"
        f"AGENT'S SQL LOG ({len(sql_log)} statements, ran in an isolated sandbox):\n"
        + "\n".join("  " + (e["sql"][:220] if isinstance(e, dict) else str(e)[:220]) for e in sql_log)
        + f"\n\nCHANGESET SUMMARY: {json.dumps(summary_dict, default=str)}\n"
        f"SAMPLED DIFF ROWS (before/after per changed column):\n"
        f"{json.dumps(sample, indent=1, default=str)[:2500]}\n"
    )


def _conversation(trajectory):
    """User-visible dialogue only (task-plane evidence)."""
    lines = [
        f"  {e.get('t', 'step')}: {str(e.get('text', ''))[:220]}"
        for e in (trajectory or [])
        if isinstance(e, dict) and (e.get("t") in ("user", "agent", "confirm") or e.get("action")) and (e.get("text") or e.get("args"))
    ]
    if not lines:
        return ""
    return (
        "CONVERSATION WITH THE CUSTOMER (proposals the customer explicitly "
        "confirmed are considered authorized):\n" + "\n".join(lines[-12:]) + "\n\n"
    )


def _vertex_review(cs, task, rules, guidance, trajectory=None) -> Dict[str, Any]:
    from datetime import datetime, timezone

    token = _get_access_token()
    prompt = (
        "You are the Wildfire change reviewer for a production PostgreSQL database (BathStuff bathstuff-prod).\n"
        f"TODAY'S DATE: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC).\n"
        "A sandboxed agent proposes the database change below. Judge ONLY the data effect: "
        "does the captured diff faithfully implement the stated task (as refined by "
        "the conversation), with no unrelated, unintended, or suspicious modifications?\n\n"
        "REVIEW GUIDANCE (set by the database admin for this role):\n"
        + (guidance or "(none provided — use conservative judgement)") + "\n\n"
        "DETERMINISTIC RULES ALREADY PASSED: " + "; ".join(rules) + "\n\n"
        + _conversation(trajectory)
        + _evidence(cs, task) +
        '\nReply with STRICT JSON only: {"approve": <boolean>, "reason": "<concise, '
        '≤50 words, name the specific rows/values that informed your decision>"}'
    )

    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }).encode()

    if LOCATION == "global":
        url = f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}:generateContent"
    else:
        url = f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}:generateContent"

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.load(resp)

    raw_text = out["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(raw_text)
    return {
        "verdict": "approve" if parsed.get("approve") else "escalate",
        "reason": str(parsed.get("reason", ""))[:500],
        "source": f"LLM ({MODEL})",
    }


def _canned_review(cs, task, rules) -> Dict[str, Any]:
    s = cs.summary() if hasattr(cs, "summary") else {"ddl": False, "segments": 0, "net_diff_rows": 1, "ops": "U"}
    if s.get("ddl"):
        return {
            "verdict": "escalate",
            "source": "canned-fallback",
            "reason": f"Schema change detected ({s.get('segments', 0)}); policy requires human sign-off.",
        }
    return {
        "verdict": "approve",
        "source": "canned-fallback",
        "reason": f"{s.get('net_diff_rows', 1)} row(s) ({s.get('ops', 'U')}) consistent with task '{task[:60]}…'; no rule violated.",
    }


def llm_review(cs, task, rules, guidance="", trajectory=None) -> Dict[str, Any]:
    """Execute Wildfire validator review with strict fail-safe semantics."""
    if os.environ.get("WF_LLM") == "canned":
        return _canned_review(cs, task, rules)

    try:
        return _vertex_review(cs, task, rules, guidance, trajectory)
    except Exception as e:
        # FAIL-SAFE: If LLM reviewer fails to run, REJECT / ESCALATE to human instead of approving
        return {
            "verdict": "escalate",
            "source": "fail-safe (LLM error)",
            "reason": f"REJECTED/ESCALATED: LLM reviewer execution failed ({str(e)[:180]}). Failing safe per policy.",
        }
