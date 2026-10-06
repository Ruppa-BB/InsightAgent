"""Calendar-month MoM. Missing months never borrow an older observed baseline."""
from backend.app.data_management.metric_versions import pinned_execution, evidence

from datetime import date
from decimal import Decimal

from sqlalchemy import Connection

from backend.app.db import read_connection
from backend.app.tools.schemas import DateRange, SalesFilters, SalesQueryRequest
from backend.app.tools.sql_allowlist import query_sales


def next_month(value: date) -> date:
    return date(value.year + 1, 1, 1) if value.month == 12 else date(value.year, value.month + 1, 1)


def previous_month(value: date) -> date:
    return date(value.year - 1, 12, 1) if value.month == 1 else date(value.year, value.month - 1, 1)


def calculate_monthly_mom(period: DateRange, values: dict[date, Decimal], coverage: dict | None = None) -> list[dict]:
    from backend.app.tools.quality_tool import month_is_covered
    rows = []
    month = period.start_date
    while month < period.end_date:
        current, previous = values.get(month), values.get(previous_month(month))
        incomplete=coverage is not None and (not month_is_covered(month,coverage) or not month_is_covered(previous_month(month),coverage))
        reason = 'incomplete_period' if incomplete else 'missing_month' if current is None or previous is None else 'zero_baseline' if previous == 0 else None
        rows.append({'month': month, 'sales_amount': current, 'previous_sales_amount': previous,
            'sales_mom': (current - previous) / previous if reason is None else None,
            'null_reason': reason})
        month = next_month(month)
    return rows


@pinned_execution
def query_sales_mom(period: DateRange, filters: SalesFilters, connection: Connection | None = None) -> dict:
    metric_definitions = evidence(['sales_amount','sales_mom'])
    if period.start_date.day != 1 or period.end_date.day != 1 or period.start_date <= date(1, 1, 1):
        raise ValueError('环比查询请使用完整月份范围，起止日期均为每月 1 日，起始月份须有可表示的上月。')
    if connection is None:
        with read_connection() as conn:
            return query_sales_mom(period, filters, conn)
    tools = [query_sales(SalesQueryRequest(metric_code='sales_amount', group_by='month', limit=500,
                filters=filters, **period.model_dump()), connection),
             query_sales(SalesQueryRequest(metric_code='sales_amount', group_by='month', limit=500,
                filters=filters, start_date=previous_month(period.start_date), end_date=period.start_date), connection)]
    values = {row['dimension']: Decimal(row['value']) for tool in tools for row in tool['rows']}
    from backend.app.tools.analysis_tool import query_coverage
    coverage=query_coverage(connection)
    return {'metric_definitions':metric_definitions, 'metric_code':'sales_mom', 'unit':'ratio', 'request':period.model_dump(mode='json'),
        'rows':calculate_monthly_mom(period, values, coverage), 'tools':tools,
        'coverage':coverage, 'warnings':['当前或上月覆盖不足、缺月或上月为0时环比返回null；覆盖边界完整不代表逐日质量完整。']}
