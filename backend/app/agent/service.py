import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import monotonic
from uuid import UUID, uuid4

from backend.app.agent import store
from backend.app.agent.execution import Run, MODEL_GATE, select_strategy
from backend.app.agent.provider import parse_question
from backend.app.agent.schemas import AnalysisIntent, AnalyzeRequest
from backend.app.config import settings
from backend.app.db import read_connection
from backend.app.errors import ServiceError
from backend.app.semantic.metrics import get_metric
from backend.app.data_management.metric_versions import pinned_execution, evidence
from backend.app.tools.analysis_tool import change_values, query_contributions, query_coverage, query_product_structure
from backend.app.tools.schemas import SalesQueryRequest
from backend.app.tools.sql_allowlist import query_sales
from backend.app.tools.yoy_tool import query_sales_yoy
from backend.app.tools.quality_tool import current_quality


def _execute_intent(run: Run, intent: AnalysisIntent, question: str, session_id: UUID, mode: str, model_usage: dict | None = None, parent_id: UUID | None = None) -> dict:
    started = monotonic()
    codes=[intent.metric_code]+(['sales_yoy'] if intent.action=='yoy' else [])
    metric_definitions=evidence(codes)
    metric = get_metric(intent.metric_code)
    planned = ['data_quality','metadata']
    planned += (['sales_yoy'] if intent.action == 'yoy' else ['previous_query','current_query'] if intent.action == 'compare'
                else ['product_structure'] if run.record['strategy']=='product_mix' else ['metric_query'])
    if intent.action == 'compare' and intent.metric_code == 'sales_amount':
        planned += ['contribution:'+d for d in intent.contribution_dimensions]
    planned += ['verify']
    trace = [{'step': 'understand', 'status': 'ok', 'intent': intent.model_dump(mode='json')},
             {'step': 'plan', 'status': 'ok', 'tools': planned}]
    warnings = ['贡献分解描述金额变化的来源，不能证明促销、流失、市场变化等业务原因。']
    result = {'id': uuid4(), 'session_id': session_id, 'created_at': datetime.now(timezone.utc),
              'status': 'completed', 'mode': mode, 'question': question, 'feedback': None,
              'model_usage': model_usage or {'strategy': 'structured', 'calls': 0, 'total_tokens': 0},
              'metric_definitions': metric_definitions, 'intent': intent.model_dump(mode='json'), 'trace': trace, 'warnings': warnings,
              'attributions': [], 'charts': [], 'comparison': None}
    if parent_id is not None:
        result['parent_analysis_id'] = str(parent_id)
    with read_connection() as connection:
        source=getattr(connection,'info',{}).get('dataset_source', {'version_id':'baseline-v1'})
        result['dataset_source']=source
        if parent_id is not None:
            parent=store.get(parent_id)
            if parent and parent.get('dataset_source',{}).get('version_id','baseline-v1')!=source['version_id']:
                raise ServiceError('dataset_context_conflict','父分析属于其他数据版本，请切回原版本再下钻。',409)
        if parent_id is not None and parent and parent.get('metric_definitions') and any(code in parent['metric_definitions'] and parent['metric_definitions'][code] != item for code,item in metric_definitions.items()):
            raise ServiceError('metric_context_conflict','父分析使用旧口径，请开启新分析；历史结果仍可查看。',409)
        quality=run.call('data_quality',lambda:current_quality(connection))
        if quality['blocking']:
            raise ServiceError('data_quality_failed','质量检查发现无效数据，已停止分析，请先查看数据质量报告。',409)
        result['quality']=quality
        trace.append({'step':'data_quality','status':'ok','data_version':quality['data_version'],'rules':quality['rules']})
        warnings.append('未确认原始记录缺失率；月份覆盖完整不代表逐日数据完整。')
        coverage = run.call('metadata',lambda:query_coverage(connection))
        result['coverage'] = coverage
        trace.append({'step': 'metadata', 'status': 'ok', 'coverage': coverage})
        periods = [intent.period] + ([intent.previous_period] if intent.previous_period else [])
        for period in periods:
            if (coverage['start_date'] is None or period.start_date < coverage['start_date'] or
                period.end_date - timedelta(days=1) > coverage['last_date']):
                warnings.append(f'{period.start_date} 至 {period.end_date} 超出数据实际覆盖范围，期间可能不完整。')
        if intent.action == 'yoy':
            tool=run.call('sales_yoy',lambda:query_sales_yoy(intent.period,intent.filters,connection))
            result['yoy']=tool
            result['data']=[{'dimension':row['month'],'value':row['sales_yoy']} for row in tool['rows']]
            warnings.extend(tool['warnings'])
            reasons=[row['null_reason'] for row in tool['rows'] if row['null_reason']]
            if reasons:
                warnings.append('部分月份同比不可计算：'+', '.join(sorted(set(reasons))))
            result['answer']='销售额同比按本月与去年同月计算；表格保留完整月份、基期金额和不可计算原因。'
            result['charts'].append({'title':'销售额同比','type':'bar','unit':'ratio',
                'points':[{'label':row['month'],'value':row['sales_yoy']} for row in tool['rows'] if row['sales_yoy'] is not None]})
            trace.append({'step':'sales_yoy','status':'ok','result':tool})
        elif intent.action == 'trend':
            tool = (run.call('product_structure',lambda:query_product_structure(connection,intent.period,intent.filters))
                    if run.record['strategy']=='product_mix' else
                    run.call('metric_query',lambda:query_sales(SalesQueryRequest(metric_code=intent.metric_code, **intent.period.model_dump(),
                        group_by=intent.group_by, filters=intent.filters, limit=500), connection)))
            trace.append({'step': 'query', 'status': 'ok', 'result': tool})
            result['data'] = tool['rows']
            if run.record['strategy']=='product_mix':
                result['product_structure'] = tool
            warnings.extend(tool['warnings'])
            if tool['empty']:
                result['answer'] = '所选范围和筛选条件没有匹配的销售记录，无法据此判断业务变化。'
            else:
                result['answer'] = f"按{ {'month':'月份', 'customer':'客户', 'product':'商品', 'country':'国家'}[intent.group_by]}查询{metric['name']}，返回 {tool['row_count']} 组结果，单位 {metric['unit']}。"
                if tool['truncated']:
                    result['answer'] += '结果已截断，请增加筛选条件后再分析完整分布。'
                result['charts'].append({'title': metric['name'], 'type': 'line' if intent.group_by == 'month' else 'bar',
                    'unit': metric['unit'], 'points': [{'label': r['dimension'], 'value': r['value']} for r in tool['rows'] if r['value'] is not None]})
        else:
            previous_tool = run.call('previous_query',lambda:query_sales(SalesQueryRequest(metric_code=intent.metric_code,
                **intent.previous_period.model_dump(), filters=intent.filters), connection))
            current_tool = run.call('current_query',lambda:query_sales(SalesQueryRequest(metric_code=intent.metric_code,
                **intent.period.model_dump(), filters=intent.filters), connection))
            trace.extend([{'step': 'previous_query', 'status': 'ok', 'result': previous_tool},
                          {'step': 'current_query', 'status': 'ok', 'result': current_tool}])
            warnings.extend(previous_tool['warnings']+current_tool['warnings'])
            if previous_tool['rows'][0]['value'] is None or current_tool['rows'][0]['value'] is None:
                result['data']=[{'dimension':str(intent.previous_period.start_date),'value':previous_tool['rows'][0]['value']},
                                {'dimension':str(intent.period.start_date),'value':current_tool['rows'][0]['value']}]
                result['answer']='至少一期没有匹配记录或分母为0，平均值及比较不可计算，不把缺失值当成0。'
            else:
                previous, current = Decimal(previous_tool['rows'][0]['value']), Decimal(current_tool['rows'][0]['value'])
                comparison = change_values(previous, current)
                result['comparison'] = comparison
                result['data'] = [{'dimension': str(intent.previous_period.start_date), 'value': previous},
                                  {'dimension': str(intent.period.start_date), 'value': current}]
                if previous_tool['empty'] or current_tool['empty']:
                    warnings.append('至少一期没有匹配记录，聚合值按 0 展示；不能据此断言业务停摆或客户流失。')
                if comparison['change_ratio'] is None:
                    warnings.append('基期为 0，变化比例返回 null。')
                if (intent.previous_period.end_date - intent.previous_period.start_date).days != (intent.period.end_date - intent.period.start_date).days:
                    warnings.append('两个期间天数不同，当前比较期间总量，没有做日均归一化。')
                result['charts'].append({'title': '两期对比', 'type': 'bar', 'unit': metric['unit'],
                    'points': [{'label': str(intent.previous_period.start_date), 'value': previous},
                               {'label': str(intent.period.start_date), 'value': current}]})
                direction = '增加' if comparison['delta'] > 0 else '减少' if comparison['delta'] < 0 else '持平'
                ratio = f"，变化 {comparison['change_ratio']:.2%}" if comparison['change_ratio'] is not None else '，基期为 0，比例无法计算'
                result['answer'] = f"{metric['name']}从 {previous:,.2f} 变为 {current:,.2f} {metric['unit']}，{direction} {abs(comparison['delta']):,.2f}{ratio}。"
                if previous_tool['empty'] and current_tool['empty']:
                    result['answer'] = '两个期间都没有匹配记录，无法分析变化原因。'
                elif intent.metric_code == 'sales_amount':
                    for dimension in intent.contribution_dimensions:
                        attribution = run.call('contribution:'+dimension,lambda:query_contributions(connection, dimension, intent.previous_period,
                            intent.period, intent.filters, previous, current))
                        result['attributions'].append(attribution)
                        trace.append({'step': 'contribution', 'dimension': dimension, 'status': 'reconciled',
                                      'group_count': attribution['group_count'], 'delta_sum': attribution['delta_sum']})
                    customers = next((a for a in result['attributions'] if a['dimension']=='customer'), {'negative':[], 'positive':[]})
                    top = customers['negative'] if comparison['delta'] < 0 else customers['positive'] if comparison['delta'] > 0 else []
                    if top:
                        result['answer'] += f" 客户维度中，{top[0]['dimension']} 的变化为 {top[0]['delta']:,.2f} GBP，是该方向贡献最大的客户。"
                    result['answer'] += ' 所选贡献维度分别对账通过；这些是金额贡献，实际业务原因仍需额外证据。'
                    for attribution in result['attributions']:
                        points = attribution['negative'] if comparison['delta'] < 0 else attribution['positive']
                        result['charts'].append({'title': {'customer':'客户', 'product':'商品', 'country':'国家','customer_product':'客户×商品'}[attribution['dimension']] + '变化贡献（前 10）',
                            'type': 'bar', 'unit': 'GBP', 'points': [{'label': row['dimension'], 'value': row['delta']} for row in points]})
                elif intent.metric_code != 'sales_amount':
                    warnings.append('当前仅销售额支持完整维度贡献分解；去重指标及平均值不能跨商品简单相加。')
    trace.append({'step': 'verify', 'status': 'ok', 'duration_ms': round((monotonic() - started) * 1000)})
    result['warnings'] = list(dict.fromkeys(warnings))
    return result




