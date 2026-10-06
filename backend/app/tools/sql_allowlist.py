"""Fixed SELECT templates. No API accepts SQL, identifiers or JOIN expressions."""
from backend.app.data_management.metric_versions import pinned_execution, evidence

from sqlalchemy import Connection, text
from sqlalchemy.sql.elements import TextClause

from backend.app.db import read_connection
from backend.app.semantic.metrics import get_metric
from backend.app.tools.schemas import SalesQueryRequest

METRIC_SQL = {
    'sales_quantity': 'COALESCE(SUM(d.quantity), 0)',
    'average_order_value': 'SUM(d.sales_amount) / NULLIF(COUNT(DISTINCT o.order_id), 0)',
    'average_selling_price': 'SUM(d.sales_amount) / NULLIF(SUM(d.quantity), 0)',
    'sales_amount': 'COALESCE(SUM(d.sales_amount), 0)',
    'order_count': 'COUNT(DISTINCT o.order_id)',
    'customer_count': 'COUNT(DISTINCT o.customer_id)',
}
DIMENSION_SQL = {
    'month': {'expression': "date_trunc('month', o.confirmed_date)::date", 'key': "date_trunc('month', o.confirmed_date)::date", 'join': ''},
    'customer': {'expression': 'c.customer_name', 'key': 'c.customer_code', 'join': 'JOIN dim_customer AS c ON c.customer_id = o.customer_id'},
    'product': {'expression': "p.product_code || ' - ' || p.product_name", 'key': 'p.product_code', 'join': 'JOIN dim_product AS p ON p.product_id = d.product_id'},
    'country': {'expression': 'r.region_name', 'key': 'r.region_code', 'join': 'JOIN dim_region AS r ON r.region_id = o.region_id'},
}
FILTER_SQL = {
    'country': 'r.region_name = :country',
    'customer_code': 'c.customer_code = :customer_code',
    'product_code': 'p.product_code = :product_code',
}
BASE_FROM = '''FROM fact_sales_order AS o
JOIN fact_sales_detail AS d ON d.order_id = o.order_id
JOIN dim_customer AS c ON c.customer_id = o.customer_id
JOIN dim_product AS p ON p.product_id = d.product_id
JOIN dim_region AS r ON r.region_id = o.region_id'''


def build_sales_query(metric_code: str, group_by: str | None, filter_names: tuple[str, ...] = ()) -> TextClause:
    if metric_code not in METRIC_SQL:
        raise ValueError(f'不支持的指标: {metric_code}')
    if group_by is not None and group_by not in DIMENSION_SQL:
        raise ValueError(f'不支持的分组维度: {group_by}')
    metric = get_metric(metric_code)
    if group_by is not None and group_by not in metric['dimensions']:
        raise ValueError(f'{metric["name"]}不支持此维度: {group_by}')
    if any(name not in FILTER_SQL for name in filter_names):
        raise ValueError('不支持的筛选字段')
    filters = ''.join(f' AND {FILTER_SQL[name]}' for name in filter_names)
    where = f"WHERE o.order_status = 'completed' AND o.confirmed_date >= :start_date AND o.confirmed_date < :end_date{filters}"
    value = METRIC_SQL[metric_code]
    if group_by is None:
        return text(f'SELECT {value} AS value, COUNT(*) AS observation_count {BASE_FROM} {where}')
    dim = DIMENSION_SQL[group_by]
    return text(f'''SELECT {dim['key']} AS dimension_key, {dim['expression']} AS dimension,
        {value} AS value, COUNT(*) AS observation_count
        {BASE_FROM} {where}
        GROUP BY {dim['key']}, {dim['expression']}
        ORDER BY {dim['key']} LIMIT :limit''')


@pinned_execution
def query_sales(request: SalesQueryRequest, connection: Connection | None = None) -> dict:
    metric_definitions = evidence([request.metric_code])
    if connection is None:
        with read_connection() as conn:
            return query_sales(request, conn)
    filters = request.filters.model_dump(exclude_none=True)
    query = build_sales_query(request.metric_code, request.group_by, tuple(filters))
    params = {'start_date': request.start_date, 'end_date': request.end_date,
              'limit': request.limit + 1, **filters}
    rows = [dict(row) for row in connection.execute(query, params).mappings().all()]
    truncated = len(rows) > request.limit
    rows = rows[:request.limit]
    return {
        'metric_definitions': metric_definitions,
        'metric_code': request.metric_code, 'unit': get_metric(request.metric_code)['unit'],
        'request': request.model_dump(mode='json'), 'rows': rows,
        'row_count': len(rows), 'truncated': truncated,
        'empty': not rows or all(row['observation_count'] == 0 for row in rows),
        'sql': str(query), 'parameters': params,
        'source': 'PostgreSQL / Online Retail II',
        'warnings': (['结果已截断，不可作为完整贡献分析输入。'] if truncated else []) +
                    (['没有匹配记录或分母为 0，平均值返回 null。'] if any(row['value'] is None for row in rows) else []),
    }


def run_sales_query(request: SalesQueryRequest) -> list[dict]:
    """Compatibility for the learning CLI and original endpoint."""
    return query_sales(request)['rows']
