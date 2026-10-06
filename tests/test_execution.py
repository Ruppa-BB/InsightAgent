from contextlib import contextmanager
from uuid import UUID, uuid4
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.app.agent import execution, service, store
from backend.app.agent.schemas import AnalysisIntent, AnalyzeRequest, ParsedQuestion, DrillRequest
from backend.app.agent.drill import derive_intent
from backend.app.config import settings
from backend.app.errors import ServiceError
from backend.app.main import app


@pytest.fixture(autouse=True)
def isolate(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'runs.sqlite3')


def intent(strategy='auto',action='compare'):
    return AnalysisIntent(strategy=strategy,action=action,period={'start_date':'2011-02-01','end_date':'2011-03-01'},previous_period={'start_date':'2011-01-01','end_date':'2011-02-01'} if action=='compare' else None)


def test_strategy_contract_and_drill_reset():
    selected,name=execution.select_strategy(intent('customer_contribution'))
    assert selected.contribution_dimensions==['customer'] and name=='customer_contribution'
    selected,name=execution.select_strategy(intent('product_mix','trend'))
    assert selected.group_by=='product'
    assert execution.select_strategy(intent())[1]=='sales_decline'
    assert derive_intent(intent('customer_contribution'),DrillRequest(metric_code='order_count')).strategy=='auto'
    assert derive_intent(intent('customer_contribution'),DrillRequest(contribution_dimensions=['customer_product'])).strategy=='auto'
    with pytest.raises(ValueError):
        intent('customer_contribution','trend')
    with pytest.raises(ValueError):
        AnalysisIntent.model_validate({**intent('product_mix').model_dump(),'metric_code':'order_count'})
    with pytest.raises(ValueError):
        intent('execute_python')


def test_cumulative_budget_and_time(monkeypatch):
    monkeypatch.setattr(settings,'agent_max_steps',1)
    run=execution.Run()
    assert run.call('first',lambda:4)==4
    second=Mock()
    with pytest.raises(ServiceError,match='步骤预算'):
        run.call('second',second)
    second.assert_not_called()
    assert run.record['steps_used']==1
    tick=[0.0]
    monkeypatch.setattr(execution,'monotonic',lambda:tick[0])
    run=execution.Run()
    tick[0]=61
    with pytest.raises(ServiceError,match='累计时间'):
        run.call('not_started',second)
    assert run.record['steps_used']==0


def test_running_call_over_time_stops(monkeypatch):
    tick=[0.0]
    monkeypatch.setattr(execution,'monotonic',lambda:tick[0])
    run=execution.Run()
    def slow():
        tick[0]=61
        return 'do not use this result'
    with pytest.raises(ServiceError):
        run.call('slow',slow)
    assert run.record['steps'][0]['status']=='failed'
    assert run.record['steps'][0]['error']=='run_timeout'


def test_model_budget_gate_and_reported_tokens(monkeypatch):
    run=execution.Run()
    run.model_gate()
    with pytest.raises(ServiceError):
        run.model_gate()
    run.usage({'calls':1,'total_tokens':30})
    assert run.record['model_calls']==1 and run.record['tokens']==30
    assert run.record['tokens_status']=='reported'
    with pytest.raises(ServiceError):
        run.usage({'calls':1,'total_tokens':20})
    assert run.record['model_calls']==2 and run.record['tokens']==50
    monkeypatch.setattr(settings,'agent_max_model_calls',0)
    with pytest.raises(ServiceError):
        execution.Run().model_gate()


def test_parser_failure_saved_and_no_execution(monkeypatch):
    monkeypatch.setattr(service,'parse_question',Mock(side_effect=ServiceError('model_unavailable','模型不可用。',502)))
    connection=Mock()
    monkeypatch.setattr(service,'read_connection',connection)
    with pytest.raises(ServiceError) as error:
        service.analyze(AnalyzeRequest(question='test'))
    record=store.get_run(UUID(error.value.run_id))
    assert record['status']=='failed' and record['error']['code']=='model_unavailable'
    assert record['steps'][0]['status']=='failed' and record['retries']==0
    connection.assert_not_called()
    assert store.recent()==[]


def test_token_budget_stops_before_queries(monkeypatch):
    parsed=ParsedQuestion(status='ready',intent=intent())
    parsed._usage={'calls':1,'total_tokens':5000}
    monkeypatch.setattr(service,'parse_question',lambda *args:parsed)
    conn=Mock()
    monkeypatch.setattr(service,'read_connection',conn)
    with pytest.raises(ServiceError) as error:
        service.analyze(AnalyzeRequest(question='test'))
    assert error.value.code=='model_budget_exceeded'
    assert store.get_run(UUID(error.value.run_id))['tokens']==5000
    conn.assert_not_called()


def test_tool_failure_stops_and_exposes_run(monkeypatch):
    @contextmanager
    def connection():
        yield object()
    monkeypatch.setattr(service,'read_connection',connection)
    monkeypatch.setattr(service,'current_quality',Mock(side_effect=ServiceError('query_timeout','查询超时。',504)))
    later=Mock()
    monkeypatch.setattr(service,'query_coverage',later)
    client=TestClient(app)
    response=client.post('/api/analyze/structured',json=intent().model_dump(mode='json'))
    assert response.status_code==504
    record=client.get('/api/runs/'+response.json()['run_id']).json()
    assert record['status']=='failed' and record['steps_used']==1
    assert record['steps'][0]['error']=='query_timeout'
    later.assert_not_called()
    assert client.get('/api/runs/'+str(uuid4())).status_code==404


def test_clarification_record(monkeypatch):
    monkeypatch.setattr(service,'parse_question',lambda *args:ParsedQuestion(status='needs_clarification',message='请明确年份'))
    result=service.analyze(AnalyzeRequest(question='销售额'))
    assert store.get_run(UUID(result['run_id']))['status']=='needs_clarification'
    assert result['execution']['model_calls']==0


def test_real_provider_gate_before_network(monkeypatch):
    from pydantic import SecretStr
    from backend.app.agent import provider
    monkeypatch.setattr(settings,'agent_max_model_calls',0)
    monkeypatch.setattr(settings,'llm_provider','deepseek')
    monkeypatch.setattr(settings,'llm_base_url','https://api.deepseek.com')
    monkeypatch.setattr(settings,'llm_model','deepseek-flash')
    monkeypatch.setattr(settings,'llm_api_key',SecretStr('test-placeholder'))
    network=Mock()
    monkeypatch.setattr(provider.request,'build_opener',network)
    run=execution.Run()
    marker=execution.MODEL_GATE.set(run.model_gate)
    try:
        with pytest.raises(ServiceError) as error:
            provider.parse_question('model budget unique gate test')
        assert error.value.code=='model_budget_exceeded'
        network.assert_not_called()
        assert run.record['model_calls']==0
    finally:
        execution.MODEL_GATE.reset(marker)
