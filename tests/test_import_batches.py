from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.data_management import batches, versions
from backend.app.main import app


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'analysis_store',tmp_path/'batches.sqlite3')
    monkeypatch.setattr(batches,'RAW',tmp_path/'raw');batches.RAW.mkdir()
    monkeypatch.setattr(batches,'WORK',tmp_path/'work')
    source=batches.RAW/'valid.csv';source.write_text('Invoice,StockCode,Description,Quantity,InvoiceDate,Price,Customer ID,Country\nA,P,Product,1,2011-01-01,2,1,UK\n')
    return TestClient(app),source


def test_registration_fingerprint_and_source_copy(setup):
    client,source=setup
    first=client.post('/api/data/imports',json={'source_file':source.name}).json()
    second=client.post('/api/data/imports',json={'source_file':source.name}).json()
    assert second['duplicate'] and second['batch']['id']==first['batch']['id']
    ident=first['batch']['id']
    source.write_text('modified')
    assert batches.fingerprint(batches.WORK/ident/'source.csv')==first['batch']['fingerprint']
    assert client.post('/api/data/imports',json={'source_file':'../secret.csv'}).status_code==422
    assert client.get('/api/data/imports/'+str(uuid4())).status_code==404
    assert len(client.get('/api/data/imports').json()['batches'])==1
    token=versions.REQUEST_VERSION.set(ident)
    try:
        with pytest.raises(Exception) as error:versions.source_info()
        assert error.value.code=='dataset_not_published'
    finally:versions.REQUEST_VERSION.reset(token)


def test_queue_duplicate_worker_and_safe_start_failure(setup,monkeypatch):
    client,source=setup;ident=client.post('/api/data/imports',json={'source_file':source.name}).json()['batch']['id']
    monkeypatch.setattr(batches.subprocess,'Popen',lambda *args,**kwargs:None)
    assert client.post('/api/data/imports/'+ident+'/run').json()['status']=='queued'
    assert client.post('/api/data/imports/'+ident+'/run').status_code==409
    record=batches.get_batch(ident);record['status']='failed';batches.save_batch(record)
    def fail(*args,**kwargs):raise OSError('sensitive diagnostics')
    monkeypatch.setattr(batches.subprocess,'Popen',fail)
    response=client.post('/api/data/imports/'+ident+'/run')
    assert response.status_code==503 and 'sensitive' not in response.text
    assert batches.get_batch(ident)['status']=='failed'


def test_source_tampering_never_publishes(setup):
    client,source=setup;ident=client.post('/api/data/imports',json={'source_file':source.name}).json()['batch']['id']
    (batches.WORK/ident/'source.csv').write_text('tampered')
    batches.worker(ident)
    assert batches.get_batch(ident)['status']=='failed'
    assert len(versions.versions()['versions'])==1


def test_cleaner_rejects_fractional_nonfinite_and_blank():
    import importlib.util
    import pandas as pd
    from backend.app.config import ROOT
    spec=importlib.util.spec_from_file_location('clean_test',ROOT/'data/scripts/import_online_retail.py')
    cleaner=importlib.util.module_from_spec(spec);spec.loader.exec_module(cleaner)
    base={'Invoice':'A','StockCode':'P','Description':'Product','Quantity':1,'InvoiceDate':'2011-01-01','Price':2.125,'Customer ID':1,'Country':'UK'}
    raw=pd.DataFrame([base,{**base,'Quantity':1.5},{**base,'Price':float('inf')},{**base,'Country':''},{**base,'Invoice':'C1'}])
    clean,stats=cleaner.clean_source(raw)
    assert stats['raw_rows']==5 and stats['clean_rows']==1 and stats['invalid_rows']==4
    assert stats['cancelled_rows']==1 and stats['sales_amount']=='2.13'


def test_interrupted_worker_requires_review_not_duplicate_write(setup,monkeypatch):
    client,source=setup
    record=client.post('/api/data/imports',json={'source_file':source.name}).json()['batch']
    record.update(status='running',worker_pid=999999)
    batches.save_batch(record)
    def dead(pid,signal):raise ProcessLookupError()
    monkeypatch.setattr(batches.os,'kill',dead)
    recovered=client.get('/api/data/imports/'+record['id']).json()
    assert recovered['status']=='recovery_required'
    assert client.post('/api/data/imports/'+record['id']+'/run').status_code==409
    assert len(versions.versions()['versions'])==1
