from contextlib import contextmanager
from uuid import uuid4
import json

from fastapi.testclient import TestClient
import pytest

from backend.app.config import settings
from backend.app.data_management import quality
from backend.app.errors import ServiceError
from backend.app.main import app


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'quality.sqlite3')
    @contextmanager
    def connection(): yield object()
    monkeypatch.setattr(quality,'read_connection',connection)
    monkeypatch.setattr(quality,'current_quality',lambda _: {'summary':{'details':100,'orders':10},
        'rules':[{'rule':code,'count':3 if code=='null_required' else 0,
                  'status':'failed' if code=='null_required' else 'passed'} for code in quality.RULES],
        'blocking':True,'data_version':'fingerprint','version_note':'summary only'})
    monkeypatch.setattr(quality,'samples',lambda db,code,limit:[{'record_id':i} for i in range(min(3,limit))])
    return TestClient(app)


def test_config_versions_and_mandatory_gate(client):
    cfg=client.get('/api/data/quality/config').json()
    body={k:cfg[k] for k in ('sample_limit','rules')};body['expected_revision']=1
    body['sample_limit']=2;body['rules']['null_required']['notification_threshold']=10
    assert client.put('/api/data/quality/config',json=body).json()['revision']==2
    assert client.put('/api/data/quality/config',json=body).status_code==409
    body['expected_revision']=2
    assert client.put('/api/data/quality/config',json=body).json()['revision']==2
    first=client.post('/api/data/quality/checks').json()
    rule=first['current']['rules'][0]
    assert rule['count']==3 and rule['status']=='failed' and not rule['attention']
    assert first['current']['blocking'] and len(rule['samples'])==2
    body['sample_limit']=5
    client.put('/api/data/quality/config',json=body)
    second=client.post('/api/data/quality/checks').json()
    reopened=client.get('/api/data/quality/checks/'+first['id']).json()
    assert reopened==first and reopened['configuration']['revision']==2
    compare=client.get('/api/data/quality/compare',params={'previous':first['id'],'current':second['id']}).json()
    assert not compare['same_config'] and all(r['delta']==0 for r in compare['rules'])
    assert len(client.get('/api/data/quality/checks').json()['checks'])==2


def test_config_rejects_unknown_missing_rules_and_limits(client):
    cfg=client.get('/api/data/quality/config').json()
    body={k:cfg[k] for k in ('sample_limit','rules')};body['expected_revision']=1
    body['rules']['arbitrary_sql']={'notification_threshold':0}
    assert client.put('/api/data/quality/config',json=body).status_code==422
    del body['rules']['arbitrary_sql'];body['sample_limit']=21
    assert client.put('/api/data/quality/config',json=body).status_code==422
    body['sample_limit']=5;body['rules']['null_required']['notification_threshold']=-1
    assert client.put('/api/data/quality/config',json=body).status_code==422
    assert client.get('/api/data/quality/checks/'+str(uuid4())).status_code==404


def test_failed_check_visible_and_comparison_guard(client,monkeypatch):
    first=client.post('/api/data/quality/checks').json()
    def fail(_): raise ServiceError('query_timeout','查询超时',504)
    monkeypatch.setattr(quality,'current_quality',fail)
    assert client.post('/api/data/quality/checks').status_code==504
    record=client.get('/api/data/quality/checks').json()['checks'][0]
    assert record['status']=='failed' and record['error']['code']=='query_timeout'
    assert client.get('/api/data/quality/compare',params={'previous':first['id'],'current':record['id']}).status_code==409
    other={**first,'id':str(uuid4()),'version_id':'another'}
    quality.persist(other)
    assert client.get('/api/data/quality/compare',params={'previous':first['id'],'current':other['id']}).status_code==409


def test_diagnostic_templates_bounded_and_allowlisted():
    class Result:
        def mappings(self): return [{'record_id':1}]
    class DB:
        def execute(self,sql,params):
            assert ':limit' in str(sql) and params=={'limit':5}
            assert 'SELECT * FROM fact_sales' not in str(sql)
            return Result()
    for code in quality.RULES:
        assert quality.samples(DB(),code,5)==[{'record_id':1}]
