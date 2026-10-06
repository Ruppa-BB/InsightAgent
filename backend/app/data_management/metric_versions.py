"""Immutable metric definitions. SQL contracts are code-owned, descriptions are governed."""
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import json
import sqlite3
from collections.abc import Callable, Iterable
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from backend.app.agent.store import connect
from backend.app.errors import ServiceError
from backend.app.semantic.metrics import METRICS

router = APIRouter(prefix='/api/data/metrics', tags=['指标口径'])
PINNED = ContextVar('metric_definitions', default=None)

class Draft(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    enabled: bool = True

class Publish(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)

def initialize(db: sqlite3.Connection) -> None:
    db.execute('CREATE TABLE IF NOT EXISTS dm_schema_versions (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO dm_schema_versions VALUES (4,?)',(datetime.now(timezone.utc).isoformat(),))
    db.execute('CREATE TABLE IF NOT EXISTS dm_metric_heads (code TEXT PRIMARY KEY, revision INTEGER NOT NULL, version INTEGER NOT NULL, draft TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS dm_metric_versions (code TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(code,version))')
    for code, template in METRICS.items():
        definition = deepcopy(template)
        definition.setdefault('default_filter', "fact_sales_order.order_status = 'completed'")
        definition.update(metric_code=code, version=1, enabled=True, formula_key=code)
        db.execute('INSERT OR IGNORE INTO dm_metric_heads VALUES (?,1,1,NULL)', (code,))
        db.execute('INSERT OR IGNORE INTO dm_metric_versions VALUES (?,1,?,?)', (code,datetime.now(timezone.utc).isoformat(),json.dumps(definition,ensure_ascii=False)))

def read_head(db: sqlite3.Connection, code: str) -> dict:
    row=db.execute('SELECT revision,version,draft FROM dm_metric_heads WHERE code=?',(code,)).fetchone()
    if not row: raise ServiceError('unknown_metric','指标不存在。',404)
    revision,version,draft=row
    raw=db.execute('SELECT payload FROM dm_metric_versions WHERE code=? AND version=?',(code,version)).fetchone()[0]
    return {'code':code,'revision':revision,'published':json.loads(raw),'draft':json.loads(draft) if draft else None}

@router.get('')
def listing() -> dict:
    with connect() as db:
        initialize(db)
        return {'metrics':[read_head(db,code) for code in METRICS]}

@router.get('/{code}/versions')
def history(code: str) -> dict:
    with connect() as db:
        initialize(db); read_head(db,code)
        rows=db.execute('SELECT created_at,payload FROM dm_metric_versions WHERE code=? ORDER BY version DESC',(code,)).fetchall()
    return {'versions':[{'created_at':date,'definition':json.loads(raw)} for date,raw in rows]}

@router.put('/{code}/draft')
def save_draft(code: str, request: Draft) -> dict:
    with connect() as db:
        initialize(db); db.commit(); db.execute('BEGIN IMMEDIATE')
        current=read_head(db,code)
        if current['revision']!=request.expected_revision: raise ServiceError('metric_conflict','口径已变化，请刷新后再编辑。',409)
        body=request.model_dump(exclude={'expected_revision'})
        if current['draft']!=body:
            db.execute('UPDATE dm_metric_heads SET revision=revision+1,draft=? WHERE code=?',(json.dumps(body,ensure_ascii=False),code))
        return read_head(db,code)

@router.post('/{code}/publish')
def publish(code: str, request: Publish) -> dict:
    with connect() as db:
        initialize(db); db.commit(); db.execute('BEGIN IMMEDIATE')
        current=read_head(db,code)
        if current['revision']!=request.expected_revision: raise ServiceError('metric_conflict','草稿已变化，请刷新后再发布。',409)
        if not current['draft']: raise ServiceError('no_metric_draft','请先保存有效草稿。',409)
        definition=current['published'] | current['draft']
        definition['version']+=1
        db.execute('INSERT INTO dm_metric_versions VALUES (?,?,?,?)',(code,definition['version'],datetime.now(timezone.utc).isoformat(),json.dumps(definition,ensure_ascii=False)))
        db.execute('UPDATE dm_metric_heads SET revision=revision+1,version=?,draft=NULL WHERE code=?',(definition['version'],code))
        from backend.app.data_management.lineage import synchronize
        synchronize(db)
        return read_head(db,code)

def definitions() -> dict:
    pinned=PINNED.get()
    if pinned is not None: return deepcopy(pinned)
    with connect() as db:
        initialize(db)
        return {code:read_head(db,code)['published'] for code in METRICS}

def pinned_execution(function: Callable) -> Callable:
    @wraps(function)
    def wrapped(*args,**kwargs):
        token=PINNED.set(definitions())
        try: return function(*args,**kwargs)
        finally: PINNED.reset(token)
    return wrapped

def evidence(codes: Iterable[str]) -> dict:
    items=definitions()
    for code in codes:
        if code not in items: raise ServiceError('unknown_metric','指标不在计算白名单中。',422)
        if not items[code]['enabled']: raise ServiceError('metric_disabled',f'指标 {code} 已停用，请发布启用版本后再计算。',409)
    return {code:items[code] for code in codes}