def verify_result(result: dict) -> None:
    comparison=result.get('comparison')
    if comparison and comparison['current']-comparison['previous'] != comparison['delta']:
        raise ServiceError('reconciliation_failed','比较差额校验失败，已停止保存结论。',409)
    for attribution in result['attributions']:
        if not attribution['reconciled'] or attribution['delta_sum'] != comparison['delta']:
            raise ServiceError('reconciliation_failed','贡献差额校验失败，已停止保存结论。',409)

def _run_intent(run: Run, intent: AnalysisIntent, question: str, session_id: UUID, mode: str,
                model_usage: dict | None = None, parent_id: UUID | None = None) -> dict:
    intent, selected = select_strategy(intent)
    run.record['strategy'] = selected
    run.record['intent'] = intent.model_dump(mode='json')
    result = _execute_intent(run,intent,question,session_id,mode,model_usage,parent_id)
    run.call('verify',lambda:verify_result(result))
    run.record['analysis_id'] = str(result['id'])
    result['run_id'] = run.record['id']
    result['execution'] = run.snapshot()
    result['trace'].insert(1,{'step':'strategy','status':'ok','selected':selected})
    store.save(result)
    run.finish('completed',result['id'])
    return result


def _fail(run: Run, exc: Exception) -> None:
    public = (exc if isinstance(exc,ServiceError) else
              ServiceError('storage_unavailable','分析保存失败，请检查本地目录权限和空间。',503) if isinstance(exc,sqlite3.Error) else
              ServiceError('execution_failed','分析执行失败，已停止；请查看运行记录。',500))
    run.finish('failed',error=public)
    if public is not exc:
        raise public from exc


