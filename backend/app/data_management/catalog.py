"""D1–D4 catalog: technical snapshots and human descriptions are independent."""
from datetime import datetime, timezone
import json

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.app.agent.store import connect
from backend.app.db import read_connection
from backend.app.errors import ServiceError
from backend.app.semantic.metrics import METRICS

DATASET_ID = 'online-retail-ii'
VERSION_ID = 'baseline-v1'
TABLES = {
    'dim_region': '区域与国家维度',
    'dim_customer': '客户维度',
    'dim_product': '商品维度',
    'fact_sales_order': '订单事实，收入分析仅纳入 completed 订单',
    'fact_sales_detail': '订单明细事实，金额 GBP；成本与毛利不可用',
}
FIELD_HINTS = {
    ('dim_region', 'country_name'): '模型默认值，不能当作零售源国家；国家分析实际使用 region_name。',
    ('dim_product', 'unit'): '模型默认单位，不代表零售源真实计量单位；销量分析按 items。',
    ('dim_product', 'standard_price'): '导入时为0的占位字段，不代表真实标准售价。',
    ('dim_product', 'product_line'): '导入统一标记 Retail，不是源数据产品线。',
    ('dim_product', 'product_category'): '源数据未提供分类，导入标记 Unknown。',
    ('dim_customer', 'industry'): '源数据未提供行业，导入标记 Unknown。',
    ('dim_customer', 'customer_level'): '源数据未提供等级，导入标记 Unknown。',
    ('dim_customer', 'acquisition_date'): '导入映射为样本中的首次订单日期，不证明真实获客日期。',
    ('dim_customer', 'customer_name'): '导入使用匿名客户编号，不是真实姓名。',
    ('fact_sales_order', 'confirmed_date'): '源数据无独立确认日期，映射自 InvoiceDate。',
}
UNAVAILABLE = {'standard_cost', 'cost_amount', 'gross_profit', 'gross_margin'}
router = APIRouter(prefix='/api/data', tags=['数据管理'])


class DescriptionUpdate(BaseModel):
    description: str = Field(max_length=2000)
    expected_revision: int = Field(ge=0)


def initialize(connection) -> None:
    connection.execute('''CREATE TABLE IF NOT EXISTS dm_schema_versions (
        version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS dm_datasets (
        id TEXT PRIMARY KEY, version_id TEXT NOT NULL, name TEXT NOT NULL,
        source TEXT NOT NULL, unit TEXT NOT NULL, registered_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS dm_assets (
        id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES dm_datasets(id),
        table_name TEXT NOT NULL, payload TEXT NOT NULL, scanned_at TEXT NOT NULL,
        UNIQUE(dataset_id,table_name))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS dm_descriptions (
        asset_id TEXT NOT NULL REFERENCES dm_assets(id), field_name TEXT NOT NULL,
        description TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(asset_id,field_name))''')
    now = datetime.now(timezone.utc).isoformat()
    connection.execute('INSERT OR IGNORE INTO dm_schema_versions VALUES (1,?)', (now,))
    connection.execute('INSERT OR IGNORE INTO dm_datasets VALUES (?,?,?,?,?,?)',
        (DATASET_ID, VERSION_ID, 'Online Retail II', '已导入的本地 PostgreSQL / public', 'GBP', now))


def asset_id(table_name: str) -> str:
    return f'{DATASET_ID}:{table_name}'


def metric_links(table_name: str) -> list[dict]:
    # Explicit semantic dependencies; not inferred from text or an LLM.
    return [{'code': code, **definition} for code, definition in METRICS.items()
            if table_name in ('fact_sales_order', 'fact_sales_detail') or
            {'dim_customer': 'customer', 'dim_product': 'product', 'dim_region': 'country'}.get(table_name) in definition.get('dimensions', [])]


def scan_registered() -> dict:
    """Only static allowed table names enter SQL; save all snapshots atomically."""
    snapshots = []
    with read_connection() as db:
        for name in TABLES:
            columns = [dict(row) for row in db.execute(text('''
                SELECT column_name, data_type, udt_name, is_nullable,
                       column_default, is_generated, generation_expression
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=:name ORDER BY ordinal_position
            '''), {'name': name}).mappings()]
            if not columns:
                raise ServiceError('asset_missing', f'注册表 {name} 不存在或没有访问权限，未更新目录。', 409)
            constraints = [dict(row) for row in db.execute(text('''
                SELECT c.conname AS name, c.contype AS type, pg_get_constraintdef(c.oid) AS definition
                FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
                JOIN pg_namespace n ON n.oid=t.relnamespace
                WHERE n.nspname='public' AND t.relname=:name AND c.contype IN ('p','f','u')
                ORDER BY c.conname
            '''), {'name': name}).mappings()]
            # name comes solely from the static registry above.
            count = db.execute(text(f'SELECT COUNT(*) FROM public."{name}"')).scalar_one()
            snapshots.append({'table_name': name, 'schema': 'public', 'row_count': count,
                              'columns': columns, 'constraints': constraints})
    now = datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        initialize(connection)
        for snapshot in snapshots:
            connection.execute('''INSERT INTO dm_assets VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,scanned_at=excluded.scanned_at''',
                (asset_id(snapshot['table_name']), DATASET_ID, snapshot['table_name'],
                 json.dumps(snapshot, ensure_ascii=False), now))
    return {'scanned_at': now, 'asset_count': len(snapshots), 'assets': list_assets()['assets']}


