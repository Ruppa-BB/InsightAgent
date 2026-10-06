from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import json
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr, ValidationError

from backend.app.agent import provider, service, store
from backend.app.agent.schemas import AnalysisIntent
from backend.app.config import settings
from backend.app.errors import ServiceError
from backend.app.main import app
from backend.app.tools.analysis_tool import change_values, reconcile_contributions
from backend.app.tools.schemas import SalesQueryRequest
from backend.app.tools.sql_allowlist import build_sales_query, query_sales


def request(**overrides):
    return SalesQueryRequest(**({'metric_code': 'sales_amount', 'start_date': '2011-01-01',
        'end_date': '2011-04-01', 'group_by': 'month'} | overrides))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'analysis_store', tmp_path / 'records.sqlite3')
    monkeypatch.setattr(settings, 'llm_provider', 'demo')
    return TestClient(app)


@pytest.mark.parametrize('overrides', [
    {'metric_code':'delete_all'}, {'group_by':'secret_table'}, {'limit':501}, {'limit':0},
    {'limit':True}, {'start_date':'2011-05-01'}, {'end_date':'2011-01-01'},
    {'end_date':'2016-01-02'}, {'sql':'DROP TABLE x'}, {'filters':{'unknown':'x'}}])
def test_reject_bad_input(overrides):
    with pytest.raises(ValidationError): request(**overrides)


def test_calendar_bounds():
    request(end_date='2016-01-01')
    request(start_date='2024-02-29', end_date='2029-02-28')
    with pytest.raises(ValidationError): request(start_date='2024-02-29', end_date='2029-03-01')
    request(start_date='9998-01-01', end_date='9999-01-01')


def test_sql_rejects_identifiers():
    with pytest.raises(ValueError): build_sales_query('sales_amount; DROP TABLE x', 'month')
    with pytest.raises(ValueError): build_sales_query('sales_amount', 'secret_table')
    with pytest.raises(ValueError): build_sales_query('customer_count', 'customer')
    with pytest.raises(ValueError): build_sales_query('sales_amount', 'month', ('unknown',))


def test_filter_is_bound_and_truncation_reported():
    conn = MagicMock()
    conn.execute.return_value.mappings.return_value.all.return_value = [
        {'dimension': 'A', 'value': Decimal('1'), 'observation_count':1},
        {'dimension': 'B', 'value': Decimal('2'), 'observation_count':1}]
    malicious = "UK'; DELETE FROM fact_sales_order; --"
    result = query_sales(request(limit=1, filters={'country': malicious}), conn)
    query, params = conn.execute.call_args.args
    assert malicious not in str(query)
    assert params['country'] == malicious
    assert params['limit'] == 2
    assert result['truncated'] and result['row_count'] == 1


def contribution(key, previous, current, before_count, after_count):
    return {'dimension_key':key, 'dimension':key, 'previous':Decimal(previous),
            'current':Decimal(current), 'previous_count':before_count, 'current_count':after_count}


def test_attribution_includes_new_and_lost():
    rows = [contribution('lost','100','0',1,0), contribution('new','0','20',0,1),
            contribution('same','10','15',1,1)]
    result = reconcile_contributions(rows, Decimal('110'), Decimal('35'))
    assert result['delta_sum'] == Decimal('-75')
    assert result['negative'][0]['status'] == 'lost'
    assert result['positive'][0]['status'] == 'new'
    assert result['other_delta'] == 0
    assert result['negative'][0]['share_of_net_change'] > 1


def test_attribution_fails_closed_on_mismatch():
    with pytest.raises(ServiceError, match='不一致'):
        reconcile_contributions([contribution('a','10','8',1,1)], Decimal('20'), Decimal('8'))


def test_zero_baseline_and_zero_delta():
    assert change_values(Decimal('0'), Decimal('5'))['change_ratio'] is None
    result = reconcile_contributions([contribution('a','10','10',1,1)], Decimal('10'), Decimal('10'))
    assert result['delta_sum'] == 0


@pytest.mark.parametrize('question', ['为什么 2011 年 2 月销售额比 1 月下降？',
    '2011 年销售额趋势', '2011 年 2 月订单数比 1 月变化', '2011年2月客户数比1月减少'])
def test_demo_supported(question):
    assert provider.parse_demo(question).status == 'ready'


@pytest.mark.parametrize('question', ['最近销售额', '2月比1月销售额', '2011年利润趋势',
    '2011年销售额趋势只看法国', '2011年2月比1月下降', '2011年1月销售额比12月下降',
    '2011年13月销售额趋势', '2011年2月销售额比1月订单数变化'])
def test_demo_clarifies_ambiguous_or_unsupported(question):
    assert provider.parse_demo(question).status == 'needs_clarification'


