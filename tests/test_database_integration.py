"""Opt in: INSIGHT_DB_TESTS=1 uv run pytest tests/test_database_integration.py.
Uses configured business data read-only; saved test history uses a temporary folder.
"""
import os
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from backend.app import db
from backend.app.agent import service
from backend.app.agent.provider import parse_demo
from backend.app.config import settings
from backend.app.errors import ServiceError
from backend.app.tools.schemas import SalesQueryRequest
from backend.app.tools.sql_allowlist import query_sales

pytestmark = pytest.mark.skipif(os.environ.get('INSIGHT_DB_TESTS') != '1', reason='Opt-in local PostgreSQL tests')


def test_real_demo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'analysis_store', tmp_path / 'integration.sqlite3')
    intent = parse_demo('为什么2011年2月销售额比1月下降').intent
    result = service.execute_intent(intent, 'integration', uuid4(), 'demo')
    assert result['comparison']['previous'] == Decimal('569445.04')
    assert result['comparison']['current'] == Decimal('447137.35')
    assert result['comparison']['delta'] == Decimal('-122307.69')
    assert len(result['attributions']) == 3
    assert all(a['reconciled'] and a['delta_sum'] == Decimal('-122307.69') for a in result['attributions'])


def test_read_only_timeout_and_pool_reset(monkeypatch):
    with db.engine.connect() as c:
        original_timeout = c.execute(text('SHOW statement_timeout')).scalar()
        original_readonly = c.execute(text('SHOW transaction_read_only')).scalar()
    with db.read_connection() as c:
        assert c.execute(text('SHOW transaction_read_only')).scalar() == 'on'
    # Even if the guard regresses, WHERE false never deletes a row.
    with pytest.raises(ServiceError) as write_error:
        with db.read_connection() as c:
            c.execute(text('DELETE FROM fact_sales_order WHERE false'))
    assert write_error.value.__cause__.orig.sqlstate == '25006'
    monkeypatch.setattr(settings, 'query_timeout_ms', 100)
    with pytest.raises(ServiceError) as timeout:
        with db.read_connection() as c: c.execute(text('SELECT pg_sleep(1)'))
    assert timeout.value.code == 'query_timeout'
    with db.engine.connect() as c:
        assert c.execute(text('SHOW statement_timeout')).scalar() == original_timeout
        assert c.execute(text('SHOW transaction_read_only')).scalar() == original_readonly
        assert c.execute(text('SELECT 1')).scalar() == 1


def test_real_query_truncation_and_injection():
    with db.read_connection() as c:
        result = query_sales(SalesQueryRequest(metric_code='sales_amount', start_date='2011-01-01',
            end_date='2011-02-01', group_by='customer', limit=1), c)
        assert result['truncated'] and result['row_count'] == 1
        malicious = query_sales(SalesQueryRequest(metric_code='sales_amount', start_date='2011-01-01',
            end_date='2011-02-01', filters={'country':"United Kingdom' OR 1=1 --"}), c)
        assert malicious['empty'] and malicious['rows'][0]['value'] == 0


def test_real_monthly_mom():
    from backend.app.tools.schemas import DateRange, SalesFilters
    from backend.app.tools.mom_tool import query_sales_mom
    result = query_sales_mom(DateRange(start_date='2011-02-01',end_date='2011-03-01'), SalesFilters())
    assert result['rows'][0]['sales_amount'] == Decimal('447137.35')
    assert result['rows'][0]['sales_mom'] == Decimal('-122307.69') / Decimal('569445.04')


