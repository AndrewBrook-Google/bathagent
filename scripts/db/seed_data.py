#!/usr/bin/env python3
"""Standalone reference seed data generator for BathStuff AlloyDB instance.
Isolated from the core BathAgent application.
"""

import argparse
from datetime import date, timedelta
import os
import random
import sys
from typing import Any, List, Tuple

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None


def generate_seed_records():
    """Generates baseline relational records for BathStuff."""
    suppliers = [
        (1, "Elbonia Dental Specialties", "Elbonia"),
        (2, "Alpine Botanicals Ltd", "Switzerland"),
        (3, "Kyoto Herbal Care", "Japan"),
        (4, "Sunshine Naturals Co", "United States"),
    ]

    tax_codes = [
        (1, "ORAL_CARE", "Toothpaste", 0.0500),
        (2, "BATH_BODY", "Soap & Bodywash", 0.0500),
        (3, "HAIR_CARE", "Shampoo & Conditioner", 0.0500),
    ]

    tax_rules = [
        (
            1,
            "Elbonia",
            "Toothpaste",
            0.2000,
            date(2026, 7, 1),
            "Executive Order 2026-T42: 20% additional import tariff on Elbonian toothpaste",
        )
    ]

    products = [
        (101, "ELB-TP-01", "Elbonian SparkleGrit Toothpaste 100ml", 1, 1),
        (102, "ELB-TP-02", "Grand Elbonian Mint Paste 120ml", 1, 1),
        (103, "ELB-TP-03", "Elbonia Royal Whitening Foam 75ml", 1, 1),
        (104, "ALP-SW-01", "Swiss Alpine Lavender Body Wash 250ml", 2, 2),
        (105, "KYO-HC-01", "Kyoto Camellia Hair Shampoo 300ml", 3, 3),
        (106, "SUN-BB-01", "California Citrus Bath Bombs 6-pack", 4, 2),
    ]

    pricing_history = [
        (1, 101, 12.50, date(2025, 1, 1), None),
        (2, 102, 15.00, date(2025, 1, 1), None),
        (3, 103, 18.00, date(2025, 1, 1), None),
        (4, 104, 22.00, date(2025, 1, 1), None),
        (5, 105, 28.00, date(2025, 1, 1), None),
        (6, 106, 16.50, date(2025, 1, 1), None),
    ]

    customers = [
        (101, "Alice Smith", "alice.smith@example.com", "United States"),
        (102, "Bob Jones", "bob.jones@example.com", "United States"),
        (103, "Charlie Davis", "charlie.davis@example.com", "Canada"),
        (104, "Dana Vance", "dana.vance@example.com", "United Kingdom"),
        (105, "Eve Miller", "eve.miller@example.com", "United States"),
        (106, "Frank Wilson", "frank.wilson@example.com", "United States"),
        (107, "Grace Hopper", "grace.hopper@example.com", "United States"),
        (108, "Hank Pym", "hank.pym@example.com", "United States"),
    ]

    orders = []
    order_items = []
    order_id_seq = 1000
    item_id_seq = 5000

    # Order 1: Alice Smith unshipped order (PROCESSING)
    order_id_seq += 1
    orders.append((order_id_seq, 101, date(2026, 8, 20), None, "PROCESSING"))
    item_id_seq += 1
    order_items.append((item_id_seq, order_id_seq, 104, 2, 22.00, 44.00, "Lavender Body Wash x2"))
    item_id_seq += 1
    order_items.append((item_id_seq, order_id_seq, 106, 1, 16.50, 16.50, "Citrus Bath Bombs"))

    # Order 2: Alice Smith historical delivered order (Pre-tariff)
    order_id_seq += 1
    orders.append((order_id_seq, 101, date(2026, 6, 10), date(2026, 6, 12), "DELIVERED"))
    item_id_seq += 1
    order_items.append((item_id_seq, order_id_seq, 101, 1, 12.50, 12.50, "Pre-tariff purchase"))

    rng = random.Random(42)

    # 10 orders shipped BEFORE July 1, 2026
    for _ in range(10):
        order_id_seq += 1
        cust = rng.choice(customers)[0]
        o_date = date(2026, 5, 1) + timedelta(days=rng.randint(0, 50))
        s_date = o_date + timedelta(days=rng.randint(1, 3))
        orders.append((order_id_seq, cust, o_date, s_date, "SHIPPED"))
        p = rng.choice([101, 102, 103, 104, 105, 106])
        price = {101: 12.50, 102: 15.00, 103: 18.00, 104: 22.00, 105: 28.00, 106: 16.50}[p]
        qty = rng.randint(1, 3)
        item_id_seq += 1
        order_items.append((item_id_seq, order_id_seq, p, qty, price, round(qty * price, 2), None))

    # 25 orders shipped AFTER July 1, 2026 (subject to Elbonia tariff)
    for _ in range(25):
        order_id_seq += 1
        cust = rng.choice(customers)[0]
        o_date = date(2026, 7, 2) + timedelta(days=rng.randint(0, 45))
        s_date = o_date + timedelta(days=rng.randint(1, 3))
        status = rng.choice(["SHIPPED", "DELIVERED"])
        orders.append((order_id_seq, cust, o_date, s_date, status))

        elb_prod = rng.choice([101, 102, 103])
        elb_price = {101: 12.50, 102: 15.00, 103: 18.00}[elb_prod]
        qty = rng.randint(1, 3)
        item_id_seq += 1
        order_items.append((item_id_seq, order_id_seq, elb_prod, qty, elb_price, round(qty * elb_price, 2), "Standard purchase"))

        if rng.random() > 0.5:
            other_p = rng.choice([104, 105, 106])
            other_price = {104: 22.00, 105: 28.00, 106: 16.50}[other_p]
            other_qty = rng.randint(1, 2)
            item_id_seq += 1
            order_items.append((item_id_seq, order_id_seq, other_p, other_qty, other_price, round(other_qty * other_price, 2), "Standard purchase"))

    return [
        ("suppliers", suppliers),
        ("tax_codes", tax_codes),
        ("tax_eligibility_rules", tax_rules),
        ("products", products),
        ("product_pricing_history", pricing_history),
        ("customers", customers),
        ("orders", orders),
        ("order_items", order_items),
    ]


