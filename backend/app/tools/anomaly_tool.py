"""Configured change alerts, not causal claims or seasonality-adjusted ML."""
from backend.app.data_management.metric_versions import pinned_execution, evidence

from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json

from sqlalchemy import Connection

from backend.app.db import read_connection
from backend.app.serialization import json_ready
from backend.app.tools.mom_tool import next_month
from backend.app.tools.quality_tool import current_quality, month_is_covered
from backend.app.tools.schemas import AnomalyRequest, SalesQueryRequest
from backend.app.tools.sql_allowlist import query_sales


def shift_month(month: date, offset: int) -> date:
    year, zero_month=divmod(month.year*12+month.month-1+offset,12)
    return date(year,zero_month+1,1)


def detect_months(request: AnomalyRequest, values: dict[date, Decimal], coverage: dict, blocking: bool=False) -> list[dict]:
    rows=[]
    month=request.start_date
    drop=Decimal(str(request.mom_drop_threshold))
    deviation=Decimal(str(request.moving_average_threshold))
    while month<request.end_date:
        current=values.get(month)
        common='quality_failed' if blocking else 'incomplete_period' if not month_is_covered(month,coverage) else 'missing_month' if current is None else None
        before_month=shift_month(month,-1)
        previous=values.get(before_month)
        reason=common or ('incomplete_baseline' if not month_is_covered(before_month,coverage)
                          else 'missing_baseline' if previous is None else 'zero_baseline' if previous==0 else None)
        ratio=(current-previous)/previous if reason is None else None
        mom={'method':'mom_drop','status':'blocked' if blocking else 'unavailable' if reason else 'triggered' if ratio < -drop else 'normal',
             'reason':reason,'baseline':previous,'baseline_months':[before_month],
             'observed_value':current,'change_ratio':ratio,'threshold':drop,'operator':'change_ratio < -threshold'}
        history=[shift_month(month,-offset) for offset in range(request.moving_average_window,0,-1)]
        reason=common or ('insufficient_history' if not all(month_is_covered(m,coverage) for m in history)
                          else 'missing_baseline' if any(m not in values for m in history) else None)
        baseline=sum((values[m] for m in history),Decimal(0))/len(history) if reason is None else None
        if baseline==0:
            reason='zero_baseline'
        ratio=(current-baseline)/baseline if reason is None else None
        ma={'method':'moving_average_deviation','status':'blocked' if blocking else 'unavailable' if reason else 'triggered' if abs(ratio)>deviation else 'normal',
            'reason':reason,'baseline':baseline,'baseline_months':history,'window':len(history),
            'observed_value':current,'change_ratio':ratio,'threshold':deviation,'operator':'abs(change_ratio) > threshold'}
        rows.append({'month':month,'sales_amount':current,'checks':[mom,ma],
                     'triggered':any(check['status']=='triggered' for check in (mom,ma))})
        month=next_month(month)
    return rows


@pinned_execution
def query_anomalies(request: AnomalyRequest, connection: Connection | None = None, quality: dict | None = None) -> dict:
    metric_definitions = evidence(['sales_amount'])
    if connection is None:
        with read_connection() as conn:
            return query_anomalies(request,conn)
    quality=current_quality(connection) if quality is None else quality
    history_start=shift_month(request.start_date,-request.moving_average_window)
    # Separate history from requested period to keep each query within the five-year bound.
    tools=[query_sales(SalesQueryRequest(metric_code='sales_amount',group_by='month',limit=500,
                start_date=start,end_date=end,filters=request.filters),connection)
           for start,end in ((request.start_date,request.end_date),(history_start,request.start_date))]
    values={row['dimension']:Decimal(row['value']) for tool in tools for row in tool['rows']}
    rows=detect_months(request,values,quality['summary'],quality['blocking'])
    version={'global_quality_version':quality['data_version'],'filters':request.filters.model_dump(mode='json'),
             'monthly_values':{str(k):str(v) for k,v in values.items()}}
    data_version=sha256(json.dumps(version,sort_keys=True).encode()).hexdigest()
    for row in rows:
        for check in row['checks']:
            check['event_id']=sha256((data_version+str(row['month'])+check['method']+json.dumps(request.model_dump(mode='json'),sort_keys=True)).encode()).hexdigest()[:24]
    return {'metric_definitions':metric_definitions, 'source':'Online Retail II / PostgreSQL','metric_code':'sales_amount','unit':'GBP',
            'checked_at':datetime.now(timezone.utc),'request':request.model_dump(mode='json'),
            'data_version':data_version,'current_quality':quality,'rows':rows,'tools':tools,
            'summary':{'months':len(rows),'triggered_months':sum(row['triggered'] for row in rows),
                       'triggered_checks':sum(c['status']=='triggered' for row in rows for c in row['checks']),
                       'unavailable_checks':sum(c['status'] in ('unavailable','blocked') for row in rows for c in row['checks'])},
            'warnings':['检测结果是配置阈值下的变化提醒，不能证明业务原因。',
                        '移动平均只使用当前月之前连续的完整月份，不含未来数据；未做季节性调整。',
                        '覆盖范围完整不等于逐日数据完整；当前质量规则失败时暂停异常判断。',
                        '当前比较月度总额，未按月份天数计算日均值。']}