def test_real_n3_metrics_and_empty_average():
    with db.read_connection() as connection:
        params={'start_date':'2011-01-01','end_date':'2011-02-01'}
        def metric(code,**overrides):
            return query_sales(SalesQueryRequest(metric_code=code,**(params|overrides)),connection)['rows'][0]['value']
        assert metric('sales_quantity')==349147
        assert abs(metric('average_order_value')-Decimal('569445.04')/Decimal(987))<Decimal('1e-12')
        assert abs(metric('average_selling_price')-Decimal('569445.04')/Decimal(349147))<Decimal('1e-12')
        assert metric('average_order_value',start_date='2005-01-01',end_date='2005-02-01') is None
        assert metric('average_selling_price',start_date='2005-01-01',end_date='2005-02-01') is None


def test_real_n3_yoy_coverage_and_value():
    from backend.app.tools.yoy_tool import query_sales_yoy
    from backend.app.tools.schemas import DateRange,SalesFilters
    result=query_sales_yoy(DateRange(start_date='2011-01-01',end_date='2012-01-01'),SalesFilters())
    january=result['rows'][0]
    assert abs(january['sales_yoy']-(Decimal('569445.04')-Decimal('557319.06'))/Decimal('557319.06'))<Decimal('1e-20')
    assert result['rows'][-1]['sales_yoy'] is None
    assert result['rows'][-1]['null_reason']=='incomplete_period'


def test_real_n4_quality_and_historical_counts():
    from backend.app.tools.quality_tool import query_data_quality
    from backend.app.tools.schemas import DateRange
    report=query_data_quality(DateRange(start_date='2009-12-01',end_date='2012-01-01'))
    assert report['current']['blocking'] is False
    assert all(rule['count']==0 for rule in report['current']['rules'])
    assert report['historical_cleaning']['status']=='summary_matches'
    assert report['historical_cleaning']['counts']['cancelled_rows']==19494
    assert report['historical_cleaning']['counts']['invalid_rows']==261751
    assert report['months'][0]['coverage_status']=='within_bounds'
    assert report['months'][-1]['coverage_status']=='partial_or_outside'
    assert all(row['missing_rate'] is None for row in report['months'])


def test_real_n5_independent_moving_average_and_partial_month():
    from backend.app.tools.anomaly_tool import query_anomalies
    from backend.app.tools.schemas import AnomalyRequest
    request=AnomalyRequest(start_date='2011-02-01',end_date='2011-03-01')
    result=query_anomalies(request)
    checks=result['rows'][0]['checks']
    with db.read_connection() as connection:
        independent=connection.execute(text('''SELECT SUM(d.sales_amount)/3 AS baseline
            FROM fact_sales_detail d JOIN fact_sales_order o ON o.order_id=d.order_id
            WHERE o.order_status='completed' AND o.confirmed_date>='2010-11-01'
            AND o.confirmed_date<'2011-02-01' ''')).scalar_one()
    assert abs(checks[1]['baseline']-independent)<Decimal('1e-12')
    assert checks[0]['status']=='triggered' and checks[1]['status']=='triggered'
    assert checks[0]['change_ratio']==(Decimal('447137.35')-Decimal('569445.04'))/Decimal('569445.04')
    repeat=query_anomalies(request)
    assert repeat['data_version']==result['data_version']
    assert repeat['rows'][0]['checks'][0]['event_id']==checks[0]['event_id']
    partial=query_anomalies(AnomalyRequest(start_date='2011-12-01',end_date='2012-01-01'))
    assert all(check['status']=='unavailable' and check['reason']=='incomplete_period' for check in partial['rows'][0]['checks'])


