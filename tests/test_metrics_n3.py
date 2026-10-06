from datetime import date
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from backend.app.agent import service
from backend.app.agent.provider import parse_demo
from backend.app.agent.schemas import AnalysisIntent
from backend.app.config import settings
from backend.app.main import app
from backend.app.tools.schemas import DateRange, SalesFilters, SalesYoYRequest
from backend.app.tools.yoy_tool import calculate_monthly_yoy, query_sales_yoy


@pytest.mark.parametrize('question,code',[
    ('2011年1月销量是多少','sales_quantity'),('2011年1月客单价是多少','average_order_value'),
    ('2011年1月加权平均售价是多少','average_selling_price')])
def test_new_metric_parser(question,code):
    parsed=parse_demo(question)
    assert parsed.status=='ready' and parsed.intent.metric_code==code


def test_yoy_parser_and_month_contract():
    assert parse_demo('2011年销售额同比趋势').intent.action=='yoy'
    with pytest.raises(ValueError):
        AnalysisIntent(action='yoy',metric_code='order_count',period={'start_date':'2011-01-01','end_date':'2012-01-01'})
    with pytest.raises(ValueError):
        AnalysisIntent(action='yoy',period={'start_date':'2011-01-02','end_date':'2011-02-01'})


def test_yoy_missing_zero_partial_and_calendar_baseline():
    coverage={'start_date':date(2009,12,1),'last_date':date(2011,12,9)}
    period=DateRange(start_date='2011-01-01',end_date='2011-04-01')
    values={date(2010,1,1):Decimal('10'),date(2011,1,1):Decimal('12'),
            date(2010,2,1):Decimal('0'),date(2011,2,1):Decimal('2'),date(2011,3,1):Decimal('20')}
    rows=calculate_monthly_yoy(period,values,coverage)
    assert rows[0]['sales_yoy']==Decimal('.2')
    assert rows[1]['sales_yoy'] is None and rows[1]['null_reason']=='zero_baseline'
    assert rows[2]['sales_yoy'] is None and rows[2]['null_reason']=='missing_month'
    partial=calculate_monthly_yoy(DateRange(start_date='2011-12-01',end_date='2012-01-01'),
                                  {date(2010,12,1):Decimal('10'),date(2011,12,1):Decimal('5')},coverage)
    assert partial[0]['null_reason']=='incomplete_period' and partial[0]['sales_yoy'] is None
    # Leap February is still a whole calendar month, rather than a 365-day shift.
    leap=calculate_monthly_yoy(DateRange(start_date='2024-02-01',end_date='2024-03-01'),
            {date(2023,2,1):Decimal('2'),date(2024,2,1):Decimal('3')},
            {'start_date':date(2023,1,1),'last_date':date(2024,2,29)})
    assert leap[0]['baseline_month']==date(2023,2,1) and leap[0]['sales_yoy']==Decimal('.5')


def test_yoy_api_handles_request_subclass_and_filters(monkeypatch):
    import backend.app.tools.yoy_tool as tool
    query=Mock(return_value={'rows':[]})
    monkeypatch.setattr(tool,'query_sales',query)
    monkeypatch.setattr(tool,'query_coverage',lambda conn:{'start_date':date(2009,12,1),'last_date':date(2011,12,9)})
    request=SalesYoYRequest(start_date='2011-01-01',end_date='2011-02-01',filters={'country':'United Kingdom'})
    result=query_sales_yoy(request,request.filters,object())
    assert query.call_count==2
    assert all(call.args[0].filters.country=='United Kingdom' for call in query.call_args_list)
    assert result['rows'][0]['null_reason']=='missing_month'
    monkeypatch.setattr('backend.app.main.query_sales_yoy',lambda period,filters:result)
    client=TestClient(app)
    assert client.post('/tools/sales-yoy',json=request.model_dump(mode='json')).status_code==200


def test_average_null_comparison_is_not_zero_or_crash(monkeypatch,tmp_path):
    from contextlib import contextmanager
    @contextmanager
    def connection(): yield object()
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'history.sqlite3')
    monkeypatch.setattr(service,'read_connection',connection)
    monkeypatch.setattr(service,'current_quality',lambda conn:{'blocking':False,'data_version':'fixture','rules':[]})
    monkeypatch.setattr(service,'query_coverage',lambda conn:{'start_date':date(2009,12,1),'last_date':date(2011,12,9)})
    monkeypatch.setattr(service,'query_sales',Mock(side_effect=[
        {'rows':[{'value':None}],'empty':True,'warnings':['分母为0']},
        {'rows':[{'value':Decimal('20')}],'empty':False,'warnings':[]}]))
    intent=parse_demo('2011年2月客单价比1月变化').intent
    result=service.execute_intent(intent,'compare',uuid4(),'structured')
    assert result['comparison'] is None and result['data'][0]['value'] is None
    assert '不把缺失值当成0' in result['answer']
    assert result['attributions']==[]
