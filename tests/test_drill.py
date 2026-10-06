from decimal import Decimal
from uuid import uuid4
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.schemas import AnalysisIntent, DrillRequest
from backend.app.agent.drill import derive_intent
from backend.app.agent.provider import parse_question
from backend.app.config import settings
from backend.app.main import app
from backend.app.tools.analysis_tool import reconcile_contributions, query_contributions
from backend.app.tools.schemas import SalesFilters


def parent(**updates):
    data={'action':'compare','period':{'start_date':'2011-02-01','end_date':'2011-03-01'},'previous_period':{'start_date':'2011-01-01','end_date':'2011-02-01'},'filters':{'country':'United Kingdom'}}
    data.update(updates)
    return AnalysisIntent.model_validate(data)


def test_inherit_replace_and_metric_compatibility():
    source=parent(group_by='customer')
    child=derive_intent(source,DrillRequest(filters={'customer_code':'CUST_12346'}))
    assert child.filters.country is None
    assert source.filters.country=='United Kingdom'
    assert child.period==source.period and child.previous_period==source.previous_period
    assert derive_intent(source,DrillRequest(metric_code='customer_count')).group_by=='month'
    yoy=parent(action='yoy',previous_period=None)
    reset=derive_intent(yoy,DrillRequest(metric_code='order_count'))
    assert reset.action=='trend' and reset.previous_period is None
    with pytest.raises(ValueError):
        derive_intent(source,DrillRequest(period={'start_date':'2010-01-01','end_date':'2010-02-01'}))
    with pytest.raises(ValueError):
        DrillRequest(filters={'table':'secret'})
    with pytest.raises(ValueError):
        derive_intent(source,DrillRequest(contribution_dimensions=['customer','customer']))


def test_exact_followups_no_model(monkeypatch):
    monkeypatch.setattr(settings,'llm_provider','deepseek')
    source=parent()
    customer=parse_question('再看客户 12346',source)
    product=parse_question('再看商品23166',customer.intent)
    assert product.intent.filters.customer_code=='CUST_12346'
    assert product.intent.filters.product_code=='23166'
    assert product.intent.filters.country=='United Kingdom'
    assert product._usage['calls']==0
    assert parse_question('取消商品筛选',product.intent).intent.filters.product_code is None
    assert parse_question('取消全部筛选',product.intent).intent.filters==SalesFilters()
    assert parse_question('改看订单数',product.intent).intent.metric_code=='order_count'


def test_pair_new_lost_and_combined_key(monkeypatch):
    connection=Mock()
    connection.execute.return_value.mappings.return_value.all.return_value=[
        {'dimension_key':['A','B|C'],'dimension':'new','previous':Decimal(0),'current':Decimal(8),'previous_count':0,'current_count':1},
        {'dimension_key':['A|B','C'],'dimension':'lost','previous':Decimal(5),'current':Decimal(0),'previous_count':1,'current_count':0}]
    source=parent()
    result=query_contributions(connection,'customer_product',source.previous_period,source.period,source.filters,Decimal(5),Decimal(8))
    assert result['reconciled'] and result['delta_sum']==3
    assert result['positive'][0]['status']=='new' and result['negative'][0]['status']=='lost'
    assert result['positive'][0]['drill_filters']=={'customer_code':'A','product_code':'B|C'}
    assert 'jsonb_build_array' in result['sql']
    from backend.app.tools import analysis_tool
    from backend.app.errors import ServiceError
    monkeypatch.setattr(analysis_tool,'MAX_PAIR_GROUPS',1)
    with pytest.raises(ServiceError,match='不会使用截断数据'):
        query_contributions(connection,'customer_product',source.previous_period,source.period,source.filters,Decimal(5),Decimal(8))
    with pytest.raises(ServiceError):
        reconcile_contributions([],Decimal(1),Decimal(1))


def test_unknown_parent_and_invalid_drill(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'drill.sqlite3')
    client=TestClient(app)
    assert client.post(f'/api/analyses/{uuid4()}/drill',json={}).status_code==404
    assert client.post(f'/api/analyses/{uuid4()}/drill',json={'filters':{'country':"x'; DROP TABLE x;--"},'sql':'delete'}).status_code==422
