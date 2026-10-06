"""Observed data checks; absent transactions are not measured missing records."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy import Connection, text

from backend.app.config import ROOT
from backend.app.db import read_connection
from backend.app.serialization import json_ready
from backend.app.tools.mom_tool import next_month
from backend.app.tools.schemas import DateRange

IMPORT_REPORT = ROOT / 'data/generated/online_retail_ii/import_report.json'


def month_is_covered(month: date, coverage: dict) -> bool:
    return (coverage['start_date'] is not None and coverage['last_date'] is not None
            and month >= coverage['start_date']
            and next_month(month)-timedelta(days=1) <= coverage['last_date'])


def monthly_coverage(period: DateRange, coverage: dict, observed: list[dict]) -> list[dict]:
    by_month={row['month']:row for row in observed}
    rows=[]
    month=period.start_date
    while month<period.end_date:
        row=by_month.get(month)
        covered=month_is_covered(month,coverage)
        rows.append({'month':month,'coverage_status':'within_bounds' if covered else 'partial_or_outside',
                     'has_records':row is not None,'empty':row is None,
                     'observation_days':row['observation_days'] if row else 0,
                     'detail_count':row['detail_count'] if row else 0,
                     'sales_amount':row['sales_amount'] if row else None,
                     'eligible_for_detection':covered and row is not None,
                     'missing_rate':None})
        month=next_month(month)
    return rows


def read_import_history(stats: dict, path: Path = IMPORT_REPORT) -> dict:
    if not path.is_file():
        return {'status':'unavailable','counts':None,'note':'没有历史导入报告，不能回推原始缺失率或排除数量。'}
    try:
        if path.stat().st_size>65536:
            raise ValueError('too large')
        raw=path.read_bytes()
        report=json.loads(raw)
        fields=('raw_rows','clean_rows','cancelled_rows','invalid_rows','orders','customers','products','regions')
        counts={field:report[field] for field in fields}
        if any(type(value) is not int or value<0 for value in counts.values()) or counts['raw_rows']-counts['clean_rows']!=counts['invalid_rows'] or counts['cancelled_rows']>counts['invalid_rows']:
            raise ValueError('invalid report')
        sales=Decimal(str(report['sales_amount']))
        if not sales.is_finite() or sales<0:
            raise ValueError('invalid amount')
        matched=(counts['clean_rows']==stats['details'] and counts['orders']==stats['orders']
                 and abs(sales-Decimal(str(stats['sales_amount'])))<=Decimal('.01'))
        return {'status':'summary_matches' if matched else 'summary_mismatch','counts':counts,
                'reported_sales_amount':str(sales),'report_sha256':sha256(raw).hexdigest(),
                'note':'历史清洗排除数；取消记录已包含在invalid_rows中，不能相加。汇总匹配不能证明原始文件逐行一致，也不能推算月度缺失率。'}
    except (OSError,ValueError,KeyError,TypeError,ArithmeticError):
        return {'status':'invalid_report','counts':None,'note':'历史导入报告无法验证；当前数据库检查继续独立执行。'}


def current_quality(connection: Connection) -> dict:
    stats=dict(connection.execute(text('''SELECT COUNT(*) AS details,
        COUNT(DISTINCT o.order_id) AS orders, MIN(o.confirmed_date) AS start_date,
        MAX(o.confirmed_date) AS last_date, COALESCE(SUM(d.sales_amount),0) AS sales_amount
        FROM fact_sales_detail d JOIN fact_sales_order o ON o.order_id=d.order_id
        WHERE o.order_status='completed' ''')).mappings().one())
    detail=dict(connection.execute(text('''SELECT
        COUNT(*) FILTER (WHERE quantity IS NULL OR unit_price IS NULL OR sales_amount IS NULL OR order_id IS NULL OR product_id IS NULL OR customer_id IS NULL OR region_id IS NULL OR order_date IS NULL) AS null_required,
        COUNT(*) FILTER (WHERE quantity<=0 OR unit_price<0 OR sales_amount<0 OR unit_price::text IN ('NaN','Infinity','-Infinity') OR sales_amount::text IN ('NaN','Infinity','-Infinity')) AS invalid_numeric
        FROM fact_sales_detail''')).mappings().one())
    order=dict(connection.execute(text('''SELECT COUNT(*) FILTER (WHERE order_status='completed' AND confirmed_date IS NULL) AS null_confirmed_date,
        COUNT(*) FILTER (WHERE confirmed_date<order_date OR confirmed_date>CURRENT_DATE) AS invalid_date
        FROM fact_sales_order''')).mappings().one())
    checks={**detail,**order}
    checks['duplicate_order_lines']=connection.execute(text('SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM fact_sales_detail GROUP BY order_id,line_number HAVING COUNT(*)>1) t')).scalar_one()
    checks['duplicate_order_numbers']=connection.execute(text('SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM fact_sales_order GROUP BY order_number HAVING COUNT(*)>1) t')).scalar_one()
    checks['orphan_detail_context']=connection.execute(text('''SELECT COUNT(*) FROM fact_sales_detail d
        LEFT JOIN fact_sales_order o ON o.order_id=d.order_id
        LEFT JOIN dim_product p ON p.product_id=d.product_id
        LEFT JOIN dim_customer c ON c.customer_id=o.customer_id
        LEFT JOIN dim_region r ON r.region_id=o.region_id
        WHERE o.order_id IS NULL OR p.product_id IS NULL OR c.customer_id IS NULL OR r.region_id IS NULL
           OR d.customer_id<>o.customer_id OR d.region_id<>o.region_id OR d.order_date<>o.order_date''')).scalar_one()
    checks['blank_dimension_codes']=connection.execute(text('''SELECT
        (SELECT COUNT(*) FROM dim_customer WHERE customer_code IS NULL OR btrim(customer_code)='')+
        (SELECT COUNT(*) FROM dim_product WHERE product_code IS NULL OR btrim(product_code)='')+
        (SELECT COUNT(*) FROM dim_region WHERE region_code IS NULL OR btrim(region_code)='')''')).scalar_one()
    rules=[{'rule':name,'count':int(count),'status':'failed' if count else 'passed'} for name,count in checks.items()]
    signature=json_ready({'summary':stats,'checks':checks})
    return {'summary':stats,'rules':rules,'blocking':any(checks.values()),
            'data_version':sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest(),
            'version_note':'汇总与质量统计指纹，不是逐行内容哈希；固定本地样本用于复现。',
            'type_check':{'status':'database_typed','note':'PostgreSQL列类型约束存储值；不能据此证明原始文件没有类型错误。'}}


def query_data_quality(period: DateRange, connection: Connection | None = None) -> dict:
    if period.start_date.day!=1 or period.end_date.day!=1:
        raise ValueError('月度质量报告起止日期须为每月1日。')
    if connection is None:
        with read_connection() as conn:
            return query_data_quality(period,conn)
    current=current_quality(connection)
    sql='''SELECT date_trunc('month',o.confirmed_date)::date AS month,
        COUNT(DISTINCT o.confirmed_date) AS observation_days,COUNT(*) AS detail_count,
        SUM(d.sales_amount) AS sales_amount FROM fact_sales_order o
        JOIN fact_sales_detail d ON d.order_id=o.order_id WHERE o.order_status='completed'
        AND o.confirmed_date>=:start_date AND o.confirmed_date<:end_date
        GROUP BY month ORDER BY month'''
    params=period.model_dump()
    observed=[dict(row) for row in connection.execute(text(sql),params).mappings()]
    months=monthly_coverage(period,current['summary'],observed)
    warnings=['没有交易的日期不等于数据缺失；缺失率未知，不用“有记录天数/自然日”计算缺失率。',
              '覆盖边界完整只表示整个日历月在样本范围内，不证明逐日数据质量完整。']
    if current['blocking']:
        warnings.append('当前质量规则发现问题，暂停生成可信异常判断；先核查数据。')
    return {'source':'Online Retail II / PostgreSQL','unit':'GBP','checked_at':datetime.now(timezone.utc),
            'period':period.model_dump(mode='json'),'current':current,'months':months,
            'historical_cleaning':read_import_history(current['summary']),
            'warnings':warnings,'sql':sql,'parameters':params}
