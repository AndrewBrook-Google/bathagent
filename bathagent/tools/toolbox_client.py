"""MCP Toolbox for Databases client and direct tool execution bindings.
Implements prebuilt tools for customer orders and strict read-only SQL execution.
"""

import re
import sqlite3
from typing import Any, Dict, List, Optional

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

from bathagent.config import settings


class ToolboxClient:
    """Provides access to database tools exposed via MCP Toolbox for Databases."""

    def __init__(self):
        self.use_sqlite = settings.USE_SQLITE or not PSYCOPG2_AVAILABLE
        self.sqlite_path = settings.SQLITE_PATH

    def _get_connection(self):
        if self.use_sqlite:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            return conn
        return psycopg2.connect(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            dbname=settings.DB_NAME,
        )

    def lookup_customer_orders(self, customer_id: int) -> Dict[str, Any]:
        """Look up all orders for a specific customer by their customer ID."""
        query = """
            SELECT 
                o.order_id,
                o.customer_id,
                c.full_name as customer_name,
                c.email,
                o.order_date,
                o.ship_date,
                o.status,
                COUNT(oi.order_item_id) as total_items,
                COALESCE(SUM(oi.line_total), 0) as total_amount
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            LEFT JOIN order_items oi ON o.order_id = oi.order_id
            WHERE o.customer_id = %s
            GROUP BY o.order_id, o.customer_id, c.full_name, c.email, o.order_date, o.ship_date, o.status
            ORDER BY o.order_date DESC;
        """
        try:
            conn = self._get_connection()
            if self.use_sqlite:
                query_sql = query.replace("%s", "?")
                cur = conn.cursor()
                cur.execute(query_sql, (customer_id,))
                rows = [dict(r) for r in cur.fetchall()]
            else:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(query, (customer_id,))
                    rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return {"status": "success", "customer_id": customer_id, "orders_count": len(rows), "orders": rows}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_order_details(self, order_id: int) -> Dict[str, Any]:
        """Retrieve comprehensive details and line items for a specific order."""
        query = """
            SELECT 
                oi.order_item_id,
                oi.order_id,
                p.product_id,
                p.sku,
                p.name as product_name,
                s.name as supplier_name,
                s.country_of_origin,
                oi.quantity,
                oi.unit_price,
                oi.line_total,
                oi.note
            FROM order_items oi
            JOIN products p ON oi.product_id = p.product_id
            JOIN suppliers s ON p.supplier_id = s.supplier_id
            WHERE oi.order_id = %s
            ORDER BY oi.order_item_id ASC;
        """
        try:
            conn = self._get_connection()
            if self.use_sqlite:
                query_sql = query.replace("%s", "?")
                cur = conn.cursor()
                cur.execute(query_sql, (order_id,))
                items = [dict(r) for r in cur.fetchall()]
            else:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(query, (order_id,))
                    items = [dict(r) for r in cur.fetchall()]
            conn.close()
            return {"status": "success", "order_id": order_id, "items_count": len(items), "items": items}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def create_customer_order(self, customer_id: int, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create a new customer order with line items in PENDING status."""
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            
            # Find next order_id and item_id
            if self.use_sqlite:
                cur.execute("SELECT COALESCE(MAX(order_id), 1000) + 1 FROM orders")
                new_order_id = cur.fetchone()[0]
                cur.execute("SELECT COALESCE(MAX(order_item_id), 5000) + 1 FROM order_items")
                next_item_id = cur.fetchone()[0]
                
                cur.execute(
                    "INSERT INTO orders (order_id, customer_id, order_date, status) VALUES (?, ?, CURRENT_DATE, 'PENDING')",
                    (new_order_id, customer_id),
                )
                
                created_items = []
                for item in items:
                    p_id = item.get("product_id")
                    qty = item.get("quantity", 1)
                    unit_price = item.get("unit_price", 10.00)
                    line_total = round(qty * unit_price, 2)
                    note = item.get("note", "New customer order")
                    
                    cur.execute(
                        "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (next_item_id, new_order_id, p_id, qty, unit_price, line_total, note),
                    )
                    created_items.append({"order_item_id": next_item_id, "product_id": p_id, "quantity": qty, "line_total": line_total})
                    next_item_id += 1
                conn.commit()
            else:
                cur.execute("SELECT COALESCE(MAX(order_id), 1000) + 1 FROM orders")
                new_order_id = cur.fetchone()[0]
                cur.execute("SELECT COALESCE(MAX(order_item_id), 5000) + 1 FROM order_items")
                next_item_id = cur.fetchone()[0]

                cur.execute(
                    "INSERT INTO orders (order_id, customer_id, order_date, status) VALUES (%s, %s, CURRENT_DATE, 'PENDING')",
                    (new_order_id, customer_id),
                )
                created_items = []
                for item in items:
                    p_id = item.get("product_id")
                    qty = item.get("quantity", 1)
                    unit_price = item.get("unit_price", 10.00)
                    line_total = round(qty * unit_price, 2)
                    note = item.get("note", "New customer order")

                    cur.execute(
                        "INSERT INTO order_items VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (next_item_id, new_order_id, p_id, qty, unit_price, line_total, note),
                    )
                    created_items.append({"order_item_id": next_item_id, "product_id": p_id, "quantity": qty, "line_total": line_total})
                    next_item_id += 1
                conn.commit()

            cur.close()
            conn.close()
            return {
                "status": "success",
                "message": f"Order #{new_order_id} successfully created in PENDING status.",
                "order_id": new_order_id,
                "customer_id": customer_id,
                "items": created_items,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def cancel_unshipped_order(self, order_id: int, reason: str = "") -> Dict[str, Any]:
        """Cancel an order only if it has not yet shipped or delivered."""
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            
            # Check current order status
            if self.use_sqlite:
                cur.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
                row = cur.fetchone()
                if not row:
                    conn.close()
                    return {"status": "error", "error": f"Order #{order_id} not found."}
                current_status = row[0]
                
                if current_status in ("SHIPPED", "DELIVERED"):
                    conn.close()
                    return {
                        "status": "rejected",
                        "error": f"Cannot cancel Order #{order_id} because its current status is '{current_status}'. Orders can only be cancelled prior to shipping.",
                    }
                
                cur.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
                conn.commit()
            else:
                cur.execute("SELECT status FROM orders WHERE order_id = %s", (order_id,))
                row = cur.fetchone()
                if not row:
                    conn.close()
                    return {"status": "error", "error": f"Order #{order_id} not found."}
                current_status = row[0]

                if current_status in ("SHIPPED", "DELIVERED"):
                    conn.close()
                    return {
                        "status": "rejected",
                        "error": f"Cannot cancel Order #{order_id} because its current status is '{current_status}'. Orders can only be cancelled prior to shipping.",
                    }

                cur.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = %s", (order_id,))
                conn.commit()

            cur.close()
            conn.close()
            return {
                "status": "success",
                "message": f"Order #{order_id} has been successfully CANCELLED.",
                "order_id": order_id,
                "previous_status": current_status,
                "new_status": "CANCELLED",
                "reason": reason or "Customer requested cancellation prior to shipping",
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def execute_sql_read_only(self, query: str) -> Dict[str, Any]:
        """Execute a read-only SQL query against the BathStuff database.
        
        Strictly blocks any mutating or DDL keywords.
        """
        clean_query = query.strip()
        
        # Security validation: Reject any query containing mutating keywords
        forbidden_patterns = [
            r"\bINSERT\b",
            r"\bUPDATE\b",
            r"\bDELETE\b",
            r"\bDROP\b",
            r"\bALTER\b",
            r"\bTRUNCATE\b",
            r"\bCREATE\b",
            r"\bGRANT\b",
            r"\bREVOKE\b",
            r"\bREPLACE\b",
        ]
        for pattern in forbidden_patterns:
            if re.search(pattern, clean_query, re.IGNORECASE):
                return {
                    "status": "security_violation",
                    "error": (
                        f"Mutating keyword detected matching '{pattern}'. "
                        "The execute_sql_read_only tool only permits SELECT statements. "
                        "For mutating or bulk data updates, you must use Wildfire's propose_sql tool."
                    ),
                }

        try:
            conn = self._get_connection()
            if self.use_sqlite:
                cur = conn.cursor()
                cur.execute(clean_query)
                cols = [desc[0] for desc in cur.description] if cur.description else []
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            else:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(clean_query)
                    rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return {
                "status": "success",
                "query": clean_query,
                "rows_returned": len(rows),
                "data": rows[:100],  # cap at 100 rows for display
            }
        except Exception as e:
            return {"status": "error", "query": clean_query, "error": str(e)}
