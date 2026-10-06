from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID, uuid4
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.app.agent import reports, store
from backend.app.config import settings
from backend.app.errors import ServiceError
from backend.app.main import app


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'reports.sqlite3')
    @contextmanager
    def connection():
        yield object()
    monkeypatch.setattr(reports,'read_connection',connection)
    def dashboard(month,connection):
        return {'month':month,'period':{'start_date':month,'end_date':date(2011,3,1)},'previous_month':date(2011,1,1),
                'cards':[{'metric_code':'sales_amount','name':'销售额','unit':'GBP','value':Decimal('100.00'),'previous':Decimal('80.00'),'change_ratio':Decimal('.25'),'null_reason':None}],
                'quality':{'summary':{'start_date':date(2009,12,1),'last_date':date(2011,12,9)}},
                'warnings':['样本没有成本数据'],'trend':[{'dimension':month,'value':Decimal('100.00')}],
                'rankings':{'customer':[{'dimension':'<img> | [link]','value':Decimal('100.00')}],'product':[]},
                'detection':{'rows':[{'month':month,'checks':[{'method':'mom_drop','status':'normal','observed_value':Decimal('100.00'),'baseline':Decimal('80.00'),'change_ratio':Decimal('.25'),'threshold':Decimal('.2'),'baseline_months':[date(2011,1,1)],'reason':None}]}]},
                'tools':[{'sql':'SELECT :start_date','parameters':{'start_date':month}}]}
    monkeypatch.setattr(reports,'query_dashboard',dashboard)
    def contribution(connection,dimension,*args):
        return {'dimension':dimension,'reconciled':True,'group_count':1,'delta_sum':Decimal('20.00'),'other_delta':Decimal(0),'negative':[],
                'positive':[{'dimension':'sample','previous':Decimal('80.00'),'current':Decimal('100.00'),'delta':Decimal('20.00'),'status':'continuing'}]}
    monkeypatch.setattr(reports,'query_contributions',contribution)
    return reports.generate_report(reports.ReportRequest(month='2011-02-01'))


def test_report_hash_sections_numbers_and_escaping(prepared):
    report=prepared
    digest=sha256(json.dumps(report['snapshot'],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    assert digest==report['snapshot_sha256']
    assert all(f'## {index}. {title}' in report['markdown'] for index,title in enumerate(['摘要','指标','销售','客户','产品','异常','发现','风险'],1))
    assert '100.00 GBP' in report['markdown'] and '20.00 GBP' in report['markdown']
    assert '<img>' not in report['markdown'].split('## 附件：')[0]
    assert '\\|' in report['markdown'] and '&lt;img&gt;' in report['markdown']
    assert report['execution']['model_calls']==0
    assert store.get_run(UUID(report['run_id']))['report_id']==report['id']


def test_read_export_snapshot_does_not_query_db(prepared,monkeypatch):
    monkeypatch.setattr(reports,'read_connection',Mock(side_effect=AssertionError('must not query')))
    monkeypatch.setattr(reports,'render_markdown',Mock(side_effect=AssertionError('must not rerender')))
    client=TestClient(app)
    identifier=prepared['id']
    saved=client.get('/api/reports/'+identifier).json()
    assert saved==prepared
    assert client.get(f'/api/reports/{identifier}/export/md').text==prepared['markdown']
    assert client.get(f'/api/reports/{identifier}/export/json').json()==prepared
    assert client.get('/api/reports').json()[0]['id']==identifier
    assert client.get('/api/reports/'+str(uuid4())).status_code==404
    assert client.get(f'/api/reports/{identifier}/export/pdf').status_code==422


def test_immutable_and_new_versions(prepared):
    another=reports.generate_report(reports.ReportRequest(month='2011-02-01'))
    assert another['id']!=prepared['id']
    assert store.get_business_report(UUID(prepared['id']))==prepared
    assert len(store.business_reports())==2


@pytest.mark.parametrize('body',[{'month':'2011-02-02'},{'month':'bad'},{'month':'2011-02-01','sql':'DELETE'},{'month':'9999-01-01'}])
def test_report_invalid_input(body):
    assert TestClient(app).post('/api/reports',json=body).status_code==422


def test_failed_report_preserves_no_conclusion(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'failed.sqlite3')
    @contextmanager
    def connection():
        yield object()
    monkeypatch.setattr(reports,'read_connection',connection)
    monkeypatch.setattr(reports,'query_dashboard',Mock(side_effect=ServiceError('incomplete_period','月份不完整。',422)))
    response=TestClient(app).post('/api/reports',json={'month':'2011-12-01'})
    assert response.status_code==422 and store.business_reports()==[]
    assert store.get_run(UUID(response.json()['run_id']))['status']=='failed'


def test_report_save_failure_is_visible_and_no_new_report(prepared,monkeypatch):
    import sqlite3
    monkeypatch.setattr(store,'save_business_report',Mock(side_effect=sqlite3.OperationalError('disk full private diagnostic')))
    response=TestClient(app).post('/api/reports',json={'month':'2011-02-01'})
    assert response.status_code==503 and response.json()['error']['code']=='storage_unavailable'
    assert 'private diagnostic' not in response.text
    assert len(store.business_reports())==1
    run=store.get_run(UUID(response.json()['run_id']))
    assert run['status']=='failed' and run['error']['code']=='storage_unavailable'
