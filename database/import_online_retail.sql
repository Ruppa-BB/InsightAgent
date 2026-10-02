-- Import cleaned Online Retail II CSV exports.
-- Run from the project root with psql so \copy can read local files.
\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE st_region (
    region_id bigint,
    region_code text,
    region_name text
);
CREATE TEMP TABLE st_customer (
    customer_id_generated bigint,
    customer_code text,
    source_customer_id bigint,
    region_code text
);
CREATE TEMP TABLE st_product (
    product_id bigint,
    product_code text,
    description text
);
CREATE TEMP TABLE st_order (
    order_id bigint,
    order_number text,
    customer_key bigint,
    region_id bigint,
    order_date date,
    confirmed_date date,
    order_status text
);
CREATE TEMP TABLE st_detail (
    order_id bigint,
    line_number integer,
    customer_key bigint,
    product_id bigint,
    region_id bigint,
    order_date date,
    quantity integer,
    unit_price numeric(18,4),
    sales_amount numeric(24,2)
);

\copy st_region FROM 'data/generated/online_retail_ii/dim_region.csv' WITH (FORMAT csv, HEADER true)
\copy st_customer FROM 'data/generated/online_retail_ii/dim_customer.csv' WITH (FORMAT csv, HEADER true)
\copy st_product FROM 'data/generated/online_retail_ii/dim_product.csv' WITH (FORMAT csv, HEADER true)
\copy st_order FROM 'data/generated/online_retail_ii/fact_sales_order.csv' WITH (FORMAT csv, HEADER true)
\copy st_detail FROM 'data/generated/online_retail_ii/fact_sales_detail.csv' WITH (FORMAT csv, HEADER true)

INSERT INTO dim_region (region_id, region_code, region_name)
SELECT region_id, region_code, region_name FROM st_region;

INSERT INTO dim_date (date_id)
SELECT DISTINCT order_date FROM st_order
ON CONFLICT (date_id) DO NOTHING;

INSERT INTO dim_customer (
    customer_id, customer_code, customer_name, industry, customer_level,
    region_id, acquisition_date
)
SELECT
    c.customer_id_generated,
    c.customer_code,
    c.customer_code,
    'Unknown',
    'Unknown',
    r.region_id,
    MIN(o.order_date)
FROM st_customer c
JOIN st_region r ON r.region_code = upper(replace(c.region_code, ' ', '_'))
JOIN st_order o ON o.customer_key = c.customer_id_generated
GROUP BY c.customer_id_generated, c.customer_code, r.region_id;

INSERT INTO dim_product (
    product_id, product_code, product_name, product_line, product_category,
    standard_price, standard_cost
)
SELECT
    product_id,
    product_code,
    COALESCE(NULLIF(description, ''), product_code),
    'Retail',
    'Unknown',
    0,
    0
FROM st_product;

INSERT INTO fact_sales_order (
    order_id, order_number, customer_id, region_id, order_date,
    confirmed_date, order_status
)
SELECT order_id, order_number, customer_key, region_id, order_date,
       confirmed_date, order_status
FROM st_order;

INSERT INTO fact_sales_detail (
    order_id, line_number, customer_id, product_id, region_id, order_date,
    quantity, unit_price, standard_cost
)
SELECT order_id, line_number, customer_key, product_id, region_id, order_date,
       quantity, unit_price, 0
FROM st_detail;

COMMIT;

SELECT 'orders' AS metric, count(*)::numeric AS value FROM fact_sales_order
UNION ALL
SELECT 'details', count(*)::numeric FROM fact_sales_detail
UNION ALL
SELECT 'sales_amount', COALESCE(sum(sales_amount), 0) FROM fact_sales_detail;