@pinned_execution
def execute_intent(intent: AnalysisIntent, question: str, session_id: UUID, mode: str,
                   model_usage: dict | None = None, parent_id: UUID | None = None) -> dict:
    run=Run()
    try:
        run.usage(model_usage or {})
        return _run_intent(run,intent,question,session_id,mode,model_usage,parent_id)
    except Exception as exc:
        _fail(run,exc)
        raise


@pinned_execution
def analyze(request: AnalyzeRequest) -> dict:
    run=Run()
    token=MODEL_GATE.set(run.model_gate)
    try:
        from backend.app.data_management.versions import source_info
        selected_source=source_info()
        session_id = request.session_id or uuid4()
        context_data = store.latest_intent(session_id) if request.session_id else None
        if request.session_id and context_data is None:
            raise ServiceError('session_not_found', '没有找到该会话，请开启新分析。', 404)
        if request.session_id:
            with store.connect() as conn:
                row=conn.execute('SELECT payload FROM analyses WHERE session_id=? ORDER BY created_at DESC LIMIT 1',(str(session_id),)).fetchone()
            import json
            prior=json.loads(row[0])
            if prior.get('dataset_source',{}).get('version_id','baseline-v1')!=selected_source['version_id']:
                raise ServiceError('dataset_context_conflict','会话属于其他数据版本，请开启新分析。',409)
        context = AnalysisIntent.model_validate(context_data) if context_data else None
        parsed = run.call('parse_question',lambda:parse_question(request.question, context))
        run.usage(parsed._usage)
        if parsed.status == 'needs_clarification':
            run.finish('needs_clarification')
            return {'status': 'needs_clarification', 'message': parsed.message, 'session_id': request.session_id,
                    'mode': settings.llm_provider, 'model_usage': parsed._usage,'run_id':run.record['id'],'execution':run.record}
        return _run_intent(run,parsed.intent,request.question,session_id,settings.llm_provider,parsed._usage)
    except Exception as exc:
        _fail(run,exc)
        raise
    finally:
        MODEL_GATE.reset(token)
