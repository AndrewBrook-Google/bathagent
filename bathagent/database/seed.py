"""Seed script for populating BathStuff database with realistic demo dataset.
Includes Elbonian toothpaste products, pre/post July 1 2026 shipped orders for tariff demo,
and active customer orders for support scenarios.
"""

from datetime import date, timedelta
import random
from typing import Any, List, Tuple


def get_seed_data() -> Tuple[List[Tuple[str, List[Tuple[Any, ...]]]]]:
    """Generates all baseline relational data for BathStuff demo."""
    
    # 1. Suppliers
    suppliers = [
        (1, "Elbonia Dental Specialties", "Elbonia"),
        (2, "Alpine Botanicals Ltd", "Switzerland"),
        (3, "Kyoto Herbal Care", "Japan"),
        (4, "Sunshine Naturals Co", "United States"),
    ]

    # 2. Tax Codes
    tax_codes = [
        (1, "ORAL_CARE", "Toothpaste", 0.0500),
        (2, "BATH_BODY", "Soap & Bodywash", 0.0500),
        (3, "HAIR_CARE", "Shampoo & Conditioner", 0.0500),
    ]

    # 3. Tax Eligibility Rules (Tariffs)
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

    # 4. Products
    products = [
        (101, "ELB-TP-01", "Elbonian SparkleGrit Toothpaste 100ml", 1, 1),
        (102, "ELB-TP-02", "Grand Elbonian Mint Paste 120ml", 1, 1),
        (103, "ELB-TP-03", "Elbonia Royal Whitening Foam 75ml", 1, 1),
        (104, "ALP-SW-01", "Swiss Alpine Lavender Body Wash 250ml", 2, 2),
        (105, "KYO-HC-01", "Kyoto Camellia Hair Shampoo 300ml", 3, 3),
        (106, "SUN-BB-01", "California Citrus Bath Bombs 6-pack", 4, 2),
    ]

    # 5. Product Pricing History
    pricing_history = [
        (1, 101, 12.50, date(2025, 1, 1), None),
        (2, 102, 15.00, date(2025, 1, 1), None),
        (3, 103, 18.00, date(2025, 1, 1), None),
        (4, 104, 22.00, date(2025, 1, 1), None),
        (5, 105, 28.00, date(2025, 1, 1), None),
        (6, 106, 16.50, date(2025, 1, 1), None),
    ]

    # 6. Customers
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

    # 7. Orders & Order Items
    # Repeatable deterministic order generation
    orders = []
    order_items = []
    
    order_id_seq = 1000
    item_id_seq = 5000

    # Specific Scenario Order 1: Alice Smith has an active unshipped order (for cancellation demo)
    order_id_seq += 1
    orders.append((order_id_seq, 101, date(2026, 8, 20), None, "PROCESSING"))
    item_id_seq += 1
    order_items.append((item_id_seq, order_id_seq, 104, 2, 22.00, 44.00, "Lavender Body Wash x2"))
    item_id_seq += 1
    order_items.append((item_id_seq, order_id_seq, 106, 1, 16.50, 16.50, "Citrus Bath Bombs"))

    # Specific Scenario Order 2: Alice Smith had an earlier delivered order
    order_id_seq += 1
    orders.append((order_id_seq, 101, date(2026, 6, 10), date(2026, 6, 12), "DELIVERED"))
    item_id_seq += 1
    order_items.append((item_id_seq, order_id_seq, 101, 1, 12.50, 12.50, "Original purchase (Pre-tariff)"))

    # Deterministic generation of 30 orders across customers:
    # - 10 shipped BEFORE July 1, 2026 (pre-tariff)
    # - 20 shipped AFTER July 1, 2026 (post-tariff - affected cohort for Toothpaste Tariff Troubles)
    rng = random.Random(42)

    # Pre-tariff orders (May - June 2026)
    for i in range(10):
        order_id_seq += 1
        cust = rng.choice(customers)[0]
        o_date = date(2026, 5, 1) + timedelta(days=rng.randint(0, 50))
        s_date = o_date + timedelta(days=rng.randint(1, 3))
        orders.append((order_id_seq, cust, o_date, s_date, "SHIPPED"))
        
        # Add 1-3 items
        p = rng.choice([101, 102, 103, 104, 105, 106])
        price = {101: 12.50, 102: 15.00, 103: 18.00, 104: 22.00, 105: 28.00, 106: 16.50}[p]
        qty = rng.randint(1, 3)
        item_id_seq += 1
        order_items.append((item_id_seq, order_id_seq, p, qty, price, round(qty * price, 2), None))

    # Post-tariff orders (July 2 - August 20, 2026)
    # Ensure a solid batch of Elbonian toothpaste orders to illustrate the 66 paired line item demo
    for i in range(25):
        order_id_seq += 1
        cust = rng.choice(customers)[0]
        o_date = date(2026, 7, 2) + timedelta(days=rng.randint(0, 45))
        s_date = o_date + timedelta(days=rng.randint(1, 3))
        status = rng.choice(["SHIPPED", "DELIVERED"])
        orders.append((order_id_seq, cust, o_date, s_date, status))

        # Include at least one Elbonian toothpaste (product 101, 102, or 103)
        elb_prod = rng.choice([101, 102, 103])
        elb_price = {101: 12.50, 102: 15.00, 103: 18.00}[elb_prod]
        qty = rng.randint(1, 3)
        item_id_seq += 1
        order_items.append((item_id_seq, order_id_seq, elb_prod, qty, elb_price, round(qty * elb_price, 2), "Standard purchase"))

        # Optionally add a secondary non-Elbonian product
        if rng.random() > 0.5:
            other_p = rng.choice([104, 105, 106])
            other_price = {104: 22.00, 105: 28.00, 106: 16.50}[other_p]
            other_qty = rng.randint(1, 2)
            item_id_seq += 1
            order_items.append((item_id_seq, order_id_seq, other_p, other_qty, other_price, round(other_qty * other_price, 2), "Standard purchase"))

    return (
        ("suppliers", suppliers),
        ("tax_codes", tax_codes),
        ("tax_eligibility_rules", tax_rules),
        ("products", products),
        ("product_pricing_history", pricing_history),
        ("customers", customers),
        ("orders", orders),
        ("order_items", order_items),
    )
