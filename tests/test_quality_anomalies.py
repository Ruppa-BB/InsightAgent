from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from backend.app.agent import service
from backend.app.agent.provider import parse_demo
from backend.app.errors import ServiceError
from backend.app.main import app
from backend.app.tools.anomaly_tool import detect_months
from backend.app.tools.mom_tool import calculate_monthly_mom
from backend.app.tools.quality_tool import monthly_coverage,read_import_history
from backend.app.tools.schemas import AnomalyRequest,DateRange

COVERAGE={'start_date':date(2010,1,1),'last_date':date(2011,12,9)}


def test_quality_months_distinguish_empty_bounds_and_unknown_missing_rate():
    rows=monthly_coverage(DateRange(start_date='2011-11-01',end_date='2012-02-01'),COVERAGE,
        [{'month':date(2011,11,1),'sales_amount':Decimal(10),'detail_count':2,'observation_days':1},
         {'month':date(2011,12,1),'sales_amount':Decimal(5),'detail_count':1,'observation_days':1}])
    assert rows[0]['eligible_for_detection'] and rows[0]['observation_days']==1
    assert not rows[1]['eligible_for_detection'] and rows[1]['has_records']
    assert rows[2]['empty'] and rows[2]['sales_amount'] is None
    assert all(r['missing_rate'] is None for r in rows)


def test_import_report_separate_overlap_missing_and_mismatch(tmp_path):
    path=tmp_path/'report.json'
    stats={'details':8,'orders':2,'sales_amount':Decimal(10)}
    assert read_import_history(stats,path)['status']=='unavailable'
    report={'raw_rows':10,'clean_rows':8,'cancelled_rows':1,'invalid_rows':2,
            'orders':2,'customers':2,'products':3,'regions':1,'sales_amount':10}
    path.write_text(json.dumps(report))
    parsed=read_import_history(stats,path)
    assert parsed['status']=='summary_matches' and parsed['counts']['invalid_rows']==2
    assert read_import_history(stats|{'details':7},path)['status']=='summary_mismatch'
    path.write_text('{broken')
    assert read_import_history(stats,path)['status']=='invalid_report'


def test_threshold_strict_boundary_and_no_future_leakage():
    request=AnomalyRequest(start_date='2011-04-01',end_date='2011-05-01')
    values={date(2011,m,1):Decimal(100) for m in (1,2,3)}|{date(2011,4,1):Decimal(80),date(2011,5,1):Decimal(999999)}
    rows=detect_months(request,values,COVERAGE)
    assert all(check['status']=='normal' for check in rows[0]['checks'])
    assert rows[0]['checks'][1]['baseline']==100
    values[date(2011,4,1)]=Decimal(79)
    rows=detect_months(request,values,COVERAGE)
    assert all(check['status']=='triggered' for check in rows[0]['checks'])
    assert all(date(2011,5,1) not in check['baseline_months'] for check in rows[0]['checks'])


def test_detection_missing_history_zero_and_partial_states():
    request=AnomalyRequest(start_date='2011-04-01',end_date='2011-05-01')
    values={date(2011,4,1):Decimal(40),date(2011,3,1):Decimal(0)}
    checks=detect_months(request,values,COVERAGE)[0]['checks']
    assert checks[0]['reason']=='zero_baseline'
    assert checks[1]['reason']=='missing_baseline'
    checks=detect_months(request,values,COVERAGE,True)[0]['checks']
    assert all(check['status']=='blocked' and check['change_ratio'] is None for check in checks)
    request=AnomalyRequest(start_date='2010-01-01',end_date='2010-02-01')
    checks=detect_months(request,{date(2010,1,1):Decimal(5)},COVERAGE)[0]['checks']
    assert checks[1]['reason']=='insufficient_history'
    request=AnomalyRequest(start_date='2011-12-01',end_date='2012-01-01')
    assert all(check['reason']=='incomplete_period' for check in detect_months(request,{date(2011,12,1):Decimal(3)},COVERAGE)[0]['checks'])


