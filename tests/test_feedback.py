from datetime import datetime, timezone
import json
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from backend.app.agent import store
from backend.app.config import settings
from backend.app.main import app


@pytest.fixture
def record(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'history.sqlite3')
    analysis_id,session_id=uuid4(),uuid4()
    payload={'id':str(analysis_id),'session_id':str(session_id),'created_at':datetime.now(timezone.utc).isoformat(),
             'question':'历史分析','answer':'来自工具的结论','mode':'structured','intent':{'metric_code':'sales_amount'},
             'warnings':[],'trace':[{'sql':'SELECT 1'}]}
    # Pre-N2 schema and payload: verify additive migration works on existing files.
    with sqlite3.connect(settings.analysis_store) as connection:
        connection.execute('CREATE TABLE analyses (id TEXT PRIMARY KEY,session_id TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL)')
        connection.execute('INSERT INTO analyses VALUES (?,?,?,?)',
            (str(analysis_id),str(session_id),payload['created_at'],json.dumps(payload)))
    return TestClient(app),analysis_id,session_id,payload


def test_feedback_create_replay_update_and_reopen(record):
    client,analysis_id,session_id,payload=record
    endpoint=f'/api/analyses/{analysis_id}/feedback'
    assert client.get(endpoint).json()=={'feedback':None}
    before=store.latest_intent(session_id)
    submitted={'rating':'helpful','reason':'  数字可对账  ','correction':'增加日均比较'}
    response=client.put(endpoint,json=submitted)
    assert response.status_code==200
    first=response.json()['feedback']
    assert first['revision']==1 and first['reason']=='数字可对账'
    assert client.put(endpoint,json=submitted).json()['feedback']==first
    edited=client.put(endpoint,json={'rating':'not_helpful','reason':'希望比较日均','correction':'保留总额，同时展示日均'}).json()['feedback']
    assert edited['revision']==2 and edited['created_at']==first['created_at']
    assert client.get(endpoint).json()['feedback']==edited
    saved=client.get(f'/api/analyses/{analysis_id}').json()
    assert saved['feedback']==edited
    assert {k:saved[k] for k in payload}==payload
    assert store.latest_intent(session_id)==before
    # Stored original analysis snapshot is not mutated by feedback or dynamic export.
    with sqlite3.connect(settings.analysis_store) as connection:
        assert json.loads(connection.execute('SELECT payload FROM analyses').fetchone()[0])==payload
        assert connection.execute('SELECT COUNT(*) FROM analysis_feedback').fetchone()[0]==1
    assert client.get(f'/api/analyses/{analysis_id}/export/json').json()['feedback']==edited


@pytest.mark.parametrize('body',[
    {'rating':'maybe'}, {'reason':'missing rating'},
    {'rating':'helpful','reason':'x'*1001}, {'rating':'helpful','correction':'x'*2001},
    {'rating':'helpful','sql':'DROP TABLE analyses'}, {'rating':'helpful','reason':None},
])
def test_feedback_rejects_bad_inputs_without_writing(record,body):
    client,analysis_id,_,_=record
    assert client.put(f'/api/analyses/{analysis_id}/feedback',json=body).status_code==422
    assert store.feedback(analysis_id) is None


def test_unknown_and_invalid_analysis_ids(record):
    client,*_=record
    path=f'/api/analyses/{uuid4()}/feedback'
    assert client.get(path).status_code==404
    assert client.put(path,json={'rating':'helpful'}).status_code==404
    assert client.put('/api/analyses/not-a-uuid/feedback',json={'rating':'helpful'}).status_code==422


def test_feedback_is_isolated_between_analyses_and_keeps_untrusted_text(record):
    client,analysis_id,_,payload=record
    second=uuid4()
    store.save(payload|{'id':str(second)})
    text='<script>alert(1)</script>'
    assert client.put(f'/api/analyses/{analysis_id}/feedback',json={'rating':'not_helpful','reason':text}).json()['feedback']['reason']==text
    assert client.get(f'/api/analyses/{second}').json()['feedback'] is None


def test_storage_failure_has_safe_error_and_does_not_claim_saved(record,monkeypatch):
    client,analysis_id,_,_=record
    def fail(*args):
        raise sqlite3.OperationalError('private local path')
    monkeypatch.setattr(store,'save_feedback',fail)
    response=client.put(f'/api/analyses/{analysis_id}/feedback',json={'rating':'helpful'})
    assert response.status_code==503
    assert 'private local path' not in response.text
    assert response.json()['error']['code']=='storage_unavailable'
