"""Versioned quality management around the existing mandatory Agent checks."""
from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.app.agent.store import connect
from backend.app.data_management.catalog import DATASET_ID, VERSION_ID, initialize
from backend.app.db import read_connection
from backend.app.errors import ServiceError
from backend.app.serialization import json_ready
from backend.app.tools.quality_tool import current_quality

router = APIRouter(prefix='/api/data/quality', tags=['数据质量管理'])
RULES = {
 'null_required': ('明细必填字段', 'fact_sales_detail', "quantity IS NULL OR unit_price IS NULL OR sales_amount IS NULL OR order_id IS NULL OR product_id IS NULL OR customer_id IS NULL OR region_id IS NULL OR order_date IS NULL"),
 'invalid_numeric': ('数量与金额合法性', 'fact_sales_detail', "quantity<=0 OR unit_price<0 OR sales_amount<0 OR unit_price::text IN ('NaN','Infinity','-Infinity') OR sales_amount::text IN ('NaN','Infinity','-Infinity')"),
 'null_confirmed_date': ('有效订单确认日期', 'fact_sales_order', "order_status='completed' AND confirmed_date IS NULL"),
 'invalid_date': ('订单日期顺序', 'fact_sales_order', 'confirmed_date<order_date OR confirmed_date>CURRENT_DATE'),
 'duplicate_order_lines': ('重复订单行', 'fact_sales_detail', None),
 'duplicate_order_numbers': ('重复订单编号', 'fact_sales_order', None),
 'orphan_detail_context': ('明细关联与订单上下文', 'fact_sales_detail', None),
 'blank_dimension_codes': ('维度业务编号非空', 'dimensions', None),
}


class RuleSetting(BaseModel):
    notification_threshold: int = Field(default=0, ge=0, le=10000)


class ConfigRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    sample_limit: int = Field(default=5,ge=1,le=20)
    rules: dict[str, RuleSetting]