def test_dashboard_independent_totals_rankings_and_drillthrough(tmp_path, monkeypatch):
    from datetime import date
    from backend.app.tools.dashboard_tool import query_dashboard
    from backend.app.agent.schemas import AnalysisIntent
    monkeypatch.setattr(settings, 'analysis_store', tmp_path / 'dashboard.sqlite3')
    result=query_dashboard(date(2011,2,1))
    cards={c['metric_code']:c for c in result['cards']}
    assert cards['sales_amount']['value']==Decimal('447137.35')
    assert cards['sales_amount']['previous']==Decimal('569445.04')
    with db.read_connection() as c:
        reference=c.execute(text("""SELECT c.customer_code AS dimension_key,SUM(d.sales_amount) AS value
            FROM fact_sales_detail d JOIN fact_sales_order o USING(order_id)
            JOIN dim_customer c ON c.customer_id=o.customer_id
            WHERE o.order_status='completed' AND o.confirmed_date>='2011-02-01' AND o.confirmed_date<'2011-03-01'
            GROUP BY c.customer_code ORDER BY value DESC,c.customer_code LIMIT 10""")).mappings().all()
    assert [(r['dimension_key'],r['value']) for r in result['rankings']['customer']]==[(r['dimension_key'],r['value']) for r in reference]
    assert all(result['rankings']['product'][i]['value']>=result['rankings']['product'][i+1]['value'] for i in range(9))
    alert=next(a for a in result['alerts'] if a['month']==date(2011,2,1))
    analysis=service.execute_intent(AnalysisIntent.model_validate(alert['intent']),'dashboard test',uuid4(),'structured')
    assert analysis['comparison']['current']==cards['sales_amount']['value']
    assert analysis['model_usage']['calls']==0
    assert query_dashboard()['month']==date(2011,11,1)
    first=query_dashboard(date(2009,12,1))
    assert all(card['change_ratio'] is None and card['null_reason']=='incomplete_baseline' for card in first['cards'])


def test_combination_contribution_and_continuous_drill(tmp_path, monkeypatch):
    from backend.app.agent.drill import drill_analysis
    from backend.app.agent.schemas import DrillRequest
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'n7.sqlite3')
    intent=parse_demo('为什么2011年2月销售额比1月下降').intent
    base=service.execute_intent(intent,'N7 base',uuid4(),'structured')
    paired=drill_analysis(base['id'],DrillRequest(contribution_dimensions=['customer_product']))
    a=paired['attributions'][0]
    assert a['dimension']=='customer_product' and a['reconciled'] and a['delta_sum']==Decimal('-122307.69')
    assert a['group_count']>10000  # covers all groups rather than original single-dimension cutoff
    with db.read_connection() as c:
        count=c.execute(text("""SELECT COUNT(*) FROM (SELECT o.customer_id,d.product_id
            FROM fact_sales_order o JOIN fact_sales_detail d USING(order_id)
            WHERE o.order_status='completed' AND o.confirmed_date>='2011-01-01' AND o.confirmed_date<'2011-03-01'
            GROUP BY o.customer_id,d.product_id) t""")).scalar_one()
    assert a['group_count']==count
    largest=a['negative'][0]
    child=drill_analysis(paired['id'],DrillRequest(filters=largest['drill_filters']))
    assert child['comparison']['previous']==largest['previous']
    assert child['comparison']['current']==largest['current']
    assert child['parent_analysis_id']==str(paired['id']) and child['session_id']==base['session_id']
    customer=drill_analysis(child['id'],DrillRequest(filters={'customer_code':largest['drill_filters']['customer_code']}))
    country=drill_analysis(customer['id'],DrillRequest(filters={**customer['intent']['filters'],'country':'United Kingdom'}))
    orders=drill_analysis(country['id'],DrillRequest(metric_code='order_count'))
    assert orders['model_usage']['calls']==0 and orders['attributions']==[]
    clear=drill_analysis(country['id'],DrillRequest(filters={}))
    assert clear['comparison']==base['comparison']


