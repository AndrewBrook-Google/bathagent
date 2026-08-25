"""Business simulation engine for BathStuff.

Simulates customer orders entering the system, progressing through shipping (2-8 hrs with
growing probability from 0% at 2h to 100% at 8h), and reaching delivery (24-48 hrs).
Persists all orders, line items, and lifecycle status changes directly into the AlloyDB
cluster bathstuff-prod in andybrook-playground (us-central1).

Ensures all customers for generated orders exist in the AlloyDB `customers` table,
automatically creating and inserting new customers into the database when needed.
"""
import datetime
import random
import time
from typing import Any, Dict, List, Optional

import alloydb_client

INITIAL_SEED_CUSTOMERS = [
    {"customer_id": 1, "name": "Felicity McFoam", "email": "felicity.mcfoam@bathstuff-demo.com", "country": "USA"},
    {"customer_id": 2, "name": "Desdemona Splashford", "email": "desdemona@splashford.org", "country": "USA"},
    {"customer_id": 3, "name": "Barnaby Cleanworth", "email": "barnaby@cleanworth.co.uk", "country": "UK"},
    {"customer_id": 4, "name": "David Martinez", "email": "david.m@cyber.net", "country": "USA"},
    {"customer_id": 5, "name": "Elena Rostova", "email": "elena.r@eurobath.fr", "country": "France"},
    {"customer_id": 6, "name": "Frank Castle", "email": "frank@defense.gov", "country": "USA"},
    {"customer_id": 7, "name": "Grace Hopper", "email": "grace@navy.mil", "country": "USA"},
    {"customer_id": 8, "name": "Hideo Kojima", "email": "hideo@kyotobath.jp", "country": "Japan"},
    {"customer_id": 9, "name": "Ingrid Bergman", "email": "ingrid@nordic.se", "country": "Sweden"},
    {"customer_id": 10, "name": "Jack Ryan", "email": "jack@cia.gov", "country": "USA"},
    {"customer_id": 11, "name": "Klaus Baudelaire", "email": "klaus@vfd.org", "country": "USA"},
    {"customer_id": 12, "name": "Catherine O'Hara", "email": "cathy@example.org", "country": "Elbonia"},
    {"customer_id": 15, "name": "Marcus Aurelius", "email": "marcus@rome.it", "country": "Italy"},
    {"customer_id": 20, "name": "Nadia Comaneci", "email": "nadia@gym.ro", "country": "Romania"},
    {"customer_id": 25, "name": "Otto Octavius", "email": "otto@oscorp.com", "country": "USA"},
]

FIRST_NAMES = [
    "Alexander", "Beatrice", "Caleb", "Daphne", "Emil", "Fiona", "Gideon", "Helena",
    "Ivan", "Jocelyn", "Killian", "Leila", "Magnus", "Nico", "Ophelia", "Piers",
    "Quinn", "Rowena", "Silas", "Tabitha", "Valerie", "Winston", "Xavier", "Yasmine",
    "Zachary", "Alistair", "Brianna", "Cassian", "Dorothy", "Evander", "Freya"
]

LAST_NAMES = [
    "Soapstone", "Bubblesmith", "Waters", "Lavender", "Cleanwell", "Foamcraft",
    "Latherby", "Sudsfield", "Bathgate", "Tideborn", "Splashmore", "Glycerine",
    "Eucalyptus", "Pumice", "Seafoam", "Sparklegrit", "Barrington", "Oatmeal"
]

COUNTRIES = [
    "USA", "UK", "Canada", "France", "Japan", "Germany", "Sweden", "Italy",
    "Spain", "Australia", "Netherlands", "Norway", "Switzerland", "Elbonia"
]