def init(db) -> None:
    initialize(db)
    db.execute('''CREATE TABLE IF NOT EXISTS dm_quality_configs (
        revision INTEGER PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS dm_quality_checks (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, dataset_id TEXT NOT NULL,
        version_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS dm_check_time ON dm_quality_checks(created_at)')
    db.execute('INSERT OR IGNORE INTO dm_schema_versions VALUES (2,?)', (now(),))
    baseline={'sample_limit':5,'rules':{code:{'notification_threshold':0} for code in RULES}}
    db.execute('INSERT OR IGNORE INTO dm_quality_configs VALUES (1,?,?)',
               (now(),json.dumps(baseline)))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_config(db) -> dict:
    revision, created_at, payload = db.execute('SELECT revision,created_at,payload FROM dm_quality_configs ORDER BY revision DESC LIMIT 1').fetchone()
    return {'revision':revision,'created_at':created_at,**json.loads(payload)}


@router.get('/config')
def config() -> dict:
    with connect() as db:
        init(db)
        result=read_config(db)
    return {**result,'definitions':[{'code':code,'name':name,'table':table,'blocking_threshold':0}
                                   for code,(name,table,_) in RULES.items()],
            'note':'通知阈值只控制关注标记；八项硬质量规则始终零容忍，不能放宽Agent门禁。'}


@router.put('/config')
def save_config(request: ConfigRequest) -> dict:
    if set(request.rules)!=set(RULES):
        raise ServiceError('invalid_quality_config','必须提供全部八项允许规则，不能增加任意SQL或删除硬规则。',422)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        init(db)
        previous=read_config(db)
        if previous['revision']!=request.expected_revision:
            raise ServiceError('revision_conflict','配置已更新，请重新读取再保存。',409)
        values=request.model_dump(exclude={'expected_revision'})
        if all(values[key]==previous[key] for key in values):
            return previous
        revision=previous['revision']+1
        db.execute('INSERT INTO dm_quality_configs VALUES (?,?,?)',
                   (revision,now(),json.dumps(values)))
        return read_config(db)


def samples(connection, code: str, limit: int) -> list[dict]:
    """Fixed diagnostic templates; never return names, credentials or full rows."""
    _, table, predicate=RULES[code]
    if predicate:
        key='order_detail_id' if table=='fact_sales_detail' else 'order_id'
        sql=f'SELECT {key} AS record_id FROM {table} WHERE {predicate} ORDER BY {key} LIMIT :limit'
    elif code=='duplicate_order_lines':
        sql='SELECT order_id,line_number,COUNT(*) AS occurrences FROM fact_sales_detail GROUP BY order_id,line_number HAVING COUNT(*)>1 ORDER BY order_id,line_number LIMIT :limit'
    elif code=='duplicate_order_numbers':
        sql='SELECT MIN(order_id) AS record_id,COUNT(*) AS occurrences FROM fact_sales_order GROUP BY order_number HAVING COUNT(*)>1 ORDER BY MIN(order_id) LIMIT :limit'
    elif code=='orphan_detail_context':
        sql='''SELECT d.order_detail_id AS record_id FROM fact_sales_detail d
        LEFT JOIN fact_sales_order o ON o.order_id=d.order_id
        LEFT JOIN dim_product p ON p.product_id=d.product_id
        LEFT JOIN dim_customer c ON c.customer_id=o.customer_id
        LEFT JOIN dim_region r ON r.region_id=o.region_id
        WHERE o.order_id IS NULL OR p.product_id IS NULL OR c.customer_id IS NULL OR r.region_id IS NULL
        OR d.customer_id<>o.customer_id OR d.region_id<>o.region_id OR d.order_date<>o.order_date
        ORDER BY d.order_detail_id LIMIT :limit'''
    else:
        sql="""SELECT * FROM (
        SELECT 'dim_customer' AS asset,customer_id AS record_id FROM dim_customer WHERE customer_code IS NULL OR btrim(customer_code)=''
        UNION ALL SELECT 'dim_product',product_id FROM dim_product WHERE product_code IS NULL OR btrim(product_code)=''
        UNION ALL SELECT 'dim_region',region_id FROM dim_region WHERE region_code IS NULL OR btrim(region_code)='') q
        ORDER BY asset,record_id LIMIT :limit"""
    return [dict(row) for row in connection.execute(text(sql),{'limit':limit}).mappings()]


def persist(check: dict) -> None:
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        init(db)
        previous = db.execute('SELECT status,payload FROM dm_quality_checks WHERE id=?', (check['id'],)).fetchone()
        payload = json.dumps(json_ready(check),ensure_ascii=False)
        if previous and previous[0] != 'running' and json.loads(previous[1]) != json.loads(payload):
            raise ServiceError('immutable_check', '完成的检查快照不可覆盖。', 409)
        db.execute('''INSERT INTO dm_quality_checks VALUES (?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET version_id=excluded.version_id,status=excluded.status,payload=excluded.payload''',
        (check['id'],check['created_at'],check['dataset_id'],check['version_id'],check['status'],
         payload))
        from backend.app.data_management.issues import synchronize
        synchronize(db)


@router.post('/checks')
def run_check() -> dict:
    from backend.app.data_management.versions import REQUEST_VERSION, source_info
    source_info()
    cfg=config()
    check={'id':str(uuid4()),'created_at':now(),'dataset_id':DATASET_ID,'version_id':REQUEST_VERSION.get(),
           'status':'running','configuration':cfg}
    persist(check)
    try:
        with read_connection() as connection:
            source=getattr(connection,'info',{}).get('dataset_source',{'version_id':VERSION_ID})
            check['version_id']=source['version_id']
            check['dataset_source']=source
            result=current_quality(connection)
            rows=[]
            for rule in result['rules']:
                code=rule['rule']
                example=samples(connection,code,cfg['sample_limit']) if rule['count'] else []
                rows.append({**rule,'name':RULES[code][0],
                    'notification_threshold':cfg['rules'][code]['notification_threshold'],
                    'attention':rule['count']>cfg['rules'][code]['notification_threshold'],
                    'samples':example,'sample_note':'仅定位用有限样例；重复规则计数是多余行数，样例是重复分组，不可直接对等。'})
            check.update(status='completed',completed_at=now(),current={**result,'rules':rows})
        persist(check)
        return json_ready(check)
    except Exception as exc:
        error=exc if isinstance(exc,ServiceError) else ServiceError('quality_check_failed','质量检查未完成，请检查数据库和本地存储。',503)
        check.update(status='failed',completed_at=now(),error={'code':error.code,'message':error.message})
        # If storage is unavailable, do not claim a saved failure record.
        persist(check)
        raise error from exc


@router.get('/checks')
def history() -> dict:
    with connect() as db:
        init(db)
        rows=db.execute('SELECT payload FROM dm_quality_checks ORDER BY created_at DESC LIMIT 30').fetchall()
    records=[json.loads(row[0]) for row in rows]
    return {'checks':[{key:record[key] for key in ('id','created_at','dataset_id','version_id','status')}
                     | {'config_revision':record['configuration']['revision'],
                        'blocking':record.get('current',{}).get('blocking'), 'error':record.get('error')}
                     for record in records]}


@router.get('/checks/{check_id}')
def get_check(check_id: UUID) -> dict:
    with connect() as db:
        init(db)
        row=db.execute('SELECT payload FROM dm_quality_checks WHERE id=?',(str(check_id),)).fetchone()
    if not row: raise ServiceError('not_found','质量检查记录不存在。',404)
    return json.loads(row[0])


@router.get('/compare')
def compare(previous: UUID, current: UUID) -> dict:
    before,after=get_check(previous),get_check(current)
    if any(record['status']!='completed' for record in (before,after)):
        raise ServiceError('invalid_comparison','只能比较已完成的检查。',409)
    if (before['dataset_id'],before['version_id'])!=(after['dataset_id'],after['version_id']):
        raise ServiceError('invalid_comparison','不能跨数据集或登记版本比较。',409)
    by_code={rule['rule']:rule for rule in before['current']['rules']}
    return {'previous':str(previous),'current':str(current),
            'same_config':before['configuration']['revision']==after['configuration']['revision'],
            'rules':[{'rule':rule['rule'],'previous':by_code[rule['rule']]['count'],
                      'current':rule['count'],'delta':rule['count']-by_code[rule['rule']]['count']}
                     for rule in after['current']['rules']],
            'note':'比较原始违规数量；登记版本不证明逐行数据相同，阈值变化不能当作质量改善。'}
