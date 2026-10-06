from datetime import date, timedelta
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from backend.app.agent import store
from backend.app.agent.reports import ReportRequest, generate_report, get_report
from backend.app.agent.schemas import AnalysisFeedback, AnalysisIntent, AnalyzeRequest, DrillRequest
from backend.app.agent.service import analyze, execute_intent
from backend.app.config import settings
from backend.app.db import read_connection
from backend.app.errors import ServiceError
from backend.app.semantic.metrics import METRICS
from backend.app.serialization import json_ready
from backend.app.tools.analysis_tool import query_coverage
from backend.app.tools.schemas import DateRange, SalesFilters, SalesQueryRequest, SalesYoYRequest, AnomalyRequest
from backend.app.tools.mom_tool import query_sales_mom
from backend.app.tools.yoy_tool import query_sales_yoy
from backend.app.tools.quality_tool import query_data_quality
from backend.app.tools.anomaly_tool import query_anomalies
from backend.app.tools.sql_allowlist import query_sales

from backend.app.data_management.catalog import router as data_router
from backend.app.data_management.quality import router as quality_router
from backend.app.data_management.batches import router as batches_router
from backend.app.data_management.versions import router as versions_router, REQUEST_VERSION

from backend.app.data_management.metric_versions import router as metric_router, definitions

from backend.app.data_management.lineage import router as lineage_router

app = FastAPI(title='InsightAgent API', version='0.2.0')
app.include_router(data_router)
app.include_router(quality_router)
from backend.app.data_management.issues import router as issues_router
app.include_router(issues_router)
app.include_router(batches_router)
app.include_router(versions_router)
app.include_router(metric_router)
app.include_router(lineage_router)
STATIC = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.exception_handler(ServiceError)
def service_error(request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={'error': {'code': exc.code, 'message': exc.message}, **({'run_id':exc.run_id} if hasattr(exc,'run_id') else {})})


@app.exception_handler(ValueError)
def input_error(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=422, content={'error': {'code': 'invalid_query', 'message': str(exc)}})


@app.exception_handler(sqlite3.Error)
def storage_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
    return JSONResponse(status_code=503, content={'error': {'code': 'storage_unavailable', 'message': '分析记录存储不可用，请检查本地目录权限和空间。'}})


@app.middleware('http')
async def selected_dataset(request: Request, call_next):
    token=REQUEST_VERSION.set(request.headers.get('X-Dataset-Version','baseline-v1'))
    try:
        return await call_next(request)
    finally:
        REQUEST_VERSION.reset(token)


@app.get('/')
def home() -> FileResponse:
    return FileResponse(STATIC / 'index.html')


@app.get('/data')
def data_home() -> FileResponse:
    return FileResponse(STATIC / 'data.html')


@app.get('/data/imports')
def import_home() -> FileResponse:
    return FileResponse(STATIC / 'imports.html')


@app.get('/data/lineage')
def lineage_home() -> FileResponse:
    return FileResponse(STATIC / 'lineage.html')


@app.get('/data/metrics')
def metric_home() -> FileResponse:
    return FileResponse(STATIC / 'metrics.html')


@app.get('/data/quality')
def quality_home() -> FileResponse:
    return FileResponse(STATIC / 'quality.html')


@app.get('/health')
def health_check() -> dict:
    return {'status': 'ok', 'mode': settings.llm_provider, 'model': settings.llm_model,
            'max_output_tokens': settings.llm_max_output_tokens,
            'llm_configured': bool(settings.llm_api_key and settings.llm_api_key.get_secret_value() and settings.llm_model)}


@app.get('/health/db')
def database_health() -> dict:
    with read_connection() as connection:
        connection.execute(text('SELECT 1'))
    return {'database': 'ok'}


@app.get('/api/metrics')
def metrics() -> dict:
    return {'metrics': {k:v for k,v in definitions().items() if v['enabled']}, 'supported_queries': ['sales_amount', 'order_count', 'customer_count','sales_quantity','average_order_value','average_selling_price'],
            'derived_metrics': {'sales_yoy':'/tools/sales-yoy 或 action=yoy，缺少完整同期数据时返回null。', 'sales_mom': '比较相邻完整月份的销售额，结果 comparison.change_ratio；基期为 0 时 null。'}}


@app.get('/api/metadata')
def metadata() -> JSONResponse:
    with read_connection() as connection:
        coverage = query_coverage(connection)
    return JSONResponse(json_ready({'source': 'Online Retail II', 'unit': 'GBP', 'coverage': coverage,
        'assumptions': ['InvoiceDate 映射为 confirmed_date', '只统计 completed 订单', '不含成本/毛利事实']}))


@app.post('/tools/sales-query')
def sales_query(request: SalesQueryRequest) -> JSONResponse:
    result = query_sales(request)
    # Preserve the original list shape; new clients use /api/query for metadata.
    rows = [{key: row[key] for key in ('dimension', 'value') if key in row} for row in result['rows']]
    return JSONResponse(json_ready(rows), headers={'X-Result-Truncated': str(result['truncated']).lower()})


@app.post('/api/query')
def query(request: SalesQueryRequest) -> JSONResponse:
    return JSONResponse(json_ready(query_sales(request)))


@app.post('/tools/sales-mom')
def sales_mom(period: DateRange) -> JSONResponse:
    return JSONResponse(json_ready(query_sales_mom(period, SalesFilters())))