def test_moving_average_can_alert_growth_and_window_is_consecutive():
    request=AnomalyRequest(start_date='2011-04-01',end_date='2011-05-01',moving_average_window=2)
    values={date(2011,2,1):Decimal(100),date(2011,3,1):Decimal(100),date(2011,4,1):Decimal(130)}
    checks=detect_months(request,values,COVERAGE)[0]['checks']
    assert checks[0]['status']=='normal' and checks[1]['status']=='triggered'
    assert checks[1]['baseline_months']==[date(2011,2,1),date(2011,3,1)]


def test_mom_now_excludes_partial_month():
    rows=calculate_monthly_mom(DateRange(start_date='2011-12-01',end_date='2012-01-01'),
        {date(2011,11,1):Decimal(10),date(2011,12,1):Decimal(4)},COVERAGE)
    assert rows[0]['sales_mom'] is None and rows[0]['null_reason']=='incomplete_period'


@pytest.mark.parametrize('overrides',[{'moving_average_window':1},{'moving_average_window':True},
    {'mom_drop_threshold':0},{'moving_average_threshold':float('inf')},{'start_date':'2011-01-02'},
    {'filters':{'sql':'DROP TABLE x'}}])
def test_invalid_detection_settings_rejected(overrides):
    with pytest.raises(ValueError):
        AnomalyRequest(**({'start_date':'2011-01-01','end_date':'2012-01-01'}|overrides))


def test_bad_quality_blocks_analysis_before_any_query(monkeypatch):
    @contextmanager
    def connection(): yield object()
    monkeypatch.setattr(service,'read_connection',connection)
    monkeypatch.setattr(service,'current_quality',lambda conn:{'blocking':True})
    monkeypatch.setattr(service,'query_sales',lambda *args:pytest.fail('query must not run'))
    with pytest.raises(ServiceError) as exc:
        service.execute_intent(parse_demo('2011年销售额趋势').intent,'quality',uuid4(),'structured')
    assert exc.value.code=='data_quality_failed'


def test_quality_and_detection_api_serialize_and_validate(monkeypatch):
    monkeypatch.setattr('backend.app.main.query_data_quality',lambda period:{'missing_rate':None})
    monkeypatch.setattr('backend.app.main.query_anomalies',lambda request:{'ratio':Decimal('.2')})
    client=TestClient(app)
    payload={'start_date':'2011-01-01','end_date':'2012-01-01'}
    assert client.post('/api/data-quality',json=payload).json()=={'missing_rate':None}
    assert client.post('/api/anomalies',json=payload).json()=={'ratio':'0.2'}
    assert client.post('/api/anomalies',json=payload|{'moving_average_window':50}).status_code==422


def test_monitor_saves_same_snapshot_for_export_and_unknown_rejected(tmp_path,monkeypatch):
    from backend.app.config import settings
    from backend.app.main import monitor
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'reports.sqlite3')
    @contextmanager
    def connection(): yield object()
    monkeypatch.setattr('backend.app.main.read_connection',connection)
    quality={'current':{'data_version':'fixture'}}
    monkeypatch.setattr('backend.app.main.query_data_quality',lambda period,conn:quality)
    def anomalies(request,conn,current):
        assert current is quality['current']
        return {'data_version':'version1','baseline':Decimal('100')}
    monkeypatch.setattr('backend.app.main.query_anomalies',anomalies)
    client=TestClient(app)
    result=client.post('/api/monitor',json={'start_date':'2011-01-01','end_date':'2011-02-01'})
    assert result.status_code==200
    report=result.json()
    export=client.get(f"/api/monitor/{report['id']}/export")
    assert export.json()==report
    assert 'attachment;' in export.headers['content-disposition']
    assert client.get(f'/api/monitor/{uuid4()}/export').status_code==404
