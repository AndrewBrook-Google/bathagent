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
from fastapi.responses import FileResponse
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
    steps, final = SESSION.run_turn(req.message)
    return {
        "steps": steps,
        "final": final,
        "agent": "bathagent",
        "reader_id": SESSION.reader_id,
        "notes": SESSION.notes,
    }


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
