"""Direct AlloyDB client for BathStuff cluster (bathstuff-prod).

Executes SQL statements over the Google Cloud AlloyDB MCP / Data API endpoint
using Application Default Credentials. Provides high-performance, direct DML/DQL
access to bathstuff-prod in andybrook-playground (us-central1).
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


def get_max_order_id() -> int:
    try:
        rows = execute_sql("SELECT COALESCE(MAX(order_id), 1500) as max_id FROM orders;")
        if rows and rows[0].get("max_id"):
            return int(rows[0]["max_id"])
    except Exception as e:
        print("Error getting max order_id from AlloyDB:", e)
    return 1500


def get_all_customers_from_alloydb() -> List[Dict[str, Any]]:
    """Query all existing customers from AlloyDB bathstuff-prod."""
    sql = """
    SELECT customer_id, full_name, email, shipping_country
    FROM customers
    ORDER BY customer_id ASC;
    """
    return execute_sql(sql)


def ensure_customer_in_alloydb(
    customer_id: int,
    full_name: str,
    email: str = "",
    shipping_country: str = "USA",
) -> bool:
    """Ensure customer exists in AlloyDB customers table; inserts if missing."""
    try:
        fn_esc = full_name.replace("'", "''")
        em_esc = (email or f"cust{customer_id}@example.com").replace("'", "''")
        sc_esc = shipping_country.replace("'", "''")
        sql = f"""
        INSERT INTO customers (customer_id, full_name, email, shipping_country)
        VALUES ({customer_id}, '{fn_esc}', '{em_esc}', '{sc_esc}')
        ON CONFLICT (customer_id) DO NOTHING;
        """
        execute_sql(sql)
        return True
    except Exception as e:
        print(f"Failed to ensure customer #{customer_id} in AlloyDB bathstuff-prod: {e}")
        return False


def insert_order_into_alloydb(
    order_id: int,
    customer_id: int,
    order_date: datetime.datetime,
    status: str,
    total_price: float,
    amount_paid: float,
    total_outstanding: float,
    items: List[Dict[str, Any]],
    customer_name: Optional[str] = None,
    customer_email: Optional[str] = None,
    customer_country: Optional[str] = None,
) -> bool:
    """Write new order and line items to AlloyDB bathstuff-prod, ensuring customer exists."""
    try:
        # 0. Ensure customer exists in customers table before creating order
        if customer_name:
            ensure_customer_in_alloydb(
                customer_id=customer_id,
                full_name=customer_name,
                email=customer_email or "",
                shipping_country=customer_country or "USA",
            )

        odate_str = order_date.strftime("%Y-%m-%d %H:%M:%S%z")

        # 1. Insert order
        sql_order = f"""
        INSERT INTO orders (order_id, customer_id, order_date, status, total_price, amount_paid, total_outstanding)
        VALUES ({order_id}, {customer_id}, '{odate_str}', '{status}', {total_price:.2f}, {amount_paid:.2f}, {total_outstanding:.2f});
        """
        execute_sql(sql_order)

        # 2. Insert order items
        for it in items:
            p_id = it["product_id"]
            qty = it["quantity"]
            uprice = it["unit_price"]
            ltotal = it["line_total"]
            sql_item = f"""
            INSERT INTO order_items (order_id, product_id, quantity, unit_price, line_total)
            VALUES ({order_id}, {p_id}, {qty}, {uprice:.2f}, {ltotal:.2f});
            """
            execute_sql(sql_item)
        return True
    except Exception as e:
        print(f"Failed to insert order #{order_id} into AlloyDB bathstuff-prod: {e}")
        return False


def update_order_status_in_alloydb(
    order_id: int,
    status: str,
    ship_date: Optional[datetime.datetime] = None,
) -> bool:
    """Update order status and ship_date in AlloyDB bathstuff-prod."""
    try:
        if ship_date:
            sdate_str = ship_date.strftime("%Y-%m-%d %H:%M:%S%z")
            sql = f"UPDATE orders SET status = '{status}', ship_date = '{sdate_str}' WHERE order_id = {order_id};"
        else:
            sql = f"UPDATE orders SET status = '{status}' WHERE order_id = {order_id};"
        execute_sql(sql)
        return True
    except Exception as e:
        print(f"Failed to update status for order #{order_id} in AlloyDB:", e)
        return False


def get_latest_orders_from_alloydb(limit: int = 50) -> List[Dict[str, Any]]:
    """Query live orders from AlloyDB bathstuff-prod."""
    sql = f"""
    SELECT o.order_id, o.customer_id, o.order_date, o.ship_date, o.status,
           o.total_price, o.amount_paid, o.total_outstanding,
           COUNT(i.order_item_id) as items_count
    FROM orders o
    LEFT JOIN order_items i ON i.order_id = o.order_id
    GROUP BY o.order_id, o.customer_id, o.order_date, o.ship_date, o.status,
             o.total_price, o.amount_paid, o.total_outstanding
    ORDER BY o.order_id DESC
    LIMIT {limit};
    """
    return execute_sql(sql)
