from copy import deepcopy
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from backend.app.config import settings
from backend.app.main import app
from backend.app.data_management import metric_versions as metrics
from backend.app.errors import ServiceError
from backend.app.semantic.metrics import get_metric
from backend.app.tools.schemas import SalesQueryRequest, DateRange, SalesFilters
from backend.app.tools.sql_allowlist import query_sales
from backend.app.tools.mom_tool import query_sales_mom
from backend.app.tools.yoy_tool import query_sales_yoy

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'metrics.sqlite3')
    return TestClient(app)

def head(client,code='sales_amount'):
    return next(item for item in client.get('/api/data/metrics').json()['metrics'] if item['code']==code)

def draft(client,current,**changes):
    definition=current['published']
    return client.put('/api/data/metrics/'+current['code']+'/draft',json={
        'expected_revision':current['revision'],'name':definition['name'],
        'description':definition['description'],'enabled':definition['enabled'],**changes})

def publish(client,current):
    return client.post('/api/data/metrics/'+current['code']+'/publish',json={'expected_revision':current['revision']})

def test_draft_publish_immutable_history(client):
    old=head(client)
    saved=draft(client,old,description='已完成订单的商品销售收入，单位 GBP。').json()
    assert metrics.definitions()['sales_amount']==old['published']
    assert draft(client,old).status_code==409
    assert publish(client,old).status_code==409
    new=publish(client,saved).json()
    assert new['published']['version']==2 and new['draft'] is None
    assert publish(client,new).status_code==409
    history=client.get('/api/data/metrics/sales_amount/versions').json()['versions']
    assert history[1]['definition']==old['published']
    assert new['published']['formula']==old['published']['formula']

@pytest.mark.parametrize('change',[{'formula':'DELETE FROM fact_sales_detail'},{'unit':'USD'},{'dimensions':['secret']},{'name':'   '}])
def test_template_contract_rejects_mutation(client,change):
    assert draft(client,head(client),**change).status_code==422
    assert head(client)['revision']==1

def test_pin_survives_publish_and_disabled_tools(client):
    original=metrics.definitions()
    token=metrics.PINNED.set(deepcopy(original))
    try:
        saved=draft(client,head(client),enabled=False).json()
        publish(client,saved)
        assert get_metric('sales_amount')['enabled']
        assert metrics.evidence(['sales_amount'])=={'sales_amount':original['sales_amount']}
    finally:metrics.PINNED.reset(token)
    connection=Mock()
    request=SalesQueryRequest(metric_code='sales_amount',start_date='2011-01-01',end_date='2011-02-01')
    with pytest.raises(ServiceError,match='停用'):query_sales(request,connection)
    connection.execute.assert_not_called()
    reenabled=publish(client,draft(client,head(client),enabled=True).json()).json()
    assert reenabled['published']['version']==3

@pytest.mark.parametrize('code,tool',[('sales_mom',query_sales_mom),('sales_yoy',query_sales_yoy)])
def test_derived_disable_checked_before_database(client,code,tool):
    publish(client,draft(client,head(client,code),enabled=False).json())
    conn=Mock()
    with pytest.raises(ServiceError):tool(DateRange(start_date='2011-01-01',end_date='2011-02-01'),SalesFilters(),conn)
    conn.execute.assert_not_called()
