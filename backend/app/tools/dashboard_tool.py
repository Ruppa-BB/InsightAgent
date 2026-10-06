"""Retail overview with deterministic drill-through and read-only evidence."""
from backend.app.data_management.metric_versions import pinned_execution, evidence

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Connection, text

from backend.app.db import read_connection
from backend.app.errors import ServiceError
from backend.app.semantic.metrics import get_metric
from backend.app.tools.anomaly_tool import query_anomalies, shift_month
from backend.app.tools.quality_tool import current_quality, month_is_covered
from backend.app.tools.schemas import AnomalyRequest, SalesQueryRequest
from backend.app.tools.sql_allowlist import query_sales, BASE_FROM, DIMENSION_SQL


@pinned_execution
def query_dashboard(month: date | None = None, connection: Connection | None = None) -> dict:
    metric_definitions = evidence(['sales_amount','order_count','customer_count','sales_quantity','average_order_value','average_selling_price'])
    if connection is None:
        with read_connection() as conn:
            return query_dashboard(month, conn)
    quality = current_quality(connection)
    coverage = quality['summary']
    if coverage['last_date'] is None:
        raise ServiceError('empty_data', '没有可用于经营总览的销售记录。', 409)
    if month is None:
        month = coverage['last_date'].replace(day=1)
        if not month_is_covered(month, coverage):
            month = shift_month(month, -1)
    if month.day != 1 or month.year <= 1 or month.year >= 9999:
        raise ValueError('总览月份须为每月1日，且能够表示前后月份。')
    if not month_is_covered(month, coverage):
        raise ServiceError('incomplete_period', '所选月份不在完整覆盖范围，请选择完整月份。', 422)
    if quality['blocking']:
        raise ServiceError('data_quality_failed', '当前数据质量规则失败，已暂停经营总览。', 409)
    end, previous = shift_month(month, 1), shift_month(month, -1)
    tools, cards = [], []
    for metric in ('sales_amount', 'order_count', 'customer_count', 'sales_quantity', 'average_order_value', 'average_selling_price'):
        values = []
        for start, finish in ((month, end), (previous, month)):
            tool = query_sales(SalesQueryRequest(metric_code=metric, start_date=start, end_date=finish), connection)
            tools.append(tool)
            row = tool['rows'][0]
            values.append(row['value'] if row['observation_count'] else None)
        current, baseline = values
        reason = ('incomplete_baseline' if not month_is_covered(previous, coverage) else
                  'missing_data' if current is None or baseline is None else 'zero_baseline' if baseline == 0 else None)
        cards.append({'metric_code': metric, 'name': get_metric(metric)['name'], 'unit': get_metric(metric)['unit'],
                      'value': current, 'previous': baseline, 'change_ratio': (Decimal(current)-Decimal(baseline))/Decimal(baseline) if reason is None else None,
                      'null_reason': reason})
    trend_start = max(shift_month(month, -11), coverage['start_date'].replace(day=1))
    trend = query_sales(SalesQueryRequest(metric_code='sales_amount', start_date=trend_start, end_date=end, group_by='month', limit=500), connection)
    tools.append(trend)
    # Rank in SQL before LIMIT; alphabetical query output is never a Top ranking.
    rankings = {}
    for dimension in ('customer', 'product'):
        dim = DIMENSION_SQL[dimension]
        query = text(f"""SELECT {dim['key']} AS dimension_key, {dim['expression']} AS dimension,
            SUM(d.sales_amount) AS value {BASE_FROM}
            WHERE o.order_status='completed' AND o.confirmed_date >= :start_date AND o.confirmed_date < :end_date
            GROUP BY {dim['key']}, {dim['expression']}
            ORDER BY value DESC, {dim['key']} LIMIT 10""")
        params = {'start_date': month, 'end_date': end}
        rankings[dimension] = [dict(row) for row in connection.execute(query, params).mappings().all()]
        tools.append({'sql': str(query), 'parameters': params})
    detection = query_anomalies(AnomalyRequest(start_date=trend_start, end_date=end), connection, quality)
    alerts = []
    for row in detection['rows']:
        triggered = [check for check in row['checks'] if check['status'] == 'triggered']
        if triggered:
            target = row['month']
            alerts.append({'month': target, 'checks': triggered, 'intent': {
                'action': 'compare', 'metric_code': 'sales_amount', 'group_by': 'month', 'filters': {},
                'period': {'start_date': target, 'end_date': shift_month(target, 1)},
                'previous_period': {'start_date': shift_month(target, -1), 'end_date': target}}})
    return {'metric_definitions':metric_definitions, 'dataset_source':getattr(connection,'info',{}).get('dataset_source',{'version_id':'baseline-v1'}), 'source': 'Online Retail II', 'unit': 'GBP', 'checked_at': datetime.now(timezone.utc),
            'month': month, 'period': {'start_date': month, 'end_date': end}, 'previous_month': previous,
            'quality': quality, 'cards': cards, 'trend': trend['rows'], 'rankings': rankings,
            'alerts': list(reversed(alerts)), 'detection': detection, 'tools': tools,
            'warnings': ['指标卡显示相对上月的变化，不是异常判定。', 'Top排行只展示当月销售额前10名，不代表完整贡献。',
                         '提醒使用20%阈值与前三月均值；不是业务原因，未调整季节性或月份天数。',
                         '覆盖完整只表示日期边界覆盖，不证明逐日数据完整；零售样本没有毛利数据。']}