def test_strategies_real_accounting_and_budget(tmp_path,monkeypatch):
    from backend.app.agent.schemas import AnalysisIntent
    from backend.app.agent import store
    from uuid import UUID
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'n8.sqlite3')
    original=parse_demo('为什么2011年2月销售额比1月下降').intent
    for strategy,dimensions in (('sales_decline',['customer','product','country']),('customer_contribution',['customer']),('product_mix',['product'])):
        plan=AnalysisIntent.model_validate({**original.model_dump(),'strategy':strategy})
        result=service.execute_intent(plan,'N8',uuid4(),'structured')
        assert [a['dimension'] for a in result['attributions']]==dimensions
        assert result['comparison']['delta']==Decimal('-122307.69')
        run=store.get_run(UUID(result['run_id']))
        assert run['status']=='completed' and run['model_calls']==0 and run['retries']==0
        assert run['steps_used']==len(run['steps']) and run['analysis_id']==str(result['id'])
    plan=AnalysisIntent(action='trend',strategy='product_mix',period=original.period)
    mix=service.execute_intent(plan,'N8 structure',uuid4(),'structured')['product_structure']
    assert mix['reconciled'] and mix['total_sales']==Decimal('447137.35')
    assert mix['group_count']>500 and not mix['truncated']
    assert abs(sum(row['sales_share'] for row in mix['rows'])-Decimal(1))<Decimal('1e-24')
    monkeypatch.setattr(settings,'agent_max_steps',1)
    with pytest.raises(ServiceError) as error:
        service.execute_intent(original,'budget fail',uuid4(),'structured')
    failed=store.get_run(UUID(error.value.run_id))
    assert error.value.code=='step_budget_exceeded' and failed['steps_used']==1
    assert failed['analysis_id'] is None and failed['status']=='failed'


def test_monthly_report_real_snapshot_and_incomplete_baseline(tmp_path,monkeypatch):
    from backend.app.agent.reports import ReportRequest, generate_report
    from backend.app.agent import store
    from uuid import UUID
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'n9.sqlite3')
    report=generate_report(ReportRequest(month='2011-02-01'))
    snapshot=report['snapshot']
    assert snapshot['comparison']['delta']=='-122307.69'
    assert all(a['reconciled'] and Decimal(a['delta_sum'])==Decimal('-122307.69') for a in snapshot['attributions'])
    assert '447,137.35 GBP' in report['markdown'] and '-122,307.69 GBP' in report['markdown']
    assert store.get_business_report(UUID(report['id']))==report
    first=generate_report(ReportRequest(month='2009-12-01'))
    assert first['snapshot']['comparison'] is None and first['snapshot']['attributions']==[]
    assert '基期覆盖不足' in first['markdown'] and '不可计算' in first['markdown']
    with pytest.raises(ServiceError) as error:
        generate_report(ReportRequest(month='2011-12-01'))
    assert error.value.code=='incomplete_period'
    assert len(store.business_reports())==2


def test_complete_business_workflow_api(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app.main import app
    from backend.app.agent import store
    import sqlite3
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'n10.sqlite3')
    client=TestClient(app)
    dashboard=client.get('/api/dashboard?month=2011-02-01').json()
    alert=next(row for row in dashboard['alerts'] if row['month']=='2011-02-01')
    base=client.post('/api/analyze/structured',json=alert['intent']).json()
    paired=client.post(f"/api/analyses/{base['id']}/drill",json={'contribution_dimensions':['customer','product','country','customer_product']}).json()
    row=next(a for a in paired['attributions'] if a['dimension']=='customer_product')['negative'][0]
    child=client.post(f"/api/analyses/{paired['id']}/drill",json={'filters':row['drill_filters']}).json()
    feedback={'rating':'helpful','reason':'闭环验收：金额和筛选可复核','correction':''}
    assert client.put(f"/api/analyses/{child['id']}/feedback",json=feedback).status_code==200
    reopened=client.get(f"/api/analyses/{child['id']}").json()
    assert reopened['feedback']['rating']=='helpful'
    assert reopened['intent']['filters']['customer_code']==row['drill_filters']['customer_code']
    assert reopened['parent_analysis_id']==paired['id']
    assert reopened['comparison']['delta']==row['delta']
    assert client.get(f"/api/analyses/{child['id']}/export/json").json()==reopened
    report=client.post('/api/reports',json={'month':'2011-02-01'}).json()
    assert report['snapshot']['comparison']==base['comparison']
    assert client.get(f"/api/reports/{report['id']}/export/md").text==report['markdown']
    assert client.get(f"/api/reports/{report['id']}").json()==report
    assert base['model_usage']['calls']==child['model_usage']['calls']==report['execution']['model_calls']==0
    # Analysis save failure must stop before success response; failure details remain public-safe.
    monkeypatch.setattr(store,'save',lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('private disk detail')))
    failure=client.post('/api/analyze/structured',json=alert['intent'])
    assert failure.status_code==503 and failure.json()['error']['code']=='storage_unavailable'
    assert 'private disk detail' not in failure.text
    assert len(client.get('/api/analyses').json())==3