PRODUCTS = [
    {"product_id": 1, "sku": "ELB-TP-01", "name": "Elbonian SparkleGrit Toothpaste", "unit_price": 14.50, "tax_code_id": 3},
    {"product_id": 2, "sku": "ELB-TP-02", "name": "Grand Duke's Minty Fortress Paste", "unit_price": 22.00, "tax_code_id": 3},
    {"product_id": 3, "sku": "FRA-SOAP-01", "name": "Provence Lavender Soap Bar", "unit_price": 8.75, "tax_code_id": 1},
    {"product_id": 4, "sku": "FRA-SOAP-02", "name": "Marseille Triple-Milled Olive Soap", "unit_price": 16.50, "tax_code_id": 1},
    {"product_id": 5, "sku": "USA-BATH-01", "name": "Apex Eucalyptus Bath Bomb 6-Pack", "unit_price": 24.99, "tax_code_id": 2},
    {"product_id": 6, "sku": "USA-BATH-02", "name": "Pacific Sea Salt Body Scrub", "unit_price": 19.50, "tax_code_id": 2},
    {"product_id": 7, "sku": "JPN-BATH-01", "name": "Kyoto Hinoki Cypress Soak Powder", "unit_price": 32.00, "tax_code_id": 2},
    {"product_id": 8, "sku": "JPN-BATH-02", "name": "Yuzu Citrus Rejuvenating Crystals", "unit_price": 28.50, "tax_code_id": 2},
    {"product_id": 9, "sku": "SWE-BODY-01", "name": "Nordic Birch & Lingonberry Lotion", "unit_price": 21.00, "tax_code_id": 1},
    {"product_id": 10, "sku": "SWE-BODY-02", "name": "Glacier Thermal Facial Polish", "unit_price": 38.00, "tax_code_id": 1},
]


class OrderItem:
    def __init__(self, product_id: int, sku: str, name: str, quantity: int, unit_price: float):
        self.product_id = product_id
        self.sku = sku
        self.name = name
        self.quantity = quantity
        self.unit_price = unit_price
        self.line_total = round(quantity * unit_price, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_id": self.product_id,
            "sku": self.sku,
            "name": self.name,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "line_total": self.line_total,
        }


class SimulatedOrder:
    def __init__(
        self,
        order_id: int,
        customer_id: int,
        customer_name: str,
        customer_country: str,
        order_date: datetime.datetime,
        items: List[OrderItem],
        status: str = "PROCESSING",
        ship_date: Optional[datetime.datetime] = None,
        delivery_date: Optional[datetime.datetime] = None,
        paid_fraction: float = 1.0,
    ):
        self.order_id = order_id
        self.customer_id = customer_id
        self.customer_name = customer_name
        self.customer_country = customer_country
        self.order_date = order_date
        self.items = items
        self.status = status
        self.ship_date = ship_date
        self.delivery_date = delivery_date

        self.total_price = round(sum(it.line_total for it in items), 2)
        self.amount_paid = round(self.total_price * paid_fraction, 2)
        self.total_outstanding = round(self.total_price - self.amount_paid, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "customer_country": self.customer_country,
            "order_date": self.order_date.isoformat(),
            "ship_date": self.ship_date.isoformat() if self.ship_date else None,
            "delivery_date": self.delivery_date.isoformat() if self.delivery_date else None,
            "status": self.status,
            "total_price": self.total_price,
            "amount_paid": self.amount_paid,
            "total_outstanding": self.total_outstanding,
            "items": [it.to_dict() for it in self.items],
            "item_count": sum(it.quantity for it in self.items),
        }


