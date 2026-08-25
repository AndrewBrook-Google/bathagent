"""BathAgent Enterprise Demo App (port 8778).

Provides the interactive chat console for BathAgent.
Directly invokes Vertex AI Gemini LLM (gemini-2.5-flash) and executes database tools
through the Wildfire transaction safety platform (port 8777).
"""
import json
import os
import pathlib
import urllib.request
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent

CONSOLE = "http://127.0.0.1:8777"
app = FastAPI(title="BathAgent Enterprise Console")

AGENTS = {
    "bathagent": {
        "role": "bathagent",
        "actor": "bathagent-chat",
        "name": "BathAgent",
        "desc": "Enterprise Assistant — Customer service, order lookups, adjustments, line items & catalog",
    },
}

SESSION: agent.ChatSession = agent.ChatSession(role="bathagent", actor="bathagent-chat")


def console(path: str, body: Any = None) -> Any:
    req = urllib.request.Request(
        CONSOLE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    return json.load(urllib.request.urlopen(req, timeout=180))


# ---------------------------------------------------------------- Chat
class ChatReq(BaseModel):
    message: str
    agent: str = "bathagent"


@app.post("/api/chat")
def chat(req: ChatReq):
    global SESSION

    def event_stream():
        for event in SESSION.run_turn_stream(req.message):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class NewChatReq(BaseModel):
    agent: str = "bathagent"


@app.post("/api/chat/new")
def chat_new(req: NewChatReq = None):
    global SESSION
    if SESSION and getattr(SESSION, "reader_id", None):
        try:
            # Cleanly resync / release branch in Wildfire
            console(f"/api/branches/{SESSION.reader_id}/resync", {})
        except Exception:
            pass
    SESSION = agent.ChatSession(role="bathagent", actor="bathagent-chat")
    return {"ok": True, "agent": "bathagent", "message": "Session reset successfully"}


@app.get("/api/agents")
def get_agents():
    return AGENTS


@app.get("/api/chat/follow_ups")
def follow_ups():
    """Poll pending human reviews; emit agent follow-up messages once decided."""
    msgs, keep = [], []
    for w in agent.WATCH:
        try:
            c = console("/api/changesets/" + w["cid"])
        except Exception:
            keep.append(w)
            continue
        status = c.get("status")
        if status == "approved":
            m = console(
                f"/api/changesets/{w['cid']}/merge",
                {"merger": w.get("actor", "operator")},
            )
            status = m.get("status")
            if status == "approved":
                text = (
                    f"Update: change {w['cid'][:8]} was approved by human reviewer, but primary database "
                    f"refused merge ({m.get('merge', {}).get('detail', '')[:120]})."
                )
                msgs.append(text)
                sess = w.get("session")
                if sess is not None:
                    sess.history.append(("agent", text))
                    sess.trajectory.append({"t": "agent", "text": text})
                continue
        if status == "merged":
            text = (
                f"Good news — the reviewer approved change {w['cid'][:8]} and it has been merged. "
                f"“{w['note']}” is confirmed! ✅"
            )
        elif status == "rejected":
            text = (
                f"Update: change {w['cid'][:8]} (“{w['note']}”) was rejected by the human reviewer. "
                f"Nothing was applied to the database."
            )
        elif status == "merge_failed":
            detail = (c.get("merge_result") or {}).get("detail", "")
            text = f"Update: change {w['cid'][:8]} was approved but merge failed ({detail})."
        else:
            keep.append(w)
            continue
        msgs.append(text)
        sess = w.get("session")
        if sess is not None:
            sess.history.append(("agent", text))
            sess.trajectory.append({"t": "agent", "text": text})
    agent.WATCH[:] = keep
    return {"messages": msgs}


@app.get("/api/orders/{order_id}")
def get_order_details(order_id: int):
    try:
        import alloydb_client
        orders_res = alloydb_client.execute_sql(f"""
            SELECT o.order_id, o.customer_id, o.order_date, o.ship_date, o.status, 
                   o.total_price, o.amount_paid, o.total_outstanding,
                   c.full_name, c.email, c.shipping_country
            FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.customer_id
            WHERE o.order_id = {order_id};
        """)
        if not orders_res:
            return {"ok": False, "error": f"Order #{order_id} not found."}

        ord_info = orders_res[0]
        items_res = alloydb_client.execute_sql(f"""
            SELECT oi.order_item_id, oi.order_id, oi.product_id, oi.quantity, 
                   oi.unit_price, oi.line_total, oi.item_type, oi.item_description,
                   p.name as product_name, p.sku
            FROM order_items oi
            LEFT JOIN products p ON oi.product_id = p.product_id
            WHERE oi.order_id = {order_id}
            ORDER BY 
                CASE 
                    WHEN oi.item_type = 'PRODUCT' THEN 1
                    WHEN oi.item_type = 'FEE' THEN 2
                    WHEN oi.item_type = 'SHIPPING' THEN 3
                    WHEN oi.item_type = 'TAX' THEN 4
                    WHEN oi.item_type = 'CREDIT' THEN 5
                    ELSE 6 
                END,
                oi.order_item_id ASC;
        """)

        products, taxes, shipping, fees, credits = [], [], [], [], []
        for it in items_res:
            itype = (it.get("item_type") or "PRODUCT").upper()
            if itype == "PRODUCT":
                products.append(it)
            elif itype == "TAX":
                taxes.append(it)
            elif itype == "SHIPPING":
                shipping.append(it)
            elif itype == "FEE":
                fees.append(it)
            elif itype == "CREDIT":
                credits.append(it)
            else:
                products.append(it)

        prod_subtotal = sum(float(p.get("line_total") or 0.0) for p in products)
        tax_total = sum(float(t.get("line_total") or 0.0) for t in taxes)
        ship_total = sum(float(s.get("line_total") or 0.0) for s in shipping)
        fee_total = sum(float(f.get("line_total") or 0.0) for f in fees)
        credit_total = sum(float(c.get("line_total") or 0.0) for c in credits)

        return {
            "ok": True,
            "order": ord_info,
            "items": items_res,
            "breakdown": {
                "products": products,
                "taxes": taxes,
                "shipping": shipping,
                "fees": fees,
                "credits": credits,
                "product_subtotal": round(prod_subtotal, 2),
                "tax_total": round(tax_total, 2),
                "shipping_total": round(ship_total, 2),
                "fee_total": round(fee_total, 2),
                "credit_total": round(credit_total, 2),
                "total_amount": float(ord_info.get("total_price") or 0.0),
                "amount_paid": float(ord_info.get("amount_paid") or 0.0),
                "total_outstanding": float(ord_info.get("total_outstanding") or 0.0),
            }
        }
    except Exception as e:
        return {"ok": False, "error": f"Failed to look up order #{order_id}: {e}"}


# ---------------------------------------------------------------- Control
@app.post("/api/sim/reset")
def reset():
    global SESSION
    SESSION = agent.ChatSession(role="bathagent", actor="bathagent-chat")
    agent.WATCH.clear()
    return console("/api/demo/reset", {})


@app.get("/api/sim/state")
def state():
    return console("/api/state")


@app.get("/")
def index():
    return FileResponse(
        pathlib.Path(__file__).parent / "static" / "index.html",
        headers={"Cache-Control": "no-store"},
    )


app.mount("/static", StaticFiles(directory=pathlib.Path(__file__).parent / "static"))
