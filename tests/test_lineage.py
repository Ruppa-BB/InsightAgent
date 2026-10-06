import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from backend.app.agent import store
from backend.app.config import settings
from backend.app.data_management import lineage, metric_versions
from backend.app.data_management.batches import initialize_batches
from backend.app.main import app

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'lineage.sqlite3')
    return TestClient(app)

def seed_versions():
    with store.connect() as db:
        initialize_batches(db);metric_versions.initialize(db)
        for index in (1,2):
            batch=f'b{index}';version=f'd{index}'
            record={'id':batch,'fingerprint':str(index)*64,'created_at':'2026-10-06','source_file':f'sample{index}.csv','status':'published','cleaning_version':'retail-clean-v2'}
            db.execute('INSERT INTO dm_import_batches VALUES (?,?,?,?,?)',(batch,record['fingerprint'],record['created_at'],record['status'],json.dumps(record)))
            db.execute('INSERT INTO dm_dataset_versions VALUES (?,?,?,?,?)',(version,batch,'ia_import_'+str(index)*32,'2026-10-06',json.dumps(record)))
        metric=json.loads(db.execute("SELECT payload FROM dm_metric_versions WHERE code='sales_amount' AND version=1").fetchone()[0])
        for index in (1,2):
            record={'id':f'a{index}','session_id':'session','created_at':'2026-10-06','question':f'version{index}',
                    'dataset_source':{'version_id':f'd{index}'},'metric_definitions':{'sales_amount':metric}}
            db.execute('INSERT INTO analyses VALUES (?,?,?,?)',(record['id'],record['session_id'],record['created_at'],json.dumps(record)))
        report={'id':'r1','title':'历史月报','created_at':'2026-10-06','month':'2011-02-01','snapshot':{'dataset_source':{'version_id':'d1'},'metric_definitions':{'sales_amount':metric}},'snapshot_sha256':'unchanged','markdown':'immutable evidence'}
        db.execute('INSERT INTO business_reports VALUES (?,?,?,?)',('r1',report['created_at'],report['month'],json.dumps(report)))
    return report

def trace(client,ident,direction='upstream'):
    return client.get('/api/data/lineage/trace',params={'node_id':ident,'direction':direction})

def test_versions_do_not_merge_and_history_immutable(client):
    report=seed_versions()
    data=trace(client,'report:r1').json();ids={n['id'] for n in data['nodes']}
    assert 'source:'+'1'*64 in ids and 'batch:b1' in ids
    assert 'dataset:d1' in ids and 'table:d1:fact_sales_detail' in ids
    assert 'binding:d1:sales_amount:v1' in ids and 'metric:sales_amount:v1' in ids
    assert not any('d2' in ident or ident=='batch:b2' for ident in ids)
    downstream=trace(client,'table:d1:fact_sales_detail','downstream').json()
    ids={n['id'] for n in downstream['nodes']}
    assert 'analysis:a1' in ids and 'report:r1' in ids and 'analysis:a2' not in ids
    assert store.get_business_report('r1')==report

def test_publication_preserves_saved_binding(client):
    seed_versions();lineage.graph()
    saved=metric_versions.save_draft('sales_amount',metric_versions.Draft(expected_revision=1,name='有效销售额',description='新的说明'))
    metric_versions.publish('sales_amount',metric_versions.Publish(expected_revision=saved['revision']))
    ids={n['id'] for n in trace(client,'report:r1').json()['nodes']}
    assert 'metric:sales_amount:v1' in ids and 'metric:sales_amount:v2' not in ids
    assert 'binding:d1:sales_amount:v1' in ids and 'binding:d1:sales_amount:v2' not in ids
    assert any(n['id']=='metric:sales_amount:v2' for n in lineage.graph()['nodes'])

def test_unknown_legacy_not_rebound_to_latest(client):
    seed_versions()
    with store.connect() as db:
        db.execute('INSERT INTO analyses VALUES (?,?,?,?)',('legacy','s','now',json.dumps({'question':'old'})))
    data=trace(client,'analysis:legacy').json()
    assert data['unknown_count']==2
    assert {e['evidence'] for e in data['edges']}=={'unknown'}
    assert not any(n['kind'] in ('metric','dataset','source') for n in data['nodes'])
    baseline=trace(client,'dataset:baseline-v1').json()
    assert baseline['unknown_count']==1
    assert baseline['nodes'][1]['id'] in ('dataset:baseline-v1','unknown:baseline-source')

def test_failed_batch_not_published_and_graph_idempotent(client):
    seed_versions()
    with store.connect() as db:
        record={'id':'failed','fingerprint':'f'*64,'created_at':'now','source_file':'bad.csv','status':'failed'}
        db.execute('INSERT INTO dm_import_batches VALUES (?,?,?,?,?)',('failed','f'*64,'now','failed',json.dumps(record)))
    first=lineage.graph();second=lineage.graph()
    assert first==second
    data=trace(client,'batch:failed','downstream').json()
    assert not any(n['kind'] in ('dataset','table','analysis','report') for n in data['nodes'])
    assert trace(client,'absent').status_code==404
    assert trace(client,'batch:failed','wrong').status_code==422
    assert client.get('/data/lineage').status_code==200

def test_definition_downstream_is_potential_not_fabricated_usage(client):
    seed_versions()
    data=trace(client,'metric:order_count:v1','downstream').json()
    assert any(n['kind']=='binding' for n in data['nodes'])
    assert not any(n['kind'] in ('analysis','report') for n in data['nodes'])
    # All base SQL joins the dimensions, including order_count without group_by.
    upstream=trace(client,'binding:d1:order_count:v1').json()
    tables={n['table'] for n in upstream['nodes'] if n['kind']=='table'}
    assert tables==set(lineage.TABLES)
    assert all(e['evidence']=='declared' for e in upstream['edges'] if e['relation']=='query_dependency')


def test_lineage_failure_rolls_back_publication_and_analysis_save(client,monkeypatch):
    seed_versions()
    saved=metric_versions.save_draft('sales_amount',metric_versions.Draft(expected_revision=1,name='新版',description='待发布'))
    def fail(_):raise RuntimeError('lineage unavailable')
    monkeypatch.setattr(lineage,'synchronize',fail)
    with pytest.raises(RuntimeError):metric_versions.publish('sales_amount',metric_versions.Publish(expected_revision=saved['revision']))
    with store.connect() as db:
        assert db.execute("SELECT version,draft FROM dm_metric_heads WHERE code='sales_amount'").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM dm_metric_versions WHERE code='sales_amount'").fetchone()[0]==1
    ident=uuid4()
    with pytest.raises(RuntimeError):store.save({'id':ident,'session_id':uuid4(),'created_at':'now','question':'rollback'})
    assert store.get(ident) is None
