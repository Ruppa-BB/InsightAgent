"""Accounting attribution: dimension deltas explain amounts, not business causality."""
from decimal import Decimal

from sqlalchemy import Connection, text

from backend.app.errors import ServiceError
from backend.app.tools.schemas import DateRange, SalesFilters
from backend.app.tools.sql_allowlist import BASE_FROM, DIMENSION_SQL, FILTER_SQL

ZERO = Decimal('0')
MAX_GROUPS = 10000
MAX_PAIR_GROUPS = 100000


def change_values(previous: Decimal, current: Decimal) -> dict:
    delta = current - previous
    return {'previous': previous, 'current': current, 'delta': delta,
            'change_ratio': delta / previous if previous != 0 else None}


def reconcile_contributions(rows: list[dict], previous: Decimal, current: Decimal) -> dict:
    previous_sum = sum((Decimal(row['previous']) for row in rows), ZERO)
    current_sum = sum((Decimal(row['current']) for row in rows), ZERO)
    if previous_sum != previous or current_sum != current:
        raise ServiceError('reconciliation_failed', '维度贡献与总额不一致，已停止生成结论。', 409)
    total_delta = current - previous
    contributions = []
    for row in rows:
        before, after = Decimal(row['previous']), Decimal(row['current'])
        contributions.append({**row, **change_values(before, after),
            'status': 'new' if row['previous_count'] == 0 else 'lost' if row['current_count'] == 0 else 'continuing',
            'share_of_net_change': (after - before) / total_delta if total_delta else None})
    negative = sorted((r for r in contributions if r['delta'] < 0), key=lambda r: (r['delta'], str(r['dimension_key'])))
    positive = sorted((r for r in contributions if r['delta'] > 0), key=lambda r: (-r['delta'], str(r['dimension_key'])))
    shown = negative[:10] + positive[:10]
    return {'reconciled': True, 'previous_sum': previous_sum, 'current_sum': current_sum,
            'delta_sum': total_delta, 'group_count': len(rows),
            'negative': negative[:10], 'positive': positive[:10],
            'other_delta': total_delta - sum((r['delta'] for r in shown), ZERO),
            'note': '各维度分别解释同一个总差额，不能跨维度累加；贡献比例可为负或超过 100%。'}


def query_contributions(connection: Connection, dimension: str, previous_period: DateRange,
                        current_period: DateRange, filters: SalesFilters,
                        previous: Decimal, current: Decimal) -> dict:
    if dimension not in ('customer', 'product', 'country', 'customer_product'):
        raise ValueError('不支持的贡献维度')
    pair = dimension == 'customer_product'
    dim = ({'key': "jsonb_build_array(c.customer_code,p.product_code)",
            'expression': "c.customer_name || ' × ' || p.product_code || ' - ' || p.product_name"} if pair else DIMENSION_SQL[dimension])
    max_groups = MAX_PAIR_GROUPS if pair else MAX_GROUPS
    filter_values = filters.model_dump(exclude_none=True)
    extra = ''.join(f' AND {FILTER_SQL[name]}' for name in filter_values)
    before = '(o.confirmed_date >= :previous_start AND o.confirmed_date < :previous_end)'
    after = '(o.confirmed_date >= :current_start AND o.confirmed_date < :current_end)'
    query = text(f'''SELECT {dim['key']} AS dimension_key, {dim['expression']} AS dimension,
        COALESCE(SUM(d.sales_amount) FILTER (WHERE {before}), 0) AS previous,
        COALESCE(SUM(d.sales_amount) FILTER (WHERE {after}), 0) AS current,
        COUNT(*) FILTER (WHERE {before}) AS previous_count,
        COUNT(*) FILTER (WHERE {after}) AS current_count
        {BASE_FROM}
        WHERE o.order_status = 'completed' AND ({before} OR {after}){extra}
        GROUP BY {dim['key']}, {dim['expression']}
        ORDER BY {dim['key']} LIMIT :max_groups''')
    params = {'previous_start': previous_period.start_date, 'previous_end': previous_period.end_date,
              'current_start': current_period.start_date, 'current_end': current_period.end_date,
              'max_groups': max_groups + 1, **filter_values}
    rows = [dict(row) for row in connection.execute(query, params).mappings().all()]
    if len(rows) > max_groups:
        raise ServiceError('too_many_groups', f'贡献对象超过 {max_groups} 个，请增加筛选条件；不会使用截断数据归因。')
    for row in rows:
        row['drill_filters'] = (dict(zip(('customer_code','product_code'), row['dimension_key'])) if pair else
                               { {'customer':'customer_code','product':'product_code','country':'country'}[dimension]: row['dimension'] if dimension == 'country' else row['dimension_key'] })
    return {'dimension': dimension, **reconcile_contributions(rows, previous, current),
            'sql': str(query), 'parameters': params}


def query_coverage(connection: Connection) -> dict:
    row = connection.execute(text('''SELECT MIN(confirmed_date) AS start_date,
        MAX(confirmed_date) AS last_date FROM fact_sales_order WHERE order_status = 'completed' ''' )).mappings().one()
    return dict(row)


def query_product_structure(connection: Connection, period: DateRange, filters: SalesFilters) -> dict:
    """Full product distribution before Top display; reconcile with scalar sales."""
    from backend.app.tools.sql_allowlist import query_sales
    from backend.app.tools.schemas import SalesQueryRequest
    total_tool=query_sales(SalesQueryRequest(metric_code='sales_amount',**period.model_dump(),filters=filters),connection)
    total=Decimal(total_tool['rows'][0]['value'])
    filter_values=filters.model_dump(exclude_none=True)
    extra=''.join(f' AND {FILTER_SQL[name]}' for name in filter_values)
    query=text(f'''SELECT p.product_code AS dimension_key,
        p.product_code || ' - ' || p.product_name AS dimension, SUM(d.sales_amount) AS value,
        COUNT(*) AS observation_count {BASE_FROM}
        WHERE o.order_status='completed' AND o.confirmed_date>=:start_date AND o.confirmed_date<:end_date{extra}
        GROUP BY p.product_code,p.product_name ORDER BY value DESC,p.product_code LIMIT :max_groups''')
    params={**period.model_dump(),'max_groups':MAX_GROUPS+1,**filter_values}
    rows=[dict(row) for row in connection.execute(query,params).mappings().all()]
    if len(rows)>MAX_GROUPS:
        raise ServiceError('too_many_groups','商品超过10000组，停止结构分析，请增加筛选。')
    if sum((Decimal(r['value']) for r in rows),ZERO)!=total:
        raise ServiceError('reconciliation_failed','商品结构与销售额总额不一致。',409)
    for row in rows:
        row['sales_share']=Decimal(row['value'])/total if total else None
    return {'rows':rows,'row_count':len(rows),'group_count':len(rows),'empty':not rows,'truncated':False,
            'unit':'GBP','total_sales':total,'reconciled':True,'warnings':['销售额结构不等于销量结构或毛利结构；零总额时占比不可计算。'],
            'sql':str(query),'parameters':params,'total_tool':total_tool}
