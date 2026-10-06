from contextlib import contextmanager
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from backend.app.config import settings
from backend.app.data_management import quality, issues
from backend.app.data_management.versions import REQUEST_VERSION
from backend.app.errors import ServiceError
from backend.app.main import app

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'analysis_store', tmp_path/'issues.sqlite3')
    state={'count':2}
    @contextmanager
    def connection():
        yield type('Connection', (), {'info':{'dataset_source':{'version_id':REQUEST_VERSION.get()}}})()
    monkeypatch.setattr(quality, 'read_connection', connection)
    monkeypatch.setattr(quality, 'current_quality', lambda _: {
        'rules':[{'rule':code,'count':state['count'] if code=='null_required' else 0,
                  'status':'failed' if code=='null_required' and state['count'] else 'passed'} for code in quality.RULES],
        'blocking':bool(state['count']), 'summary':{}, 'data_version':'test', 'version_note':'test'})
    monkeypatch.setattr(quality, 'samples', lambda *_:[{'record_id':1}])
    return TestClient(app),state


def one(client):return client.get('/api/data/issues').json()['issues'][0]
def action(client,issue,kind):return client.post('/api/data/issues/'+issue['id']+'/actions',json={'expected_revision':issue['revision'],'action':kind,'note':'处理记录'})
def recheck(client,issue):return client.post('/api/data/issues/'+issue['id']+'/recheck',json={'expected_revision':issue['revision']})


def test_dedup_idempotent_and_immutable(env):
    client,_=env
    check=client.post('/api/data/quality/checks').json()
    quality.persist(check)
    assert one(client)['observations']==1
    client.post('/api/data/quality/checks')
    issue=one(client)
    assert issue['observations']==2 and len(client.get('/api/data/issues').json()['issues'])==1
    check['current']['blocking']=False
    with pytest.raises(ServiceError):quality.persist(check)
    assert one(client)['observations']==2


def test_failure_success_close_and_recurrence(env):
    client,state=env
    client.post('/api/data/quality/checks')
    issue=one(client)
    assert action(client,issue,'close').status_code==409
    issue=recheck(client,issue).json()
    assert not issue['candidate']['passed'] and action(client,issue,'close').status_code==409
    state['count']=0
    issue=recheck(client,issue).json()
    assert issue['candidate']['passed']
    assert action(client,issue,'close').json()['status']=='resolved'
    assert len(client.get('/api/data/issues').json()['issues'])==1
    state['count']=2
    client.post('/api/data/quality/checks')
    assert len(client.get('/api/data/issues').json()['issues'])==2
    assert one(client)['id']!=issue['id']


def test_new_observation_invalidates_success(env):
    client,state=env
    client.post('/api/data/quality/checks');state['count']=0
    issue=recheck(client,one(client)).json()
    client.post('/api/data/quality/checks')
    fresh=one(client)
    assert fresh['candidate'] is None
    assert action(client,issue,'close').status_code==409
    assert action(client,fresh,'close').status_code==409


def test_waiver_and_revision_conflict(env):
    client,_=env
    client.post('/api/data/quality/checks');issue=one(client)
    updated=action(client,issue,'start').json()
    assert action(client,issue,'note').status_code==409
    assert action(client,updated,'waive').json()['status']=='waived'
    check=client.post('/api/data/quality/checks').json()
    assert check['current']['blocking']
    assert len(client.get('/api/data/issues').json()['issues'])==2
    assert action(client,{**updated,'revision':updated['revision']+1},'reopen').status_code==409


def test_recheck_error_and_context_restoration(env,monkeypatch):
    client,_=env
    client.post('/api/data/quality/checks')
    def fail():raise ServiceError('timeout','数据库超时',504)
    monkeypatch.setattr(quality,'run_check',fail)
    issue=one(client)
    result=client.post('/api/data/issues/'+issue['id']+'/recheck',headers={'X-Dataset-Version':str(uuid4())},json={'expected_revision':issue['revision']})
    assert result.status_code==504
    fresh=one(client)
    assert fresh['pending'] is None and fresh['candidate'] is None
    assert REQUEST_VERSION.get()=='baseline-v1'
    assert action(client,fresh,'close').status_code==409


def test_cancelled_recheck_cannot_close(env,monkeypatch):
    client,state=env
    client.post('/api/data/quality/checks');state['count']=0
    original=quality.run_check
    def interrupted():
        check=original()
        issue=one(client)
        assert action(client,issue,'cancel_recheck').status_code==200
        return check
    monkeypatch.setattr(quality,'run_check',interrupted)
    assert recheck(client,one(client)).status_code==409
    fresh=one(client)
    assert fresh['pending'] is None and fresh['candidate'] is None
    assert action(client,fresh,'close').status_code==409


def test_threshold_does_not_hide_issue(env):
    client,_=env
    cfg=client.get('/api/data/quality/config').json()
    cfg['rules']['null_required']['notification_threshold']=10
    client.put('/api/data/quality/config',json={**{key:cfg[key] for key in ('rules','sample_limit')},'expected_revision':cfg['revision']})
    result=client.post('/api/data/quality/checks').json()
    assert not result['current']['rules'][0]['attention']
    assert one(client)['latest_count']==2


def test_failed_check_does_not_create_issue(env, monkeypatch):
    client,_=env
    def fail(_):raise ServiceError('query_timeout','超时',504)
    monkeypatch.setattr(quality,'current_quality',fail)
    assert client.post('/api/data/quality/checks').status_code==504
    assert client.get('/api/data/issues').json()['issues']==[]


def test_version_selection_rejected_before_recording(env):
    client,_=env
    assert client.post('/api/data/quality/checks',headers={'X-Dataset-Version':str(uuid4())}).status_code==409
    assert client.get('/api/data/quality/checks').json()['checks']==[]
