"""Published source selection is per request; baseline remains the default."""
from contextvars import ContextVar
import json
import re
from uuid import UUID

from fastapi import APIRouter

from backend.app.agent.store import connect
from backend.app.errors import ServiceError

REQUEST_VERSION = ContextVar('request_dataset_version',default='baseline-v1')
router=APIRouter(prefix='/api/data',tags=['数据版本'])


def source_info() -> dict:
    ident=REQUEST_VERSION.get()
    if ident=='baseline-v1':return {'dataset_id':'online-retail-ii','version_id':ident,'schema':'public','batch_id':None,'provenance':'legacy_summary_only'}
    try: UUID(ident)
    except ValueError:raise ServiceError('invalid_dataset_version','数据版本不合法。',422)
    with connect() as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dm_dataset_versions'").fetchone()
        row=db.execute('SELECT schema_name,payload FROM dm_dataset_versions WHERE id=?',(ident,)).fetchone() if exists else None
    if not row:raise ServiceError('dataset_not_published','数据版本不存在或尚未发布。',409)
    schema,raw=row
    if not re.fullmatch('ia_import_[0-9a-f]{32}',schema):raise ServiceError('invalid_dataset_version','版本登记无效。',409)
    record=json.loads(raw)
    return {'dataset_id':'online-retail-ii','version_id':ident,'schema':schema,'batch_id':ident,
            'source_sha256':record['fingerprint'],'provenance':'managed_import'}


@router.get('/versions')
def versions():
    from backend.app.data_management.batches import initialize_batches
    with connect() as db:
        initialize_batches(db)
        rows=db.execute('SELECT id,payload FROM dm_dataset_versions ORDER BY created_at DESC').fetchall()
    return {'versions':[{'id':'baseline-v1','label':'原有零售基线','status':'published'}]+[
        {'id':ident,'label':json.loads(raw)['source_file']+' · '+ident[:8], 'status':'published'} for ident,raw in rows]}
