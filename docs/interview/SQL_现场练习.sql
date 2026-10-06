-- InsightAgent 面试 SQL 练习：仅针对公开零售基线。
-- BEGIN 内全部只读；真实接口应绑定用户输入参数。
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL search_path TO public, pg_catalog;
SET LOCAL statement_timeout = '5s';

-- Q121
SELECT date_trunc('month', o.confirmed_date)::date AS month,
       SUM(d.sales_amount) AS sales_amount,
       COUNT(DISTINCT o.order_id) AS order_count,
       COUNT(DISTINCT o.customer_id) AS customer_count
FROM fact_sales_order o
JOIN fact_sales_detail d ON d.order_id = o.order_id
WHERE o.order_status = 'completed'
  AND o.confirmed_date >= DATE '2011-01-01'
  AND o.confirmed_date < DATE '2011-04-01'
GROUP BY 1
ORDER BY 1;

-- Q122
SELECT c.customer_code, c.customer_name,
       SUM(d.sales_amount) AS sales_amount
FROM fact_sales_order o
JOIN fact_sales_detail d ON d.order_id = o.order_id
JOIN dim_customer c ON c.customer_id = o.customer_id
WHERE o.order_status = 'completed'
  AND o.confirmed_date >= DATE '2011-01-01'
  AND o.confirmed_date < DATE '2012-01-01'
GROUP BY c.customer_code, c.customer_name
ORDER BY sales_amount DESC, c.customer_code
LIMIT 10;

-- Q123
WITH monthly AS (
    SELECT date_trunc('month', o.confirmed_date)::date AS month,
           SUM(d.sales_amount) AS amount
    FROM fact_sales_order o
    JOIN fact_sales_detail d ON d.order_id = o.order_id
    WHERE o.order_status = 'completed'
      AND o.confirmed_date >= DATE '2010-12-01'
      AND o.confirmed_date < DATE '2012-01-01'
    GROUP BY 1
)
SELECT a.month, a.amount,
       b.amount AS previous_amount,
       (a.amount - b.amount) / NULLIF(b.amount, 0) AS mom_ratio
FROM monthly a
LEFT JOIN monthly b ON b.month = (a.month - INTERVAL '1 month')::date
WHERE a.month >= DATE '2011-01-01'
ORDER BY a.month;

-- Q124
WITH monthly AS (
    SELECT date_trunc('month', o.confirmed_date)::date AS month,
           SUM(d.sales_amount) AS amount
    FROM fact_sales_order o
    JOIN fact_sales_detail d ON d.order_id = o.order_id
    WHERE o.order_status = 'completed'
      AND o.confirmed_date >= DATE '2011-01-01'
      AND o.confirmed_date < DATE '2012-01-01'
    GROUP BY 1
)
SELECT month, amount,
       SUM(amount) OVER (
           ORDER BY month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
       ) AS cumulative_amount
FROM monthly
ORDER BY month;

-- Q125
WITH monthly_country AS (
    SELECT date_trunc('month', o.confirmed_date)::date AS month,
           r.region_code, r.region_name,
           SUM(d.sales_amount) AS amount
    FROM fact_sales_order o
    JOIN fact_sales_detail d ON d.order_id = o.order_id
    JOIN dim_region r ON r.region_id = o.region_id
    WHERE o.order_status = 'completed'
      AND o.confirmed_date >= DATE '2011-01-01'
      AND o.confirmed_date < DATE '2012-01-01'
    GROUP BY 1, r.region_code, r.region_name
), ranked AS (
    SELECT *, DENSE_RANK() OVER (
        PARTITION BY month ORDER BY amount DESC
    ) AS amount_rank
    FROM monthly_country
)
SELECT * FROM ranked
WHERE amount_rank <= 3
ORDER BY month, amount_rank, region_code;

-- Q126
WITH first_seen AS (
    SELECT customer_id, MIN(confirmed_date) AS first_date
    FROM fact_sales_order
    WHERE order_status = 'completed' AND confirmed_date IS NOT NULL
    GROUP BY customer_id
)
SELECT date_trunc('month', first_date)::date AS month,
       COUNT(*) AS newly_observed_customers
FROM first_seen
WHERE first_date >= DATE '2011-01-01'
  AND first_date < DATE '2012-01-01'
GROUP BY 1
ORDER BY 1;

-- Q127
SELECT d.order_detail_id, d.order_id
FROM fact_sales_detail d
LEFT JOIN fact_sales_order o ON o.order_id = d.order_id
LEFT JOIN dim_product p ON p.product_id = d.product_id
LEFT JOIN dim_customer c ON c.customer_id = o.customer_id
LEFT JOIN dim_region r ON r.region_id = o.region_id
WHERE o.order_id IS NULL OR p.product_id IS NULL
   OR c.customer_id IS NULL OR r.region_id IS NULL
   OR d.customer_id <> o.customer_id
   OR d.region_id <> o.region_id
   OR d.order_date <> o.order_date
ORDER BY d.order_detail_id
LIMIT 20;

-- Q128
SELECT order_id, line_number, COUNT(*) AS occurrences
FROM fact_sales_detail
GROUP BY order_id, line_number
HAVING COUNT(*) > 1
ORDER BY order_id, line_number;

-- Q129
SELECT
    (SELECT COUNT(*) FROM fact_sales_order WHERE order_status = 'completed')
        AS completed_orders,
    COUNT(DISTINCT o.order_id) AS orders_with_details,
    COUNT(*) AS detail_rows,
    SUM(d.sales_amount) AS total_amount
FROM fact_sales_detail d
JOIN fact_sales_order o ON o.order_id = d.order_id
WHERE o.order_status = 'completed';

-- Q130
EXPLAIN (ANALYZE, BUFFERS)
SELECT SUM(d.sales_amount)
FROM fact_sales_order o
JOIN fact_sales_detail d ON d.order_id = o.order_id
WHERE o.order_status = 'completed'
  AND o.confirmed_date >= DATE '2011-02-01'
  AND o.confirmed_date < DATE '2011-03-01';

COMMIT;
