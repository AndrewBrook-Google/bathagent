"""Direct AlloyDB client for BathStuff cluster (bathstuff-prod).

Executes SQL statements over the Google Cloud AlloyDB MCP / Data API endpoint
using Application Default Credentials. Provides direct DML/DQL access to
bathstuff-prod in andybrook-playground (us-central1).
"""
import datetime
import json
import subprocess
import time
from typing import Any, Dict, List, Optional

import httpx

PROJECT = "andybrook-playground"
REGION = "us-central1"
CLUSTER = "bathstuff-prod"
INSTANCE = "bathstuff-prod-primary"
INSTANCE_URI = f"projects/{PROJECT}/locations/{REGION}/clusters/{CLUSTER}/instances/{INSTANCE}"
MCP_URL = "https://alloydb.googleapis.com/mcp"

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
    _TOKEN_CACHE["expiry"] = now + 1800.0  # cache 30 mins
    return token


def execute_sql(sql: str, database: str = "postgres") -> List[Dict[str, Any]]:
    """Execute arbitrary SQL on bathstuff-prod and return list of row dicts."""
    token = _get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "datacloud.jetski",
        "Content-Type": "application/json",
    }
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "execute_sql",
            "arguments": {
                "instance": INSTANCE_URI,
                "database": database,
                "sqlStatement": sql,
            },
        },
    }

    resp = httpx.post(MCP_URL, json=req, headers=headers, timeout=30.0)
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"AlloyDB MCP RPC Error: {data['error']}")

    result = data.get("result", {})
    if result.get("isError"):
        content_txt = (result.get("content", [{}])[0].get("text", "Unknown error"))
        raise RuntimeError(f"AlloyDB execution error: {content_txt}")

    # Parse structured content or JSON text in content
    structured = result.get("structuredContent", {})
    if not structured and result.get("content"):
        try:
            structured = json.loads(result["content"][0]["text"])
        except Exception:
            pass

    sql_results = structured.get("sqlResults", [])
    if not sql_results:
        return []

    first_res = sql_results[0]
    columns = [col["name"] for col in first_res.get("columns", [])]
    rows = []
    for r in first_res.get("rows", []):
        vals = [v.get("value") for v in r.get("values", [])]
        rows.append(dict(zip(columns, vals)))
    return rows
