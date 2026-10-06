"""Managed local retail imports: immutable input, transactional isolated schema."""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.app.agent.store import connect
from backend.app.config import ROOT, settings
from backend.app.data_management.catalog import initialize, DATASET_ID
from backend.app.errors import ServiceError
from backend.app.serialization import json_ready

router=APIRouter(prefix='/api/data/imports',tags=['导入批次'])
RAW=ROOT/'data/raw'
WORK=ROOT/'data/generated/managed_imports'
MAX_BYTES=512*1024*1024
MAX_ROWS=1500000


def now(): return datetime.now(timezone.utc).isoformat()


def initialize_batches(db):
    initialize(db)
    db.execute('''CREATE TABLE IF NOT EXISTS dm_import_batches (
        id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        status TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS dm_dataset_versions (
        id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE REFERENCES dm_import_batches(id),
        schema_name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('INSERT OR IGNORE INTO dm_schema_versions VALUES (3,?)',(now(),))


def path_for(relative: str) -> Path:
    candidate=(RAW/relative).resolve()
    if not candidate.is_relative_to(RAW.resolve()) or not candidate.is_file() or candidate.suffix.lower() not in ('.xlsx','.csv'):
        raise ServiceError('invalid_source','只允许data/raw内的已存在Excel或约定结构CSV文件。',422)
    if candidate.stat().st_size>MAX_BYTES:
        raise ServiceError('source_too_large','源文件不能超过512 MiB。',422)
    return candidate


def fingerprint(path: Path) -> str:
    digest=sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''): digest.update(block)
    return digest.hexdigest()


def get_batch(ident: UUID | str) -> dict:
    with connect() as db:
        initialize_batches(db)
        row=db.execute('SELECT payload FROM dm_import_batches WHERE id=?',(str(ident),)).fetchone()
    if not row: raise ServiceError('not_found','导入批次不存在。',404)
    return json.loads(row[0])


def save_batch(record: dict, db=None):
    values=(record['id'],record['fingerprint'],record['created_at'],record['status'],json.dumps(json_ready(record),ensure_ascii=False))
    if db is None:
        with connect() as connection:
            initialize_batches(connection)
            save_batch(record,connection)
        return
    db.execute('''INSERT INTO dm_import_batches VALUES (?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload''',values)


class Register(BaseModel):
    source_file: str = Field(min_length=1,max_length=300)


@router.get('/sources')
def sources():
    return {'files':[{'name':str(path.relative_to(RAW)),'size_bytes':path.stat().st_size}
        for path in sorted(RAW.rglob('*')) if path.is_file() and path.suffix.lower() in ('.csv','.xlsx')
        and path.resolve().is_relative_to(RAW.resolve())],
        'limits':{'max_bytes':MAX_BYTES,'max_raw_rows':MAX_ROWS},
        'note':'只读取本地约定零售结构；CSV可使用原始Excel列名。没有上传/任意字段映射。'}


@router.post('')
def register(request: Register):
    source=path_for(request.source_file)
    digest=fingerprint(source)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE');initialize_batches(db)
        existing=db.execute('SELECT payload FROM dm_import_batches WHERE fingerprint=?',(digest,)).fetchone()
        if existing:return {'batch':json.loads(existing[0]),'duplicate':True}
        ident=str(uuid4());folder=WORK/ident;folder.mkdir(parents=True,exist_ok=True)
        copied=folder/('source'+source.suffix.lower())
        shutil.copyfile(source,copied)
        if fingerprint(copied)!=digest:
            copied.unlink(missing_ok=True)
            raise ServiceError('source_changed','源文件登记时发生变化，请重新登记。',409)
        record={'id':ident,'fingerprint':digest,'created_at':now(),'status':'registered',
                'source_file':str(source.relative_to(RAW.resolve())), 'source_size':copied.stat().st_size,
                'cleaning_version':'retail-clean-v2', 'schema_name':'ia_import_'+UUID(ident).hex,'attempts':0,'events':[{'at':now(),'message':'源文件副本与SHA256已登记'}]}
        save_batch(record,db)
        from backend.app.data_management.lineage import synchronize
        synchronize(db)
    return {'batch':record,'duplicate':False}


@router.get('')
def batches():
    with connect() as db:
        initialize_batches(db)
        records=[json.loads(r[0]) for r in db.execute('SELECT payload FROM dm_import_batches ORDER BY created_at DESC LIMIT 30')]
    records=[refresh_interrupted(r['id']) if r['status'] in ('queued','running') else r for r in records]
    return {'batches':records,'historical_baseline':{'version_id':'baseline-v1','status':'legacy_summary_only',
        'note':'原有导入统计不是受管理的新批次；未核实原始文件逐行关联，不伪造批次来源。'}}



def refresh_interrupted(ident: str) -> dict:
    with connect() as db:
        db.execute('BEGIN IMMEDIATE');initialize_batches(db)
        row=db.execute('SELECT payload FROM dm_import_batches WHERE id=?',(ident,)).fetchone()
        if not row:raise ServiceError('not_found','导入批次不存在。',404)
        record=json.loads(row[0])
        interrupted=False
        if record['status']=='running' and record.get('worker_pid'):
            try: os.kill(record['worker_pid'],0)
            except ProcessLookupError: interrupted=True
            except PermissionError: pass
        if record['status']=='queued' and (datetime.now(timezone.utc)-datetime.fromisoformat(record.get('queued_at',record['created_at']))).total_seconds()>300:
            interrupted=True
        if interrupted:
            record.update(status='recovery_required',error={'code':'worker_interrupted','message':'导入进程已中断或队列未启动，请核查隔离schema与登记状态后恢复；不自动重跑。'})
            record['events'].append({'at':now(),'message':'刷新检测到进程中断，需要人工核查'})
            save_batch(record,db)
        return record

@router.get('/{ident}')
def batch(ident: UUID): return refresh_interrupted(str(ident))


@router.post('/{ident}/run')
def start(ident: UUID):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE');initialize_batches(db)
        row=db.execute('SELECT payload FROM dm_import_batches WHERE id=?',(str(ident),)).fetchone()
        if not row:raise ServiceError('not_found','批次不存在。',404)
        record=json.loads(row[0])
        if record['status']=='published': return record
        if record['status']=='recovery_required': raise ServiceError('recovery_required','隔离schema已存在但未完成登记，需人工核查；不能自动重复写入。',409)
        if record['status'] in ('queued','running'): raise ServiceError('batch_busy','批次正在运行，不重复启动。',409)
        record.update(status='queued',error=None,queued_at=now())
        save_batch(record,db)
    try:
        # Separate controlled process: the HTTP request does not await Excel/1M rows.
        env=os.environ.copy();env['ANALYSIS_STORE']=str(settings.analysis_store)
        subprocess.Popen([sys.executable,'-m','backend.app.data_management.batches','--run',str(ident)],
                         cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    except OSError as exc:
        record.update(status='failed',error={'code':'worker_start_failed','message':'本地导入进程无法启动。'})
        save_batch(record)
        raise ServiceError('worker_start_failed','本地导入进程无法启动，请检查环境。',503) from exc
    return record


def progress(record: dict,message: str):
    record['events'].append({'at':now(),'message':message})
    save_batch(record)


def worker(ident: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE');initialize_batches(db)
        record=json.loads(db.execute('SELECT payload FROM dm_import_batches WHERE id=?',(ident,)).fetchone()[0])
        if record['status'] not in ('queued','registered','failed'):return
        record.update(status='running',attempts=record['attempts']+1,error=None,worker_pid=os.getpid())
        save_batch(record,db)
    committed=False
    try:
        from backend.app import db as business
        if business.engine is None: raise ValueError('No database')
        source=next((WORK/ident).glob('source.*'))
        if fingerprint(source)!=record['fingerprint']:raise ValueError('Source fingerprint mismatch')
        spec=importlib.util.spec_from_file_location('retail_cleaner',ROOT/'data/scripts/import_online_retail.py')
        cleaner=importlib.util.module_from_spec(spec);spec.loader.exec_module(cleaner)
        import pandas as pd
        progress(record,'读取固定结构源文件；上限150万原始行')
        raw=cleaner.load_source(source) if source.suffix=='.xlsx' else pd.read_csv(source)
        if len(raw)>MAX_ROWS: raise ValueError('Raw row budget exceeded')
        cleaned,stats=cleaner.clean_source(raw)
        if not stats['clean_rows']:raise ValueError('No clean rows')
        record['statistics']=stats
        output=WORK/ident/'cleaned';cleaner.export(cleaned,stats,output)
        progress(record,'清洗产物生成；取消数包含于无效数，不相加')
        schema=record['schema_name']
        if not re.fullmatch(r'ia_import_[0-9a-f]{32}',schema):raise ValueError('Invalid managed schema')
        schema_sql=(ROOT/'database/schema.sql').read_text()
        schema_sql=re.sub(r'^\s*(BEGIN|COMMIT);\s*$', '',schema_sql,flags=re.M)
        import_sql=(ROOT/'database/import_online_retail.sql').read_text()
        staging=import_sql[import_sql.index('CREATE TEMP TABLE'):import_sql.index('\n\\copy')]
        insert_sql=import_sql[import_sql.index('INSERT INTO dim_region'):import_sql.index('COMMIT;')]
        progress(record,'开始隔离事务：创建表、COPY、插入与质量/金额核验')
        with business.engine.begin() as connection:
            if connection.execute(text('SELECT 1 FROM pg_namespace WHERE nspname=:name'),{'name':schema}).first():
                record['status']='recovery_required'
                raise ValueError('Managed schema already exists')
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", pg_catalog')
            connection.execute(text("SELECT set_config('statement_timeout','120000',true)"))
            connection.connection.driver_connection.execute(schema_sql)
            connection.connection.driver_connection.execute(staging)
            cursor=connection.connection.driver_connection.cursor()
            for table,file in [('st_region','dim_region'),('st_customer','dim_customer'),('st_product','dim_product'),
                               ('st_order','fact_sales_order'),('st_detail','fact_sales_detail')]:
                with cursor.copy(f'COPY {table} FROM STDIN WITH (FORMAT CSV, HEADER true)') as copy:
                    with (output/(file+'.csv')).open('rb') as handle:
                        for block in iter(lambda:handle.read(1024*1024),b''):copy.write(block)
            connection.connection.driver_connection.execute(insert_sql)
            from backend.app.tools.quality_tool import current_quality
            quality=current_quality(connection)
            summary=quality['summary']
            if quality['blocking'] or summary['details']!=stats['clean_rows'] or summary['orders']!=stats['orders'] or abs(Decimal(str(stats['sales_amount']))-summary['sales_amount'])>Decimal('.01'):
                raise ValueError('Reconciliation or quality failed')
            for table,key in [('dim_customer','customers'),('dim_product','products'),('dim_region','regions')]:
                if connection.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one()!=stats[key]:
                    raise ValueError('Dimension reconciliation failed')
            record['verification']=json_ready(quality)
            # Registry commit occurs after PG commit; orphan schema is never selectable.
        committed=True
        record.update(status='published',completed_at=now(),version_id=ident)
        record['events'].append({'at':now(),'message':'隔离事务成功，质量与金额核验通过；发布可选分析版本'})
        with connect() as db:
            db.execute('BEGIN IMMEDIATE');initialize_batches(db)
            db.execute('INSERT INTO dm_dataset_versions VALUES (?,?,?,?,?)',
                       (ident,ident,schema,now(),json.dumps(json_ready(record),ensure_ascii=False)))
            save_batch(record,db)
            from backend.app.data_management.lineage import synchronize
            synchronize(db)
    except Exception as exc:
        record["diagnostic"]={"exception_type":type(exc).__name__,"sqlstate":getattr(exc,"sqlstate",getattr(getattr(exc,"orig",None),"sqlstate",None)),"location":__import__("traceback").extract_tb(exc.__traceback__)[-1].name}
        # Failed transaction rolls back all created schema/tables. If PG committed but
        # registry failed, leave schema inaccessible; never DROP a possibly committed source.
        record.update(status='recovery_required' if committed or record['status']=='recovery_required' else 'failed',completed_at=now(),error={'code':'import_failed',
            'message':'导入未发布。请检查列结构、日期/订单一致性、数据库建表权限与存储空间。'})
        record['events'].append({'at':now(),'message':'导入失败；不切换现有分析数据，不重复写入public'})
        save_batch(record)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=UUID,required=True)
    worker(str(parser.parse_args().run))
