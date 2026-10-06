"""Version-scoped lineage from recorded ledgers and an explicit SQL dependency manifest.

Declared dependencies mean possible influence, recorded edges mean saved consumption.
Never link a dataset table to a global metric node: that merges unrelated versions.
"""
from datetime import datetime, timezone
import json
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Query
from backend.app.agent.store import connect
from backend.app.errors import ServiceError

router = APIRouter(prefix='/api/data/lineage', tags=['数据血缘'])
TABLES = ('fact_sales_order','fact_sales_detail','dim_customer','dim_product','dim_region')
# BASE_FROM joins all five tables for each base metric. Derived tools use that query.
METRIC_CODES = ('sales_amount','order_count','customer_count','sales_quantity',
                'average_order_value','average_selling_price','sales_mom','sales_yoy')
MANIFEST_VERSION = 'retail-sql-v1'


def initialize(db: sqlite3.Connection) -> None:
    db.execute('CREATE TABLE IF NOT EXISTS dm_schema_versions (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO dm_schema_versions VALUES (5,?)',(datetime.now(timezone.utc).isoformat(),))
    db.execute('CREATE TABLE IF NOT EXISTS dm_lineage_nodes (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS dm_lineage_edges (source TEXT NOT NULL REFERENCES dm_lineage_nodes(id), target TEXT NOT NULL REFERENCES dm_lineage_nodes(id), relation TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(source,target,relation))')
    db.execute('CREATE INDEX IF NOT EXISTS dm_lineage_target ON dm_lineage_edges(target)')


def node(db: sqlite3.Connection, ident: str, kind: str, label: str, **data) -> str:
    payload={'id':ident,'kind':kind,'label':label,**data}
    db.execute('INSERT INTO dm_lineage_nodes VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
               (ident,kind,json.dumps(payload,ensure_ascii=False)))
    return ident


def edge(db: sqlite3.Connection, source: str, target: str, relation: str, evidence: str) -> None:
    db.execute('INSERT OR IGNORE INTO dm_lineage_edges VALUES (?,?,?,?)',(source,target,relation,evidence))


def payloads(db: sqlite3.Connection, table: str) -> list[dict]:
    # table is only supplied by this module's static callers.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is None:
        return []
    return [json.loads(row[0]) for row in db.execute(f'SELECT payload FROM {table}')]


def dataset(db: sqlite3.Connection, ident: str, schema: str | None = None, known: bool = True) -> str:
    return node(db,'dataset:'+ident,'dataset',('零售基线' if ident=='baseline-v1' else '数据版本 '+ident[:8]),
                version_id=ident,schema=schema,known=known,href='/data' if ident=='baseline-v1' else '/data/imports?batch='+quote(ident),
                note='数据版本已登记；基线原始导入来源未登记。' if ident=='baseline-v1' else '按保存的版本登记，不随当前选择变化。')


def table_node(db: sqlite3.Connection, version: str, table: str) -> str:
    return node(db,'table:'+version+':'+table,'table',table+' · '+version[:8],
                version_id=version,table=table,href='/data?table='+quote(table) if version=='baseline-v1' else '/data/imports?batch='+quote(version),
                note='版本内的表实例；资产目录当前扫描仅对应 public 基线。')


def metric_node(db: sqlite3.Connection, definition: dict) -> str:
    code,version=definition['metric_code'],definition['version']
    ident=node(db,f'metric:{code}:v{version}','metric',f"{definition['name']} v{version}",
               metric_code=code,metric_version=version,definition=definition,
               href=f'/data/metrics?code={quote(code)}#{quote(code)}-v{version}')
    if code in METRIC_CODES and definition.get('formula_key')==code:
        template=node(db,'template:'+MANIFEST_VERSION+':'+code,'template',code+' · 固定计算模板',
                      manifest_version=MANIFEST_VERSION,note='代码声明的表级依赖；不是自动 SQL 解析或字段级血缘。')
        edge(db,template,ident,'defines','declared')
    return ident


def binding(db: sqlite3.Connection, version: str, definition: dict) -> str:
    metric=metric_node(db,definition)
    code,mv=definition['metric_code'],definition['version']
    ident=node(db,f'binding:{version}:{code}:v{mv}','binding',f"{definition['name']} v{mv} × {version[:8]}",
               version_id=version,metric_code=code,metric_version=mv,
               note='数据版本与口径版本组合；接入分析/报告的 recorded 边才表示实际使用。')
    edge(db,metric,ident,'uses_definition','registered')
    if code in METRIC_CODES and definition.get('formula_key')==code:
        for table in TABLES:
            table_id=table_node(db,version,table)
            edge(db,'dataset:'+version,table_id,'contains','registered')
            edge(db,table_id,ident,'query_dependency','declared')
    else:
        unknown=node(db,'unknown:dependency:'+ident,'unknown','未知计算依赖',known=False)
        edge(db,unknown,ident,'dependency_unknown','unknown')
    return ident