@app.get('/sales/trend')
def sales_trend() -> JSONResponse:
    with read_connection() as connection:
        coverage = query_coverage(connection)
        if coverage['start_date'] is None:
            return JSONResponse([])
        by_metric = {}
        for metric in ('sales_amount', 'order_count', 'customer_count'):
            tool = query_sales(SalesQueryRequest(metric_code=metric, start_date=coverage['start_date'],
                end_date=coverage['last_date'] + timedelta(days=1), group_by='month', limit=500), connection)
            by_metric[metric] = {row['dimension']: row['value'] for row in tool['rows']}
        rows = [{'month': month, **{metric: values.get(month, 0) for metric, values in by_metric.items()}}
                for month in by_metric['sales_amount']]
    return JSONResponse(json_ready(rows))


@app.post('/api/analyze')
def analyze_question(request: AnalyzeRequest) -> JSONResponse:
    return JSONResponse(json_ready(analyze(request)))


@app.post('/api/analyze/structured')
def analyze_structured(intent: AnalysisIntent) -> JSONResponse:
    return JSONResponse(json_ready(execute_intent(intent, '结构化分析请求', uuid4(), 'structured')))


@app.get('/api/analyses')
def history() -> list[dict]:
    return store.recent()


@app.get('/api/analyses/{analysis_id}')
def saved_analysis(analysis_id: UUID) -> JSONResponse:
    result = store.get(analysis_id)
    if result is None:
        raise ServiceError('not_found', '分析记录不存在。', 404)
    return JSONResponse(result)


@app.get('/api/analyses/{analysis_id}/export/{format}')
def export_analysis(analysis_id: UUID, format: str) -> Response:
    if format not in ('json', 'md'):
        raise ServiceError('unsupported_format', '支持 json 或 md 导出。')
    result = store.get(analysis_id)
    if result is None:
        raise ServiceError('not_found', '分析记录不存在。', 404)
    headers = {'Content-Disposition': f'attachment; filename="analysis-{analysis_id}.{format}"'}
    if format == 'json':
        return JSONResponse(result, headers=headers)
    return Response(store.markdown(result), media_type='text/markdown; charset=utf-8', headers=headers)


@app.get('/api/analyses/{analysis_id}/feedback')
def read_analysis_feedback(analysis_id: UUID) -> dict:
    return {'feedback':store.feedback(analysis_id)}


@app.put('/api/analyses/{analysis_id}/feedback')
def update_analysis_feedback(analysis_id: UUID, feedback: AnalysisFeedback) -> dict:
    return {'feedback':store.save_feedback(analysis_id,feedback)}


@app.post('/tools/sales-yoy')
def sales_yoy(request: SalesYoYRequest) -> JSONResponse:
    return JSONResponse(json_ready(query_sales_yoy(request,request.filters)))


@app.post('/api/data-quality')
def data_quality(period: DateRange) -> JSONResponse:
    return JSONResponse(json_ready(query_data_quality(period)))


@app.post('/api/anomalies')
def anomalies(request: AnomalyRequest) -> JSONResponse:
    return JSONResponse(json_ready(query_anomalies(request)))


@app.post('/api/monitor')
def monitor(request: AnomalyRequest) -> JSONResponse:
    with read_connection() as connection:
        quality=query_data_quality(DateRange(start_date=request.start_date,end_date=request.end_date),connection)
        anomalies=query_anomalies(request,connection,quality['current'])
    report_id=uuid4()
    report={'id':report_id,'quality':quality,'anomalies':anomalies}
    store.save_monitor_report(report_id,report)
    return JSONResponse(json_ready(report))


@app.get('/api/monitor/{report_id}/export')
def export_monitor(report_id: UUID) -> JSONResponse:
    report=store.get_monitor_report(report_id)
    if report is None:
        raise ServiceError('not_found','检查报告不存在。',404)
    return JSONResponse(report,headers={'Content-Disposition':f'attachment; filename="quality-anomalies-{report_id}.json"'})


@app.get('/api/dashboard')
def dashboard(month: date | None = None) -> JSONResponse:
    from backend.app.tools.dashboard_tool import query_dashboard
    return JSONResponse(json_ready(query_dashboard(month)))


@app.post('/api/analyses/{analysis_id}/drill')
def drill(analysis_id: UUID, change: DrillRequest) -> JSONResponse:
    from backend.app.agent.drill import drill_analysis
    return JSONResponse(json_ready(drill_analysis(analysis_id, change)))


@app.get('/api/runs/{run_id}')
def run_record(run_id: UUID) -> JSONResponse:
    result=store.get_run(run_id)
    if result is None:
        raise ServiceError('not_found','运行记录不存在。',404)
    return JSONResponse(result)


@app.post('/api/reports')
def create_business_report(request: ReportRequest) -> JSONResponse:
    return JSONResponse(json_ready(generate_report(request)))


@app.get('/api/reports')
def list_business_reports() -> list[dict]:
    return store.business_reports()


@app.get('/api/reports/{report_id}')
def read_business_report(report_id: UUID) -> JSONResponse:
    return JSONResponse(get_report(report_id))


@app.get('/api/reports/{report_id}/export/{format}')
def export_business_report(report_id: UUID, format: str) -> Response:
    if format not in ('md','json'):
        raise ServiceError('unsupported_format','经营报告支持md或json导出。')
    report=get_report(report_id)
    headers={'Content-Disposition':f'attachment; filename="monthly-report-{report_id}.{format}"'}
    if format=='json':
        return JSONResponse(report,headers=headers)
    return Response(report['markdown'],media_type='text/markdown; charset=utf-8',headers=headers)


@app.get('/data/issues')
def issues_home() -> FileResponse:
    return FileResponse(STATIC / 'issues.html')
