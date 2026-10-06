"""Monthly sales YoY with explicit coverage and missing/zero-baseline states."""
from backend.app.data_management.metric_versions import pinned_execution, evidence

from datetime import date
from decimal import Decimal

from sqlalchemy import Connection

from backend.app.db import read_connection
from backend.app.tools.analysis_tool import query_coverage
from backend.app.tools.quality_tool import month_is_covered
from backend.app.tools.mom_tool import next_month
from backend.app.tools.schemas import DateRange, SalesFilters, SalesQueryRequest
from backend.app.tools.sql_allowlist import query_sales


def calculate_monthly_yoy(period: DateRange, values: dict[date, Decimal], coverage: dict) -> list[dict]:
    rows=[]
    month=period.start_date
    while month < period.end_date:
        baseline=month.replace(year=month.year-1)
        current,previous=values.get(month),values.get(baseline)
        complete=lambda start: month_is_covered(start,coverage)
        reason=('incomplete_period' if not complete(month) or not complete(baseline)
                else 'missing_month' if current is None or previous is None
                else 'zero_baseline' if previous==0 else None)
        rows.append({'month':month,'baseline_month':baseline,'sales_amount':current,
                     'previous_year_sales_amount':previous,'sales_yoy':(current-previous)/previous if reason is None else None,
                     'null_reason':reason})
        month=next_month(month)
    return rows


@pinned_execution
def query_sales_yoy(period: DateRange, filters: SalesFilters, connection: Connection | None = None) -> dict:
    metric_definitions = evidence(['sales_amount','sales_yoy'])
    if period.start_date.day!=1 or period.end_date.day!=1 or period.start_date.year<=1:
        raise ValueError('同比使用完整月份范围，起止日期须为每月1日，起始年份须有可表示的去年同期。')
    if connection is None:
        with read_connection() as conn:
            return query_sales_yoy(period,filters,conn)
    period=DateRange(start_date=period.start_date,end_date=period.end_date)
    baseline=DateRange(start_date=period.start_date.replace(year=period.start_date.year-1),
                       end_date=period.end_date.replace(year=period.end_date.year-1))
    tools=[query_sales(SalesQueryRequest(metric_code='sales_amount',group_by='month',limit=500,filters=filters,
                                        **p.model_dump()),connection) for p in (period,baseline)]
    values={r['dimension']:Decimal(r['value']) for tool in tools for r in tool['rows']}
    coverage=query_coverage(connection)
    return {'metric_definitions':metric_definitions, 'metric_code':'sales_yoy','unit':'ratio','request':period.model_dump(mode='json')|{'filters':filters.model_dump(mode='json')},
            'coverage':coverage,'rows':calculate_monthly_yoy(period,values,coverage),'tools':tools,
            'warnings':['仅比较完整日历月份；数据覆盖不足、缺月或去年同月为0时同比返回null。覆盖范围完整不代表逐日数据质量已验证。']}
