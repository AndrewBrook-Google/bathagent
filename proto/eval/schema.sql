-- BathStuff minimal schema (Toothbrush Tariffs CUJ)
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS products CASCADE;

CREATE TABLE products (
    id          bigint PRIMARY KEY,
    name        text NOT NULL,
    category    text NOT NULL,
    imported    boolean NOT NULL DEFAULT false
);

CREATE TABLE orders (
    id          bigint PRIMARY KEY,
    product_id  bigint NOT NULL REFERENCES products(id),
    country     text NOT NULL,
    ordered_at  timestamptz NOT NULL,
    price       numeric(10,2) NOT NULL,
    tax         numeric(10,2) NOT NULL,
    total       numeric(10,2) NOT NULL,
    comment     text,
    updated_by  text
);