def list_assets(search: str = '') -> dict:
    with connect() as connection:
        initialize(connection)
        dataset = connection.execute('SELECT * FROM dm_datasets WHERE id=?', (DATASET_ID,)).fetchone()
        rows = connection.execute('SELECT id,payload,scanned_at FROM dm_assets ORDER BY table_name').fetchall()
        descriptions = {(r[0],r[1]): {'description': r[2], 'revision': r[3], 'updated_at': r[4]}
                        for r in connection.execute('SELECT * FROM dm_descriptions')}
    assets = []
    for ident, payload, scanned_at in rows:
        entry = json.loads(payload)
        entry.update(id=ident, dataset_id=DATASET_ID, version_id=VERSION_ID, scanned_at=scanned_at,
                     suggested_description=TABLES[entry['table_name']],
                     human=descriptions.get((ident,''), {'description': '', 'revision': 0, 'updated_at': None}),
                     metrics=metric_links(entry['table_name']))
        for field in entry['columns']:
            field['human'] = descriptions.get((ident,field['column_name']),
                                              {'description': '', 'revision': 0, 'updated_at': None})
            field['availability'] = 'unavailable' if field['column_name'] in UNAVAILABLE else 'registered'
            field['warning'] = '零售源没有真实成本事实；该占位/派生字段不能用于成本、毛利分析。' if field['column_name'] in UNAVAILABLE else FIELD_HINTS.get((entry['table_name'],field['column_name']))
        if not search or search.casefold() in json.dumps(entry, ensure_ascii=False).casefold():
            assets.append(entry)
    return {'dataset': dict(zip(('id','version_id','name','source','unit','registered_at'),dataset)),
            'assets': assets, 'registry_size': len(TABLES),
            'warnings': ['基线版本是当前数据登记，不代表已核实历史导入批次。',
                         '扫描时间和行数是一次只读快照；baseline-v1不是逐行数据哈希。',
                         '业务说明由人工维护；技术字段及关联来自数据库。']}


def update_description(name: str, field_name: str, request: DescriptionUpdate) -> dict:
    if name not in TABLES:
        raise ServiceError('not_found', '未注册的数据表。', 404)
    ident = asset_id(name)
    with connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        initialize(connection)
        row = connection.execute('SELECT payload FROM dm_assets WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ServiceError('not_found', '请先扫描注册数据表。', 404)
        if field_name and field_name not in {f['column_name'] for f in json.loads(row[0])['columns']}:
            raise ServiceError('not_found', '字段不存在。', 404)
        old = connection.execute('SELECT description,revision,updated_at FROM dm_descriptions WHERE asset_id=? AND field_name=?',
                                 (ident,field_name)).fetchone()
        revision = old[1] if old else 0
        if request.expected_revision != revision:
            raise ServiceError('revision_conflict', '说明已被更新，请刷新后再保存。', 409)
        description = request.description.strip()
        if old and old[0] == description:
            return {'description': old[0], 'revision': revision, 'updated_at': old[2]}
        now = datetime.now(timezone.utc).isoformat()
        connection.execute('''INSERT INTO dm_descriptions VALUES (?,?,?,?,?)
            ON CONFLICT(asset_id,field_name) DO UPDATE SET description=excluded.description,
            revision=excluded.revision,updated_at=excluded.updated_at''',
            (ident,field_name,description,revision+1,now))
    return {'description': description, 'revision': revision+1, 'updated_at': now}


@router.get('/assets')
def assets(search: str = '') -> dict:
    return list_assets(search)


@router.post('/scan')
def scan() -> dict:
    return scan_registered()


@router.get('/assets/{name}')
def detail(name: str) -> dict:
    for item in list_assets()['assets']:
        if item['table_name'] == name:
            return item
    raise ServiceError('not_found', '注册资产尚未扫描或不存在。', 404)


@router.put('/assets/{name}/description')
def table_description(name: str, request: DescriptionUpdate) -> dict:
    return update_description(name, '', request)


@router.put('/assets/{name}/fields/{field_name}/description')
def field_description(name: str, field_name: str, request: DescriptionUpdate) -> dict:
    return update_description(name, field_name, request)