def main():
    parser = argparse.ArgumentParser(description="Seed BathStuff AlloyDB instance.")
    parser.add_argument("--host", default=os.getenv("DB_HOST", "localhost"), help="AlloyDB host/IP")
    parser.add_argument("--port", type=int, default=int(os.getenv("DB_PORT", "5432")), help="Port")
    parser.add_argument("--user", default=os.getenv("DB_USER", "postgres"), help="Database user")
    parser.add_argument("--password", default=os.getenv("DB_PASSWORD", "postgres"), help="Password")
    parser.add_argument("--dbname", default=os.getenv("DB_NAME", "bathstuff"), help="Database name")
    parser.add_argument("--apply-schema", action="store_true", help="Apply scripts/db/schema.sql prior to seeding")
    args = parser.parse_args()

    if psycopg2 is None:
        print("Error: psycopg2 is required to seed AlloyDB. Run `pip install psycopg2-binary`.")
        sys.exit(1)

    print(f"Connecting to AlloyDB at {args.host}:{args.port}/{args.dbname}...")
    try:
        conn = psycopg2.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            dbname=args.dbname,
        )
        conn.autocommit = True
        cur = conn.cursor()

        if args.apply_schema:
            schema_file = os.path.join(os.path.dirname(__file__), "schema.sql")
            print(f"Applying schema from {schema_file}...")
            with open(schema_file, "r") as f:
                cur.execute(f.read())
            print("Schema applied.")

        print("Populating seed records...")
        tables = generate_seed_records()
        for table_name, rows in tables:
            placeholders = ",".join(["%s"] * len(rows[0]))
            query = f"INSERT INTO {table_name} VALUES ({placeholders})"
            cur.executemany(query, rows)
            print(f"  ✓ {len(rows)} rows inserted into {table_name}")

        cur.close()
        conn.close()
        print("✅ AlloyDB seeding complete.")
    except Exception as e:
        print(f"❌ Failed to seed AlloyDB: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
