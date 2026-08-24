-- BathStuff Baseline Production Schema
-- Designed for AlloyDB / PostgreSQL

DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS customers CASCADE;
DROP TABLE IF EXISTS tax_eligibility_rules CASCADE;
DROP TABLE IF EXISTS product_pricing_history CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS tax_codes CASCADE;
DROP TABLE IF EXISTS suppliers CASCADE;

CREATE TABLE suppliers (
    supplier_id INT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    country_of_origin VARCHAR(50) NOT NULL
);

CREATE TABLE tax_codes (
    tax_code_id INT PRIMARY KEY,
    code_name VARCHAR(50) NOT NULL,
    category VARCHAR(50) NOT NULL,
    default_rate DECIMAL(5,4) NOT NULL
);

CREATE TABLE products (
    product_id INT PRIMARY KEY,
    sku VARCHAR(30) UNIQUE NOT NULL,
    name VARCHAR(150) NOT NULL,
    supplier_id INT REFERENCES suppliers(supplier_id),
    tax_code_id INT REFERENCES tax_codes(tax_code_id)
);

CREATE TABLE product_pricing_history (
    pricing_id INT PRIMARY KEY,
    product_id INT REFERENCES products(product_id),
    unit_price DECIMAL(10,2) NOT NULL,
    effective_start DATE NOT NULL,
    effective_end DATE
);

CREATE TABLE tax_eligibility_rules (
    rule_id INT PRIMARY KEY,
    country_of_origin VARCHAR(50) NOT NULL,
    category VARCHAR(50) NOT NULL,
    additional_tariff_rate DECIMAL(5,4) NOT NULL,
    effective_date DATE NOT NULL,
    note TEXT
);

CREATE TABLE customers (
    customer_id INT PRIMARY KEY,
    full_name VARCHAR(100) NOT NULL,
    email VARCHAR(100) NOT NULL,
    shipping_country VARCHAR(50) NOT NULL
);

CREATE TABLE orders (
    order_id INT PRIMARY KEY,
    customer_id INT REFERENCES customers(customer_id),
    order_date DATE NOT NULL,
    ship_date DATE,
    status VARCHAR(20) NOT NULL CHECK (status IN ('PENDING', 'PROCESSING', 'SHIPPED', 'DELIVERED', 'CANCELLED'))
);

CREATE TABLE order_items (
    order_item_id INT PRIMARY KEY,
    order_id INT REFERENCES orders(order_id),
    product_id INT REFERENCES products(product_id),
    quantity INT NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    line_total DECIMAL(10,2) NOT NULL,
    note TEXT
);

-- Indexing for performance and query optimization
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_ship_date ON orders(ship_date);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE INDEX idx_products_supplier ON products(supplier_id);