def test_followup_keeps_periods_and_filters():
    previous = provider.parse_demo('为什么2011年2月销售额比1月下降').intent
    country = provider.parse_demo('再看英国', previous).intent
    customer = provider.parse_demo('再看客户18102', country).intent
    assert customer.period == previous.period
    assert customer.filters.country == 'United Kingdom'
    assert customer.filters.customer_code == 'CUST_18102'


def fake_model(monkeypatch, content):
    monkeypatch.setattr(settings, 'llm_provider', 'openai_compatible')
    monkeypatch.setattr(settings, 'llm_api_key', SecretStr('test-key'))
    monkeypatch.setattr(settings, 'llm_model', 'test-model')
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
        {'choices':[{'message':{'content':content}}]}).encode()
    monkeypatch.setattr(provider.request, 'build_opener', lambda *args: opener)
    return opener


def test_model_plan_validated(monkeypatch):
    plan = provider.parse_demo('2011年销售额趋势').model_dump_json()
    opener = fake_model(monkeypatch, plan)
    result = provider.parse_question('给我2011年的趋势')
    assert result.intent.metric_code == 'sales_amount'
    body = json.loads(opener.open.call_args.args[0].data)
    assert body['response_format'] == {'type':'json_object'}


def test_model_cannot_supply_sql(monkeypatch):
    fake_model(monkeypatch, '{"status":"ready","sql":"DELETE FROM x"}')
    with pytest.raises(ServiceError) as exc: provider.parse_question('test')
    assert exc.value.code == 'invalid_model_output'


def test_missing_model_credentials_no_silent_demo(monkeypatch):
    monkeypatch.setattr(settings, 'llm_provider', 'openai_compatible')
    monkeypatch.setattr(settings, 'llm_api_key', None)
    with pytest.raises(ServiceError) as exc: provider.parse_question('2011年销售额趋势')
    assert exc.value.code == 'llm_unconfigured'


@pytest.fixture
def fake_database(monkeypatch):
    @contextmanager
    def connection(): yield object()
    monkeypatch.setattr(service, 'read_connection', connection)
    monkeypatch.setattr(service, 'current_quality', lambda conn: {'blocking':False,'data_version':'fixture','rules':[]})
    monkeypatch.setattr(service, 'query_coverage', lambda conn: {'start_date':date(2009,12,1),'last_date':date(2011,12,9)})
    def sales(req, conn):
        value = Decimal('200') if req.start_date.month == 1 else Decimal('100')
        return {'rows':[{'dimension':req.start_date, 'value':value, 'observation_count':1}],
            'row_count':1, 'truncated':False, 'empty':False, 'warnings':[], 'sql':'SELECT fixture', 'parameters':{}}
    monkeypatch.setattr(service, 'query_sales', sales)
    def attribution(conn, dimension, previous_period, current_period, filters, previous, current):
        return {'dimension':dimension, **reconcile_contributions(
            [contribution('fixture', str(previous), str(current), 1, 1)], previous, current)}
    monkeypatch.setattr(service, 'query_contributions', attribution)


def test_agent_api_context_save_export(client, fake_database):
    response = client.post('/api/analyze', json={'question':'为什么2011年2月销售额比1月下降'})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['comparison']['delta'] == '-100'
    assert len(data['attributions']) == 3
    assert all(a['reconciled'] for a in data['attributions'])
    assert client.get('/api/analyses/'+data['id']).json() == data
    assert client.get('/api/analyses/'+data['id']+'/export/md').status_code == 200
    follow = client.post('/api/analyze', json={'question':'再看英国', 'session_id':data['session_id']}).json()
    assert follow['intent']['filters']['country'] == 'United Kingdom'
    assert len(client.get('/api/analyses').json()) == 2


def test_api_invalid_input_never_queries(client, monkeypatch):
    query = MagicMock()
    monkeypatch.setattr('backend.app.main.query_sales', query)
    response = client.post('/api/query', json={'metric_code':'delete_all','start_date':'2011-01-01','end_date':'2011-02-01'})
    assert response.status_code == 422
    query.assert_not_called()


def test_api_clarification_and_missing_history(client, monkeypatch):
    query = MagicMock()
    monkeypatch.setattr(service, 'execute_intent', query)
    assert client.post('/api/analyze', json={'question':'最近利润下降了吗'}).json()['status'] == 'needs_clarification'
    query.assert_not_called()
    assert client.get('/api/analyses/'+str(uuid4())).status_code == 404
    assert client.post('/api/analyze', json={'question':'再看英国','session_id':str(uuid4())}).status_code == 404


def test_api_timeout_is_safe(client, monkeypatch):
    def timeout(req): raise ServiceError('query_timeout', '查询超时', 504)
    monkeypatch.setattr('backend.app.main.query_sales', timeout)
    response = client.post('/api/query', json=request().model_dump(mode='json'))
    assert response.status_code == 504
    assert 'DATABASE_URL' not in response.text