def output(db: sqlite3.Connection, record: dict, kind: str) -> None:
    ident=str(record['id']); snapshot=record.get('snapshot',{}) if kind=='report' else record
    source=snapshot.get('dataset_source') or {}
    version=source.get('version_id')
    target=node(db,kind+':'+ident,kind,
                record.get('title') or record.get('question') or ident,
                record_id=ident,created_at=record.get('created_at'),version_id=version,
                href='/api/reports/'+ident+'/export/json' if kind=='report' else '/api/analyses/'+ident+'/export/json',
                metric_evidence_registered=bool(snapshot.get('metric_definitions')))
    if version:
        exists=db.execute('SELECT 1 FROM dm_lineage_nodes WHERE id=?',('dataset:'+version,)).fetchone()
        if not exists:
            dataset(db,version,source.get('schema'),known=False)
            unknown=node(db,'unknown:dataset:'+version,'unknown','数据版本未找到发布登记',known=False)
            edge(db,unknown,'dataset:'+version,'registry_unknown','unknown')
        edge(db,'dataset:'+version,target,'saved_source','recorded')
    else:
        unknown=node(db,'unknown:source:'+kind+':'+ident,'unknown','历史来源未登记',known=False)
        edge(db,unknown,target,'source_unknown','unknown')
    definitions=snapshot.get('metric_definitions') or {}
    if definitions and version:
        for definition in definitions.values():
            if all(key in definition for key in ('metric_code','version','name')):
                edge(db,binding(db,version,definition),target,'consumed','recorded')
    else:
        unknown=node(db,'unknown:metric:'+kind+':'+ident,'unknown','历史口径未登记' if not definitions else '缺少数据版本，不能建立指标绑定',known=False)
        edge(db,unknown,target,'metric_unknown','unknown')


def synchronize(db: sqlite3.Connection) -> None:
    """Idempotent ledger backfill, also called in new publication/save transactions."""
    initialize(db)
    baseline=dataset(db,'baseline-v1','public')
    unknown=node(db,'unknown:baseline-source','unknown','基线原始文件/导入批次未登记',known=False,
                 note='仅有历史清洗摘要，不能伪造文件指纹或导入时间。')
    edge(db,unknown,baseline,'source_unknown','unknown')
    versions={'baseline-v1'}
    for record in payloads(db,'dm_import_batches'):
        ident=record['id'];digest=record['fingerprint']
        source=node(db,'source:'+digest,'source',record['source_file'],sha256=digest,
                    source_file=record['source_file'],href='/data/imports?batch='+quote(ident))
        batch=node(db,'batch:'+ident,'batch','导入批次 '+ident[:8],record_id=ident,
                   status=record['status'],href='/data/imports?batch='+quote(ident),cleaning_version=record.get('cleaning_version'),
                   note='清洗程序版本未登记' if not record.get('cleaning_version') else '文件副本SHA-256已登记')
        edge(db,source,batch,'imported_by','recorded')
    # Only the published ledger creates dataset and table lineage. Failed batches do not.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dm_dataset_versions'").fetchone():
        for ident,batch_id,schema in db.execute('SELECT id,batch_id,schema_name FROM dm_dataset_versions'):
            versions.add(ident);target=dataset(db,ident,schema)
            if db.execute('SELECT 1 FROM dm_lineage_nodes WHERE id=?',('batch:'+batch_id,)).fetchone():
                edge(db,'batch:'+batch_id,target,'published','recorded')
            else:
                missing=node(db,'unknown:batch:'+batch_id,'unknown','导入批次记录缺失',known=False)
                edge(db,missing,target,'batch_unknown','unknown')
    for version in versions:
        for table in TABLES:
            edge(db,'dataset:'+version,table_node(db,version,table),'contains','registered')
    for definition in payloads(db,'dm_metric_versions'):
        metric_node(db,definition)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dm_metric_heads'").fetchone():
        for code,mv in db.execute('SELECT code,version FROM dm_metric_heads'):
            row=db.execute('SELECT payload FROM dm_metric_versions WHERE code=? AND version=?',(code,mv)).fetchone()
            if row:
                definition=json.loads(row[0])
                if definition.get('enabled',True):
                    for version in versions:binding(db,version,definition)
    for table,kind in [('analyses','analysis'),('business_reports','report')]:
        for ident,raw in db.execute(f'SELECT id,payload FROM {table}'):
            output(db,json.loads(raw) | {'id':ident},kind)


@router.get('')
def graph() -> dict:
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        synchronize(db)
        nodes=[json.loads(r[0]) for r in db.execute('SELECT payload FROM dm_lineage_nodes ORDER BY id')]
        edges=[dict(zip(('source','target','relation','evidence'),r)) for r in db.execute('SELECT source,target,relation,evidence FROM dm_lineage_edges ORDER BY source,target,relation')]
    return {'manifest_version':MANIFEST_VERSION,'nodes':nodes,'edges':edges,
            'note':'表级血缘。declared=固定计算依赖/潜在影响；recorded=保存的消费或发布记录；unknown=缺少登记。'}


@router.get('/trace')
def trace(node_id: str = Query(min_length=1,max_length=500), direction: str = Query(pattern='^(upstream|downstream)$')) -> dict:
    data=graph();nodes={item['id']:item for item in data['nodes']}
    if node_id not in nodes:raise ServiceError('lineage_not_found','血缘节点不存在。',404)
    visited={node_id};frontier=[node_id];edges=[]
    while frontier:
        current=frontier.pop()
        for item in data['edges']:
            match=item['target'] if direction=='upstream' else item['source']
            if match!=current:continue
            edges.append(item)
            other=item['source'] if direction=='upstream' else item['target']
            if other not in visited:visited.add(other);frontier.append(other)
    return {'root':nodes[node_id],'direction':direction,'nodes':[nodes[key] for key in sorted(visited)],
            'edges':edges,'note':data['note'],'unknown_count':sum(nodes[key]['kind']=='unknown' for key in visited)}