class SimulatorEngine:
    def __init__(self):
        self.running: bool = False
        
        # Determine starting order_id from AlloyDB
        max_id = alloydb_client.get_max_order_id()
        self.next_order_id: int = max_id + 1
        self.next_customer_id: int = 251

        # Configuration parameters
        self.order_interval_min_sec: float = 2.0
        self.order_interval_max_sec: float = 5.0
        self.time_scale_multiplier: float = 1.0  # Realtime: 1 real second = 1 business second
        self.ship_min_hours: float = 2.0
        self.ship_max_hours: float = 8.0
        self.deliver_min_hours: float = 24.0
        self.deliver_max_hours: float = 48.0
        self.partial_payment_prob: float = 0.05
        self.new_customer_prob: float = 0.20  # 20% of new orders generate a new customer

        # Clock (Realtime UTC)
        self.sim_time: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
        self.last_real_time: float = time.monotonic()
        self.next_order_due_real_time: float = 0.0

        # State storage
        self.known_customers: Dict[int, Dict[str, Any]] = {}
        self.orders: Dict[int, SimulatedOrder] = {}
        self.events: List[Dict[str, Any]] = []
        self._max_events = 200

        # Stats counters
        self.total_generated: int = 0
        self.total_shipped: int = 0
        self.total_delivered: int = 0

        # Sync initial state from AlloyDB bathstuff-prod
        self._sync_initial_from_alloydb()

    def _sync_initial_from_alloydb(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.sim_time = now

        # 1. Sync known customers from AlloyDB
        try:
            db_custs = alloydb_client.get_all_customers_from_alloydb()
            if db_custs:
                for c in db_custs:
                    cid = int(c["customer_id"])
                    self.known_customers[cid] = {
                        "customer_id": cid,
                        "name": c.get("full_name") or f"Customer {cid}",
                        "email": c.get("email") or f"cust{cid}@example.com",
                        "country": c.get("shipping_country") or "USA",
                    }
                max_cid = max(self.known_customers.keys(), default=250)
                self.next_customer_id = max_cid + 1
            else:
                for c in INITIAL_SEED_CUSTOMERS:
                    self.known_customers[c["customer_id"]] = c
        except Exception as e:
            for c in INITIAL_SEED_CUSTOMERS:
                self.known_customers[c["customer_id"]] = c
            self._log_event("ALLOYDB_WARN", f"Customer sync fallback: {e}")

        # 2. Sync recent orders from AlloyDB
        try:
            db_orders = alloydb_client.get_latest_orders_from_alloydb(30)
            if db_orders:
                for row in db_orders:
                    oid = int(row["order_id"])
                    if oid >= self.next_order_id:
                        self.next_order_id = oid + 1
                    try:
                        odate = datetime.datetime.fromisoformat(row["order_date"].replace("Z", "+00:00"))
                    except Exception:
                        odate = now
                    sdate = datetime.datetime.fromisoformat(row["ship_date"].replace("Z", "+00:00")) if row.get("ship_date") else None
                    cid = int(row.get("customer_id") or 1)
                    cust = self.known_customers.get(cid, {
                        "customer_id": cid,
                        "name": f"Customer #{cid}",
                        "country": "USA",
                        "email": f"cust{cid}@example.com"
                    })

                    item = OrderItem(1, "ELB-TP-01", "Elbonian SparkleGrit Toothpaste", int(row.get("items_count") or 1), float(row.get("total_price") or 14.50))
                    order = SimulatedOrder(
                        order_id=oid,
                        customer_id=cid,
                        customer_name=cust["name"],
                        customer_country=cust["country"],
                        order_date=odate,
                        items=[item],
                        status=row.get("status") or "PROCESSING",
                        ship_date=sdate,
                    )
                    order.total_price = float(row.get("total_price") or 0.0)
                    order.amount_paid = float(row.get("amount_paid") or 0.0)
                    order.total_outstanding = float(row.get("total_outstanding") or 0.0)
                    self.orders[oid] = order
                    self.total_generated += 1
                    if order.status in ("SHIPPED", "DELIVERED"):
                        self.total_shipped += 1
                    if order.status == "DELIVERED":
                        self.total_delivered += 1

                self._log_event("ALLOYDB_SYNC", f"Connected to AlloyDB bathstuff-prod. Loaded {len(db_orders)} recent orders and {len(self.known_customers)} customers.")
                return
        except Exception as e:
            self._log_event("ALLOYDB_WARN", f"AlloyDB initial read: {e}")

    def _log_event(self, kind: str, message: str, order_id: Optional[int] = None, meta: Optional[Dict[str, Any]] = None):
        evt = {
            "seq": len(self.events) + 1,
            "sim_time": self.sim_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "real_time": datetime.datetime.now().strftime("%H:%M:%S"),
            "kind": kind,
            "message": message,
            "order_id": order_id,
            "meta": meta or {},
        }
        self.events.insert(0, evt)
        if len(self.events) > self._max_events:
            self.events = self.events[: self._max_events]

    def _get_or_create_customer(self) -> Dict[str, Any]:
        """Return an existing customer or generate and persist a new one to AlloyDB."""
        if self.known_customers and random.random() >= self.new_customer_prob:
            return random.choice(list(self.known_customers.values()))

        # Generate a new customer
        cid = self.next_customer_id
        self.next_customer_id += 1
        name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        country = random.choice(COUNTRIES)
        email = f"{name.lower().replace(' ', '.')}@{random.choice(['bathstuff.org', 'bubblemail.net', 'cleanexample.com', 'lather.io'])}"

        cust = {
            "customer_id": cid,
            "name": name,
            "email": email,
            "country": country,
        }

        # Persist new customer directly into AlloyDB customers table
        ok = alloydb_client.ensure_customer_in_alloydb(
            customer_id=cid,
            full_name=name,
            email=email,
            shipping_country=country,
        )
        self.known_customers[cid] = cust

        self._log_event(
            "NEW_CUSTOMER",
            f"Created new customer #{cid} '{name}' ({country}) and added to AlloyDB customers table.",
            meta={"customer_id": cid, "name": name, "country": country, "alloydb": ok},
        )
        return cust

    def create_order(self) -> SimulatedOrder:
        cust = self._get_or_create_customer()
        
        # Ensure customer exists in database
        if cust["customer_id"] not in self.known_customers:
            alloydb_client.ensure_customer_in_alloydb(
                customer_id=cust["customer_id"],
                full_name=cust["name"],
                email=cust.get("email", ""),
                shipping_country=cust.get("country", "USA"),
            )
            self.known_customers[cust["customer_id"]] = cust

        item_defs = random.sample(PRODUCTS, k=random.randint(1, 4))
        items = [
            OrderItem(
                p["product_id"],
                p["sku"],
                p["name"],
                random.randint(1, 4),
                p["unit_price"],
            )
            for p in item_defs
        ]

        paid_fraction = 1.0
        if random.random() < self.partial_payment_prob:
            paid_fraction = random.choice([0.0, 0.5, 0.75])

        oid = self.next_order_id
        self.next_order_id += 1

        order = SimulatedOrder(
            order_id=oid,
            customer_id=cust["customer_id"],
            customer_name=cust["name"],
            customer_country=cust["country"],
            order_date=self.sim_time,
            items=items,
            status="PROCESSING",
            paid_fraction=paid_fraction,
        )
        self.orders[oid] = order
        self.total_generated += 1

        # WRITE DIRECTLY TO ALLOYDB BATHSTUFF-PROD
        item_dicts = [it.to_dict() for it in items]
        ok = alloydb_client.insert_order_into_alloydb(
            order_id=oid,
            customer_id=cust["customer_id"],
            order_date=self.sim_time,
            status="PROCESSING",
            total_price=order.total_price,
            amount_paid=order.amount_paid,
            total_outstanding=order.total_outstanding,
            items=item_dicts,
            customer_name=cust["name"],
            customer_email=cust.get("email", ""),
            customer_country=cust.get("country", "USA"),
        )

        items_summary = ", ".join(f"{it.quantity}x {it.name}" for it in items)
        pay_info = f"Paid: ${order.amount_paid:.2f}" if order.total_outstanding == 0 else f"Paid: ${order.amount_paid:.2f} (Outstanding: ${order.total_outstanding:.2f})"
        db_tag = "AlloyDB bathstuff-prod" if ok else "Local"
        self._log_event(
            "ORDER_PLACED",
            f"Order #{oid} written to {db_tag} · Customer #{cust['customer_id']} {cust['name']} ({cust['country']}) · Total: ${order.total_price:.2f} · {items_summary} · {pay_info}",
            order_id=oid,
            meta={"customer": cust["name"], "customer_id": cust["customer_id"], "total": order.total_price, "items_count": len(items), "alloydb": ok},
        )
        return order

    def update_config(self, cfg: Dict[str, Any]):
        if "order_interval_min_sec" in cfg:
            self.order_interval_min_sec = max(0.5, float(cfg["order_interval_min_sec"]))
        if "order_interval_max_sec" in cfg:
            self.order_interval_max_sec = max(self.order_interval_min_sec, float(cfg["order_interval_max_sec"]))
        if "ship_min_hours" in cfg:
            self.ship_min_hours = max(0.1, float(cfg["ship_min_hours"]))
        if "ship_max_hours" in cfg:
            self.ship_max_hours = max(self.ship_min_hours + 0.1, float(cfg["ship_max_hours"]))
        if "deliver_min_hours" in cfg:
            self.deliver_min_hours = max(1.0, float(cfg["deliver_min_hours"]))
        if "deliver_max_hours" in cfg:
            self.deliver_max_hours = max(self.deliver_min_hours + 1.0, float(cfg["deliver_max_hours"]))
        if "partial_payment_prob" in cfg:
            self.partial_payment_prob = max(0.0, min(1.0, float(cfg["partial_payment_prob"])))

        self._log_event("CONFIG_UPDATE", f"Simulation rates updated: Interval={self.order_interval_min_sec:.1f}-{self.order_interval_max_sec:.1f}s, Ship={self.ship_min_hours:.1f}-{self.ship_max_hours:.1f}h")

    def start(self):
        if not self.running:
            self.running = True
            self.last_real_time = time.monotonic()
            self._schedule_next_order()
            self._log_event("CONTROL", "Simulation engine STARTED in realtime mode writing to AlloyDB.")

    def stop(self):
        if self.running:
            self.running = False
            self._log_event("CONTROL", "Simulation engine STOPPED.")

    def _schedule_next_order(self):
        delay = random.uniform(self.order_interval_min_sec, self.order_interval_max_sec)
        self.next_order_due_real_time = time.monotonic() + delay

    def tick(self, delta_real_sec: float):
        """Advance time in realtime and evaluate order transitions."""
        self.sim_time = datetime.datetime.now(datetime.timezone.utc)

        if not self.running:
            return

        # 1. Check if new order is due
        now_mono = time.monotonic()
        if now_mono >= self.next_order_due_real_time:
            self.create_order()
            self._schedule_next_order()

        # 2. Evaluate un-shipped orders for shipping transition
        for order in list(self.orders.values()):
            if order.status == "PROCESSING":
                elapsed_hours = (self.sim_time - order.order_date).total_seconds() / 3600.0
                if elapsed_hours < self.ship_min_hours:
                    continue

                if elapsed_hours >= self.ship_max_hours:
                    prob = 1.0
                else:
                    # Linear transition probability from 0.0 at min to 1.0 at max
                    prob = (elapsed_hours - self.ship_min_hours) / (self.ship_max_hours - self.ship_min_hours)

                # Probabilistic transition step per evaluation
                eval_rate = min(1.0, (delta_real_sec / 3600.0) * 2.0 + prob * 0.4)
                if random.random() < eval_rate or elapsed_hours >= self.ship_max_hours:
                    order.status = "SHIPPED"
                    order.ship_date = order.order_date + datetime.timedelta(hours=elapsed_hours)
                    self.total_shipped += 1
                    
                    # Update status in AlloyDB bathstuff-prod
                    alloydb_client.update_order_status_in_alloydb(order.order_id, "SHIPPED", ship_date=order.ship_date)

                    self._log_event(
                        "ORDER_SHIPPED",
                        f"Order #{order.order_id} SHIPPED in AlloyDB (customer: {order.customer_name}, elapsed {elapsed_hours:.1f} hrs, prob {prob*100:.1f}%)",
                        order_id=order.order_id,
                        meta={"elapsed_hours": round(elapsed_hours, 2), "prob": round(prob, 3)},
                    )

            elif order.status == "SHIPPED":
                # Evaluate delivery transition
                base_time = order.ship_date or order.order_date
                elapsed_delivery_hours = (self.sim_time - base_time).total_seconds() / 3600.0
                if elapsed_delivery_hours < self.deliver_min_hours:
                    continue

                if elapsed_delivery_hours >= self.deliver_max_hours:
                    delivery_prob = 1.0
                else:
                    delivery_prob = (elapsed_delivery_hours - self.deliver_min_hours) / (self.deliver_max_hours - self.deliver_min_hours)

                eval_rate = min(1.0, (delta_real_sec / 3600.0) * 2.0 + delivery_prob * 0.4)
                if random.random() < eval_rate or elapsed_delivery_hours >= self.deliver_max_hours:
                    order.status = "DELIVERED"
                    order.delivery_date = base_time + datetime.timedelta(hours=elapsed_delivery_hours)
                    self.total_delivered += 1

                    # Update status in AlloyDB bathstuff-prod
                    alloydb_client.update_order_status_in_alloydb(order.order_id, "DELIVERED")

                    self._log_event(
                        "ORDER_DELIVERED",
                        f"Order #{order.order_id} DELIVERED in AlloyDB (customer: {order.customer_name}, elapsed {elapsed_delivery_hours:.1f} hrs)",
                        order_id=order.order_id,
                        meta={"elapsed_delivery_hours": round(elapsed_delivery_hours, 2)},
                    )

    def get_state(self) -> Dict[str, Any]:
        """Return live dashboard snapshot."""
        # Top 30 recent orders for display
        recent_orders = sorted(self.orders.values(), key=lambda o: o.order_id, reverse=True)[:30]
        
        # Calculate dynamic probabilities for display
        unshipped_list = []
        for o in recent_orders:
            if o.status == "PROCESSING":
                el_h = (self.sim_time - o.order_date).total_seconds() / 3600.0
                if el_h <= self.ship_min_hours:
                    p = 0.0
                elif el_h >= self.ship_max_hours:
                    p = 100.0
                else:
                    p = ((el_h - self.ship_min_hours) / (self.ship_max_hours - self.ship_min_hours)) * 100.0
                od_dict = o.to_dict()
                od_dict["elapsed_sim_hours"] = round(el_h, 2)
                od_dict["current_ship_probability"] = round(p, 1)
                unshipped_list.append(od_dict)

        # Count active processing and shipped orders
        processing_count = sum(1 for o in self.orders.values() if o.status == "PROCESSING")
        shipped_count = sum(1 for o in self.orders.values() if o.status == "SHIPPED")
        delivered_count = sum(1 for o in self.orders.values() if o.status == "DELIVERED")

        return {
            "running": self.running,
            "sim_time": self.sim_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "config": {
                "order_interval_min_sec": self.order_interval_min_sec,
                "order_interval_max_sec": self.order_interval_max_sec,
                "time_scale_multiplier": self.time_scale_multiplier,
                "ship_min_hours": self.ship_min_hours,
                "ship_max_hours": self.ship_max_hours,
                "deliver_min_hours": self.deliver_min_hours,
                "deliver_max_hours": self.deliver_max_hours,
                "partial_payment_prob": self.partial_payment_prob,
            },
            "stats": {
                "total_generated": self.total_generated,
                "total_shipped": self.total_shipped,
                "total_delivered": self.total_delivered,
                "processing_count": processing_count,
                "shipped_count": shipped_count,
                "delivered_count": delivered_count,
                "total_in_db": self.next_order_id - 1,
                "total_customers": len(self.known_customers),
            },
            "unshipped_orders": unshipped_list,
            "events": self.events[:50],
        }


ENGINE = SimulatorEngine()

