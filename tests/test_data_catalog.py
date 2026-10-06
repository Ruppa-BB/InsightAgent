import json
import sqlite3
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.data_management import catalog
from backend.app.errors import ServiceError
from backend.app.main import app


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'analysis_store', tmp_path/'old.sqlite3')
    # Existing history survives the additive metadata migration.
    with sqlite3.connect(settings.analysis_store) as db:
        db.execute('CREATE TABLE analyses (id TEXT PRIMARY KEY,session_id TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL)')
        db.execute('INSERT INTO analyses VALUES (?,?,?,?)', ('old','s','now','{}'))
    class Result:
        def __init__(self, value): self.value=value
        def mappings(self): return self.value
        def scalar_one(self): return self.value
    class DB:
        def execute(self, sql, params=None):
            statement=str(sql)
            if 'information_schema' in statement:
                return Result([{'column_name':'id','data_type':'bigint','udt_name':'int8','is_nullable':'NO',
                                'column_default':None,'is_generated':'NEVER','generation_expression':None},
                               {'column_name':'standard_cost','data_type':'numeric','udt_name':'numeric','is_nullable':'NO',
                                'column_default':None,'is_generated':'NEVER','generation_expression':None}])
            if 'pg_constraint' in statement: return Result([{'name':'pk','type':'p','definition':'PRIMARY KEY (id)'}])
            assert any(f'public."{name}"' in statement for name in catalog.TABLES)
            return Result(7)
    @contextmanager
    def read(): yield DB()
    monkeypatch.setattr(catalog, 'read_connection', read)
    return TestClient(app)


def test_scan_preserves_descriptions_and_old_history(setup):
    client=setup
    assert client.get('/api/data/assets').json()['assets']==[]
    assert client.post('/api/data/scan').json()['asset_count']==5
    endpoint='/api/data/assets/fact_sales_detail/fields/id/description'
    saved=client.put(endpoint,json={'description':' 订单行编号 ','expected_revision':0}).json()
    assert saved['revision']==1 and saved['description']=='订单行编号'
    assert client.put(endpoint,json={'description':'订单行编号','expected_revision':1}).json()==saved
    assert client.put(endpoint,json={'description':'覆盖','expected_revision':0}).status_code==409
    for _ in range(2): assert client.post('/api/data/scan').status_code==200
    assets=client.get('/api/data/assets').json()['assets']
    assert len(assets)==5
    detail=client.get('/api/data/assets/fact_sales_detail').json()
    assert detail['columns'][0]['human']==saved
    assert detail['columns'][1]['availability']=='unavailable'
    assert detail['metrics']
    with sqlite3.connect(settings.analysis_store) as db:
        assert db.execute('SELECT id FROM analyses').fetchall()==[('old',)]
        assert db.execute('SELECT count(*) FROM dm_schema_versions').fetchone()[0]==1


def test_allowlist_search_and_validation(setup):
    client=setup;client.post('/api/data/scan')
    assert client.get('/api/data/assets/pg_authid').status_code==404
    assert client.put('/api/data/assets/pg_authid/description',json={'description':'x','expected_revision':0}).status_code==404
    assert client.put('/api/data/assets/dim_customer/fields/secret/description',json={'description':'x','expected_revision':0}).status_code==404
    assert client.put('/api/data/assets/dim_customer/description',json={'description':'x'*2001,'expected_revision':0}).status_code==422
    assert len(client.get('/api/data/assets',params={'search':'dim_customer'}).json()['assets'])==1
    assert len(client.get('/api/data/assets',params={'search':"' OR 1=1 --"}).json()['assets'])==0
    assert client.get('/data').status_code==200


def test_failed_scan_keeps_previous_snapshot(setup,monkeypatch):
    setup.post('/api/data/scan')
    before=setup.get('/api/data/assets').json()
    @contextmanager
    def fail():
        raise ServiceError('query_timeout','查询超时',504)
        yield
    monkeypatch.setattr(catalog,'read_connection',fail)
    assert setup.post('/api/data/scan').status_code==504
    assert setup.get('/api/data/assets').json()==before
