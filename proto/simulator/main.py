"""BathStuff Business Simulator Web App.

Runs on port 8779, providing single-page interactive controls, live probability gauges,
and real-time order lifecycle feeds pointing at BathStuff catalog and AlloyDB database.
"""
import asyncio
import contextlib
import pathlib
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import engine

_tick_task = None


async def _simulation_ticker():
    last_t = time.monotonic()
    while True:
        await asyncio.sleep(0.2)
        now_t = time.monotonic()
        delta = now_t - last_t
        last_t = now_t
        try:
            engine.ENGINE.tick(delta)
        except Exception as e:
            print("Error in simulation tick:", e)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _tick_task
    _tick_task = asyncio.create_task(_simulation_ticker())
    yield
    if _tick_task:
        _tick_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _tick_task


app = FastAPI(title="BathStuff Business Simulator", lifespan=lifespan)


class ConfigReq(BaseModel):
    order_interval_min_sec: float = 2.0
    order_interval_max_sec: float = 5.0
    time_scale_multiplier: float = 360.0
    ship_min_hours: float = 2.0
    ship_max_hours: float = 8.0
    deliver_min_hours: float = 24.0
    deliver_max_hours: float = 48.0
    partial_payment_prob: float = 0.05


class StepReq(BaseModel):
    hours: float = 1.0


@app.get("/api/state")
def get_state():
    return engine.ENGINE.get_state()


@app.get("/api/orders")
def get_orders(limit: int = 50):
    try:
        import alloydb_client
        return alloydb_client.get_latest_orders_from_alloydb(limit)
    except Exception:
        return [o.to_dict() for o in list(engine.ENGINE.orders.values())[-limit:]]


@app.get("/api/history")
def get_history():
    return {
        "orders": [o.to_dict() for o in sorted(engine.ENGINE.orders.values(), key=lambda o: o.order_id, reverse=True)[:100]],
        "events": engine.ENGINE.events[:100],
    }


@app.post("/api/control/start")
def start_simulation():
    engine.ENGINE.start()
    return {"status": "started", "running": True}


@app.post("/api/control/stop")
def stop_simulation():
    engine.ENGINE.stop()
    return {"status": "stopped", "running": False}


@app.post("/api/control/order")
def trigger_single_order():
    order = engine.ENGINE.create_order()
    return {"status": "order_created", "order": order.to_dict()}


@app.post("/api/control/step")
def fast_forward(req: StepReq = None):
    hours = req.hours if req else 1.0
    # Simulate step by ticking with equivalent delta
    seconds_sim = hours * 3600.0
    # Process in chunks of 5 simulated minutes
    chunk_sim = 300.0
    steps = int(seconds_sim / chunk_sim)
    for _ in range(steps):
        engine.ENGINE.tick(chunk_sim / engine.ENGINE.time_scale_multiplier)
    engine.ENGINE._log_event("FAST_FORWARD", f"Fast-forwarded simulation by {hours:.1f} hours.")
    return engine.ENGINE.get_state()


@app.post("/api/config")
def update_configuration(req: ConfigReq):
    engine.ENGINE.update_config(req.model_dump())
    return {"status": "config_updated", "config": engine.ENGINE.get_state()["config"]}


@app.post("/api/control/reset")
def reset_simulation():
    engine.ENGINE.stop()
    engine.ENGINE.orders.clear()
    engine.ENGINE.events.clear()
    engine.ENGINE.next_order_id = 1501
    engine.ENGINE.total_generated = 0
    engine.ENGINE.total_shipped = 0
    engine.ENGINE.total_delivered = 0
    engine.ENGINE._bootstrap_initial_orders()
    return engine.ENGINE.get_state()


@app.get("/api/db/orders")
def get_db_orders(limit: int = 50):
    return {"orders": engine.ENGINE.get_db_orders(limit)}


@app.get("/")
def index():
    return FileResponse(pathlib.Path(__file__).parent / "static" / "index.html",
                        headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=pathlib.Path(__file__).parent / "static"))