def test_home_and_static_assets(client):
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/health').json()['mode'] == 'demo'
    assert 'database_url' not in client.get('/health').text


def test_comparison_disallows_overlap():
    with pytest.raises(ValidationError):
        AnalysisIntent(action='compare', period={'start_date':'2011-01-01','end_date':'2011-02-01'},
            previous_period={'start_date':'2011-01-01','end_date':'2011-02-01'})


def test_monthly_mom_does_not_skip_missing_month():
    from backend.app.tools.schemas import DateRange
    from backend.app.tools.mom_tool import calculate_monthly_mom
    period = DateRange(start_date='2011-01-01', end_date='2011-05-01')
    rows = calculate_monthly_mom(period, {date(2010,12,1):Decimal('0'),date(2011,1,1):Decimal('10'),
        date(2011,3,1):Decimal('20'),date(2011,4,1):Decimal('30')})
    assert rows[0]['null_reason'] == 'zero_baseline'
    assert rows[1]['null_reason'] == 'missing_month'
    assert rows[2]['sales_mom'] is None  # Do not compare March against January.
    assert rows[3]['sales_mom'] == Decimal('.5')


def test_monthly_mom_rejects_partial_period():
    from backend.app.tools.schemas import DateRange, SalesFilters
    from backend.app.tools.mom_tool import query_sales_mom
    with pytest.raises(ValueError):
        query_sales_mom(DateRange(start_date='2011-01-15',end_date='2011-03-01'), SalesFilters(), MagicMock())


def test_deepseek_budget_and_cached_parse(monkeypatch):
    provider.clear_parse_cache()
    plan = provider.parse_demo('2011年销售额趋势').model_dump_json()
    opener = fake_model(monkeypatch, plan)
    monkeypatch.setattr(settings, 'llm_provider', 'deepseek')
    monkeypatch.setattr(settings, 'llm_base_url', 'https://api.deepseek.com')
    monkeypatch.setattr(settings, 'llm_model', 'deepseek-flash')
    monkeypatch.setattr(settings, 'llm_cache_seconds', 3600)
    first = provider.parse_question('请给我2011年销售额趋势')
    second = provider.parse_question('请给我2011年销售额趋势')
    body = json.loads(opener.open.call_args.args[0].data)
    assert body['thinking'] == {'type':'disabled'}
    assert body['max_tokens'] == 700
    assert body['model'] == 'deepseek-flash'
    assert first._usage['calls'] == 1
    assert second._usage['calls'] == 0 and second._usage['strategy'] == 'cache'
    assert second.intent == first.intent
    assert opener.open.call_count == 1


def test_local_followup_never_calls_model(monkeypatch):
    monkeypatch.setattr(settings, 'llm_provider', 'deepseek')
    context = provider.parse_demo('为什么2011年2月销售额比1月下降').intent
    opener = MagicMock()
    monkeypatch.setattr(provider.request, 'build_opener', opener)
    parsed = provider.parse_question('再看英国', context)
    assert parsed._usage['calls'] == 0
    assert parsed._usage['strategy'] == 'local_followup'
    assert parsed.intent.period == context.period
    assert parsed.intent.filters.country == 'United Kingdom'
    opener.assert_not_called()


def test_cache_does_not_cross_context_or_credentials(monkeypatch):
    provider.clear_parse_cache()
    plan = provider.parse_demo('2011年销售额趋势').model_dump_json()
    opener = fake_model(monkeypatch, plan)
    provider.parse_question('same test question')
    context = provider.parse_demo('为什么2011年2月销售额比1月下降').intent
    provider.parse_question('same test question', context)
    monkeypatch.setattr(settings, 'llm_api_key', SecretStr('different-test-key'))
    provider.parse_question('same test question')
    assert opener.open.call_count == 3


def test_truncated_model_plan_not_executed_or_cached(monkeypatch):
    provider.clear_parse_cache()
    opener = fake_model(monkeypatch, '{"status":"ready"')
    opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
        {'choices':[{'finish_reason':'length','message':{'content':'{"status":"ready"'}}]}).encode()
    with pytest.raises(ServiceError) as exc: provider.parse_question('output budget test')
    assert exc.value.code == 'model_output_limit'
    assert not provider._CACHE


def test_real_provider_usage_saved_by_api(client, fake_database, monkeypatch):
    parsed = provider.parse_demo('为什么2011年2月销售额比1月下降')
    parsed._usage = {'strategy':'model','calls':1,'total_tokens':543,'model':'deepseek-flash'}
    monkeypatch.setattr(service, 'parse_question', lambda *args: parsed)
    response = client.post('/api/analyze', json={'question':'销售变化'})
    assert response.status_code == 200
    assert response.json()['model_usage']['total_tokens'] == 543
    saved = client.get('/api/analyses/'+response.json()['id']).json()
    assert saved['model_usage'] == response.json()['model_usage']