def test_real_registered_catalog(tmp_path,monkeypatch):
    from backend.app.data_management import catalog
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'catalog.sqlite3')
    scanned=catalog.scan_registered()
    assert scanned['asset_count']==5
    assets={a['table_name']:a for a in scanned['assets']}
    assert assets['fact_sales_detail']['row_count']==805620
    assert assets['fact_sales_order']['row_count']==36975
    assert any(c['type']=='f' for c in assets['fact_sales_detail']['constraints'])
    field=next(f for f in assets['fact_sales_detail']['columns'] if f['column_name']=='gross_profit')
    assert field['availability']=='unavailable'
    catalog.update_description('fact_sales_detail','sales_amount',catalog.DescriptionUpdate(description='有效订单明细销售额，GBP',expected_revision=0))
    catalog.scan_registered()
    field=next(f for f in catalog.detail('fact_sales_detail')['columns'] if f['column_name']=='sales_amount')
    assert field['human']['description']=='有效订单明细销售额，GBP'


def test_real_quality_management(tmp_path,monkeypatch):
    from backend.app.data_management import quality
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'quality-management.sqlite3')
    first=quality.run_check()
    assert first['status']=='completed' and not first['current']['blocking']
    assert first['current']['summary']['details']==805620
    assert len(first['current']['rules'])==8
    assert all(rule['count']==0 and rule['samples']==[] for rule in first['current']['rules'])
    with db.read_connection() as connection:
        for code in quality.RULES:
            assert quality.samples(connection,code,5)==[]
    second=quality.run_check()
    comparison=quality.compare(first['id'],second['id'])
    assert comparison['same_config'] and all(row['delta']==0 for row in comparison['rules'])


def test_isolated_import_publish_select_and_rollback(tmp_path,monkeypatch):
    from backend.app.data_management import batches,versions
    from backend.app.tools import quality_tool
    from backend.app.agent.schemas import AnalysisIntent
    from backend.app.tools.schemas import DateRange
    from fastapi.testclient import TestClient
    from backend.app.main import app
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'imports.sqlite3')
    monkeypatch.setattr(batches,'RAW',tmp_path/'raw');batches.RAW.mkdir()
    monkeypatch.setattr(batches,'WORK',tmp_path/'work')
    source=batches.RAW/'sample.csv'
    source.write_text('Invoice,StockCode,Description,Quantity,InvoiceDate,Price,Customer ID,Country\nA,P,Product,1,2011-01-01,2,1,UK\nB,P,Product,2,2011-02-28,2,1,UK\n')
    first=batches.register(batches.Register(source_file='sample.csv'))['batch'];schema=first['schema_name']
    try:
        batches.worker(first['id'])
        published=batches.get_batch(first['id'])
        assert published['status']=='published', published
        batches.worker(first['id'])
        assert batches.get_batch(first['id'])['attempts']==1
        token=versions.REQUEST_VERSION.set(first['id'])
        try:
            with db.read_connection() as connection:
                assert connection.execute(text('SELECT COUNT(*) FROM fact_sales_detail')).scalar_one()==2
            intent=AnalysisIntent(action='trend',metric_code='sales_amount',period=DateRange(start_date='2011-01-01',end_date='2011-03-01'),group_by='month')
            result=service.execute_intent(intent,'import test',uuid4(),'structured')
            assert result['dataset_source']['version_id']==first['id']
            assert [row['value'] for row in result['data']]==[Decimal('2'),Decimal('4')]
            from backend.app.agent.reports import generate_report,ReportRequest
            report=generate_report(ReportRequest(month='2011-02-01'))
            assert report['snapshot']['dataset_source']['version_id']==first['id']
        finally:versions.REQUEST_VERSION.reset(token)
        client=TestClient(app)
        assert client.post('/api/analyses/'+str(result['id'])+'/drill',json={}).status_code==409
        with db.read_connection() as connection:
            assert connection.execute(text('SELECT COUNT(*) FROM fact_sales_detail')).scalar_one()==805620
        # New file/version; fail inside PG transaction after tables are created.
        source.write_text(source.read_text().replace('Product','Other'))
        second=batches.register(batches.Register(source_file='sample.csv'))['batch']
        def fail(_):raise ValueError('test rollback')
        monkeypatch.setattr(quality_tool,'current_quality',fail)
        batches.worker(second['id'])
        assert batches.get_batch(second['id'])['status']=='failed'
        with db.engine.connect() as connection:
            assert connection.execute(text('SELECT COUNT(*) FROM pg_namespace WHERE nspname=:name'),{'name':second['schema_name']}).scalar_one()==0
    finally:
        # Only the schema constructed by this isolated test is removed.
        assert schema.startswith('ia_import_')
        with db.engine.begin() as connection:connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_metric_version_real_analysis_and_report(tmp_path,monkeypatch):
    from backend.app.data_management import metric_versions as registry
    from backend.app.agent import store
    from backend.app.agent.reports import generate_report, ReportRequest, get_report
    from datetime import date
    from backend.app.agent.schemas import AnalysisIntent
    intent=AnalysisIntent(action='trend',metric_code='sales_amount',period={'start_date':'2011-02-01','end_date':'2011-03-01'},group_by='month')
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'metric-real.sqlite3')
    before=registry.listing()['metrics'][0]
    old=service.execute_intent(intent,'version test',uuid4(),'demo')
    old_report=generate_report(ReportRequest(month=date(2011,2,1)))
    draft=registry.save_draft('sales_amount',registry.Draft(expected_revision=before['revision'],name='有效销售额',description='已完成订单的收入'))
    registry.publish('sales_amount',registry.Publish(expected_revision=draft['revision']))
    new=service.execute_intent(intent,'version test',uuid4(),'demo')
    assert old['metric_definitions']['sales_amount']['version']==1
    assert new['metric_definitions']['sales_amount']['version']==2
    assert old['data']==new['data'] and new['data'][0]['value']==Decimal('447137.35')
    assert store.get(old['id'])['metric_definitions']==old['metric_definitions']
    reopened=get_report(old_report['id'])
    assert reopened['snapshot_sha256']==old_report['snapshot_sha256']
    assert reopened['snapshot']['metric_definitions']['sales_amount']['version']==1
    assert reopened['markdown']==old_report['markdown']
    with pytest.raises(ServiceError) as exc:
        service.execute_intent(intent,'drill',uuid4(),'demo',parent_id=old['id'])
    assert exc.value.code=='metric_context_conflict'


def test_real_lineage_saved_version_and_publication(tmp_path,monkeypatch):
    from backend.app.data_management import lineage, metric_versions as registry
    from backend.app.agent.schemas import AnalysisIntent
    from backend.app.agent.reports import generate_report, ReportRequest
    from backend.app.agent import store
    from datetime import date
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'lineage-real.sqlite3')
    intent=AnalysisIntent(action='trend',metric_code='sales_amount',period={'start_date':'2011-02-01','end_date':'2011-03-01'},group_by='month')
    analysis=service.execute_intent(intent,'lineage real',uuid4(),'structured')
    report=generate_report(ReportRequest(month=date(2011,2,1)))
    before=store.get_business_report(report['id'])
    first=lineage.trace('report:'+report['id'],'upstream')
    ids={n['id'] for n in first['nodes']}
    assert 'dataset:baseline-v1' in ids and 'unknown:baseline-source' in ids
    assert 'table:baseline-v1:fact_sales_detail' in ids
    assert 'binding:baseline-v1:sales_amount:v1' in ids
    assert not any(n['kind']=='source' for n in first['nodes'])
    draft=registry.save_draft('sales_amount',registry.Draft(expected_revision=1,name='有效销售额',description='新版本说明'))
    registry.publish('sales_amount',registry.Publish(expected_revision=draft['revision']))
    assert lineage.trace('report:'+report['id'],'upstream')==first
    impacts=lineage.trace('table:baseline-v1:fact_sales_detail','downstream')
    ids={n['id'] for n in impacts['nodes']}
    assert 'analysis:'+str(analysis['id']) in ids and 'report:'+report['id'] in ids
    assert store.get_business_report(report['id'])==before


def test_quality_issue_real_version_recheck(tmp_path, monkeypatch):
    """Dirty and repair only this test-created import schema; public stays untouched."""
    from backend.app.data_management import batches, quality, issues, versions
    monkeypatch.setattr(settings, 'analysis_store', tmp_path/'issues.sqlite3')
    monkeypatch.setattr(batches, 'RAW', tmp_path/'raw'); batches.RAW.mkdir()
    monkeypatch.setattr(batches, 'WORK', tmp_path/'work')
    (batches.RAW/'issue-test.csv').write_text('Invoice,StockCode,Description,Quantity,InvoiceDate,Price,Customer ID,Country\nA,P,Product,1,2011-01-01,2,1,UK\n')
    batch=batches.register(batches.Register(source_file='issue-test.csv'))['batch']
    schema=batch['schema_name']
    token=None
    try:
        batches.worker(batch['id'])
        assert batches.get_batch(batch['id'])['status']=='published', batches.get_batch(batch['id'])
        token=versions.REQUEST_VERSION.set(batch['id'])
        with db.engine.begin() as connection:
            connection.exec_driver_sql(f"INSERT INTO \"{schema}\".dim_date(date_id) VALUES ('2099-01-01') ON CONFLICT DO NOTHING")
            connection.exec_driver_sql(f'UPDATE "{schema}".fact_sales_order SET confirmed_date=\'2099-01-01\'')
        check=quality.run_check()
        rule=next(rule for rule in check['current']['rules'] if rule['rule']=='invalid_date')
        assert rule['count']==1 and len(rule['samples'])==1
        issue=issues.listing()['issues'][0]
        assert issue['version_id']==batch['id'] and issue['rule_code']=='invalid_date'
        failed=issues.recheck(UUID(issue['id']), issues.Recheck(expected_revision=issue['revision']))
        assert not failed['candidate']['passed']
        with pytest.raises(ServiceError):
            issues.act(UUID(issue['id']), issues.Action(expected_revision=failed['revision'],action='close',note='不能关闭'))
        with db.engine.begin() as connection:
            connection.exec_driver_sql(f'UPDATE "{schema}".fact_sales_order SET confirmed_date=order_date')
        passed=issues.recheck(UUID(issue['id']), issues.Recheck(expected_revision=failed['revision']))
        assert passed['candidate']['passed']
        closed=issues.act(UUID(issue['id']),issues.Action(expected_revision=passed['revision'],action='close',note='测试隔离版本修复日期，复检通过'))
        assert closed['status']=='resolved'
    finally:
        if token is not None: versions.REQUEST_VERSION.reset(token)
        assert schema.startswith('ia_import_')
        with db.engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    with db.read_connection() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM fact_sales_detail')).scalar_one()==805620
