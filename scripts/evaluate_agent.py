"""Separate natural-language parsing from gold-plan execution and independent SQL.

Offline never calls a model. Live is opt-in, explicitly selected and capped at 5.
Evaluation history is isolated in a temporary SQLite file, not the user's history.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from time import monotonic
from uuid import uuid4

from sqlalchemy import text

from backend.app.agent import provider
from backend.app.agent.schemas import AnalysisIntent, ParsedQuestion
from backend.app.agent.service import execute_intent
from backend.app.config import settings
from backend.app.db import read_connection
from backend.app.errors import ServiceError
from backend.app.serialization import json_ready

ROOT = Path(__file__).resolve().parents[1]
CASE_FILE = ROOT / 'tests/evaluation/questions.json'
# Independent reference expressions. Do NOT import production SQL templates.
AGGREGATES = {'sales_quantity':'COALESCE(SUM(line.quantity),0)',
              'average_order_value':'SUM(line.sales_amount)/NULLIF(COUNT(DISTINCT header.order_id),0)',
              'average_selling_price':'SUM(line.sales_amount)/NULLIF(SUM(line.quantity),0)',
              'sales_amount': 'COALESCE(SUM(line.sales_amount),0)',
              'order_count': 'COUNT(DISTINCT header.order_id)',
              'customer_count': 'COUNT(DISTINCT header.customer_id)'}
DIMENSIONS = {
    'month': "date_trunc('month',header.confirmed_date)::date",
    'customer': '(SELECT customer_name FROM dim_customer WHERE customer_id=header.customer_id)',
    'product': "(SELECT product_code || ' - ' || product_name FROM dim_product WHERE product_id=line.product_id)",
    'country': '(SELECT region_name FROM dim_region WHERE region_id=header.region_id)',
}
REFERENCE_FROM = '''FROM fact_sales_detail line JOIN fact_sales_order header
ON header.order_id=line.order_id WHERE header.order_status='completed'
AND header.confirmed_date >= :start_date AND header.confirmed_date < :end_date
AND (CAST(:country AS text) IS NULL OR EXISTS (SELECT 1 FROM dim_region region
    WHERE region.region_id=header.region_id AND region.region_name=:country))
AND (CAST(:customer_code AS text) IS NULL OR EXISTS (SELECT 1 FROM dim_customer customer
    WHERE customer.customer_id=header.customer_id AND customer.customer_code=:customer_code))
AND (CAST(:product_code AS text) IS NULL OR EXISTS (SELECT 1 FROM dim_product product
    WHERE product.product_id=line.product_id AND product.product_code=:product_code))'''


def load_cases(path: Path = CASE_FILE) -> dict:
    suite = json.loads(path.read_text())
    ids = [c['id'] for c in suite['cases']]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate case IDs')
    for case in suite['cases']:
        ParsedQuestion.model_validate(case['expected'] | (
            {'message': '需要澄清'} if case['expected']['status'] == 'needs_clarification' else {}))
        if case['context']:
            AnalysisIntent.model_validate(case['context'])
    return suite


def compare_parsed(actual: ParsedQuestion, expected: dict) -> list[str]:
    errors = []
    if actual.status != expected['status']:
        return [f"status: expected {expected['status']}, got {actual.status}"]
    if actual.status == 'ready':
        gold = AnalysisIntent.model_validate(expected['intent']).model_dump(mode='json')
        observed = actual.intent.model_dump(mode='json')
        for field in gold:
            if gold[field] != observed[field]:
                errors.append(f'{field}: expected {gold[field]}, got {observed[field]}')
    elif actual.intent is not None or not actual.message.strip():
        errors.append('clarification must not contain executable intent and must have a message')
    return errors


def summary(rows: list[dict]) -> dict:
    applicable = [r for r in rows if r['status'] != 'not_applicable']
    counts = Counter(r['status'] for r in applicable)
    return {'total': len(rows), 'evaluated': len(applicable), 'passed': counts['passed'],
            'failed': counts['failed'], 'errors': counts['error'],
            'not_applicable': len(rows)-len(applicable),
            'pass_rate': counts['passed']/len(applicable) if applicable else None}


def reference(intent: AnalysisIntent, connection) -> dict:
    evidence = []
    def run(period, dimension: str | None) -> list[dict]:
        params = period.model_dump() | intent.filters.model_dump()
        group = DIMENSIONS[dimension] if dimension else None
        sql = f"SELECT {group + ' AS dimension,' if group else ''} {AGGREGATES[intent.metric_code]} AS value {REFERENCE_FROM}"
        if group:
            sql += f' GROUP BY {group}'
        rows = [dict(r) for r in connection.execute(text(sql), params).mappings()]
        evidence.append({'sql':sql,'parameters':json_ready(params)})
        return rows
    if intent.action == 'yoy':
        baseline=intent.period.model_copy(update={'start_date':intent.period.start_date.replace(year=intent.period.start_date.year-1),
                                                'end_date':intent.period.end_date.replace(year=intent.period.end_date.year-1)})
        values={r['dimension']:Decimal(r['value']) for p in (intent.period,baseline) for r in run(p,'month')}
        coverage_sql="SELECT MIN(confirmed_date) AS first,MAX(confirmed_date) AS last FROM fact_sales_order WHERE order_status='completed'"
        coverage=connection.execute(text(coverage_sql)).mappings().one()
        evidence.append({'sql':coverage_sql,'parameters':{}})
        month=intent.period.start_date
        rows=[]
        def month_end(start):
            return date(start.year+1,1,1)-timedelta(days=1) if start.month==12 else date(start.year,start.month+1,1)-timedelta(days=1)
        while month<intent.period.end_date:
            prior=month.replace(year=month.year-1)
            current,previous=values.get(month),values.get(prior)
            complete=coverage['first'] is not None and coverage['first']<=prior and coverage['last']>=month_end(month)
            reason='incomplete_period' if not complete else 'missing_month' if current is None or previous is None else 'zero_baseline' if previous==0 else None
            rows.append({'dimension':month,'value':(current-previous)/previous if reason is None else None,'null_reason':reason})
            month=month_end(month)+timedelta(days=1)
        return {'rows':json_ready([{'dimension':r['dimension'],'value':r['value']} for r in rows]),'truncated':False,
                'yoy_null_reasons':[r['null_reason'] for r in rows],'evidence':evidence}
    if intent.action == 'trend':
        rows = run(intent.period, intent.group_by)
        # Production sort uses stable business keys; compute independent order here.
        if intent.group_by == 'customer':
            names = {r['customer_name']:r['customer_code'] for r in connection.execute(text('SELECT customer_name,customer_code FROM dim_customer')).mappings()}
            rows.sort(key=lambda r:names[r['dimension']])
        elif intent.group_by == 'country':
            names = {r['region_name']:r['region_code'] for r in connection.execute(text('SELECT region_name,region_code FROM dim_region')).mappings()}
            rows.sort(key=lambda r:names[r['dimension']])
        else:
            rows.sort(key=lambda r:str(r['dimension']).split(' - ')[0])
        return {'rows':json_ready(rows[:500]),'truncated':len(rows)>500,'evidence':evidence}
    before=run(intent.previous_period,None)[0]['value']
    after=run(intent.period,None)[0]['value']
    if before is None or after is None:
        return {'rows':json_ready([{'dimension':str(intent.previous_period.start_date),'value':before},
                                  {'dimension':str(intent.period.start_date),'value':after}]),
                'truncated':False,'comparison_null':True,'evidence':evidence}
    previous = Decimal(before)
    current = Decimal(after)
    return {'comparison':{'previous':str(previous),'current':str(current),'delta':str(current-previous),
                         'change_ratio':str((current-previous)/previous) if previous else None},
            'evidence':evidence}


def result_errors(actual: dict, oracle: dict, checks: dict) -> list[str]:
    errors = []
    if 'rows' in oracle:
        rows = json_ready([{'dimension':r['dimension'],'value':r['value']} for r in actual['data']])
        # Values normalized by Decimal, dimensions by exact JSON representation.
        if len(rows) != len(oracle['rows']) or any(
            a['dimension'] != b['dimension'] or (a['value'] is None) != (b['value'] is None) or (a['value'] is not None and abs(Decimal(str(a['value']))-Decimal(str(b['value'])))>Decimal('1e-20'))
            for a,b in zip(rows,oracle['rows'])):
            errors.append('data rows disagree with independent reference SQL')
        truncated = any(t.get('result',{}).get('truncated') for t in actual['trace'])
        if bool(truncated) != oracle['truncated']:
            errors.append('truncation disagrees with full reference groups')
    else:
        for field, expected in oracle['comparison'].items():
            observed = actual['comparison'][field]
            if (expected is None and observed is not None) or (expected is not None and (observed is None or abs(Decimal(str(observed))-Decimal(expected))>Decimal('1e-20'))):
                errors.append(f'comparison {field} disagrees with reference SQL')
        for attribution in actual['attributions']:
            if not attribution['reconciled'] or Decimal(str(attribution['delta_sum'])) != Decimal(oracle['comparison']['delta']):
                errors.append('attribution reconciliation failed')
    if oracle.get('comparison_null') and actual['comparison'] is not None:
        errors.append('null average comparison must not become zero')
    if 'yoy_null_reasons' in oracle and [r['null_reason'] for r in actual['yoy']['rows']] != oracle['yoy_null_reasons']:
        errors.append('YoY missing/coverage reasons disagree')
    for field in ('previous','current','delta'):
        if field in checks and Decimal(str(actual['comparison'][field])) != Decimal(checks[field]):
            errors.append(f'fixed anchor {field} disagrees')
    if 'first_value' in checks and (not actual['data'] or actual['data'][0]['value'] is None or abs(Decimal(str(actual['data'][0]['value']))-Decimal(checks['first_value']))>Decimal('1e-20')):
        errors.append('fixed anchor first value disagrees')
    if 'row_count' in checks and len(actual['data']) != checks['row_count']:
        errors.append('unexpected row count')
    if checks.get('change_ratio_null') and actual['comparison']['change_ratio'] is not None:
        errors.append('zero baseline ratio must be null')
    if 'attribution_dimensions' in checks and [a['dimension'] for a in actual['attributions']] != checks['attribution_dimensions']:
        errors.append('unexpected attribution dimensions')
    if 'top_negative_customer' in checks:
        customers = next((a for a in actual['attributions'] if a['dimension']=='customer'),None)
        if not customers or not customers['negative'] or customers['negative'][0]['dimension_key'] != checks['top_negative_customer']:
            errors.append('top negative customer disagrees')
    if 'truncated' in checks and oracle.get('truncated') != checks['truncated']:
        errors.append('fixed truncation expectation disagrees')
    for fragment in checks.get('warning_contains',[]):
        if not any(fragment in w for w in actual['warnings']):
            errors.append(f'missing warning: {fragment}')
    for fragment in checks.get('answer_contains',[]):
        if fragment not in actual['answer']:
            errors.append(f'missing answer fragment: {fragment}')
    return errors


def data_signature() -> dict:
    with read_connection() as connection:
        row = dict(connection.execute(text('''SELECT COUNT(*) AS details,
            COUNT(DISTINCT header.order_id) AS orders,
            MIN(header.confirmed_date) AS start_date, MAX(header.confirmed_date) AS end_date,
            SUM(line.sales_amount) AS sales_amount
            FROM fact_sales_detail line JOIN fact_sales_order header ON header.order_id=line.order_id
            WHERE header.order_status='completed' ''')).mappings().one())
    stats = json_ready(row)
    return {'summary':stats,'summary_sha256':sha256(json.dumps(stats,sort_keys=True).encode()).hexdigest(),
            'limitation':'Summary fingerprint is not a full row-level content hash; use an unchanged local dataset for reproduction.'}


def run_suite(suite: dict, mode: str, selected: list[str], output: Path) -> dict:
    cases = suite['cases']
    if selected:
        unknown = set(selected)-{c['id'] for c in cases}
        if unknown:
            raise ValueError(f'unknown IDs: {sorted(unknown)}')
        cases = [c for c in cases if c['id'] in selected]
    if mode == 'live' and (not selected or len(cases)>5 or settings.llm_provider=='demo'):
        raise ValueError('live requires 1–5 explicit IDs and a configured real provider')
    old_store, old_cache = settings.analysis_store, settings.llm_cache_seconds
    parsing, execution = [], []
    started = monotonic()
    report = {'suite_version':suite['version'],'suite_sha256':sha256(json.dumps(suite,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
              'started_at':datetime.now(timezone.utc).isoformat(),'mode':mode,
              'provider':'demo' if mode=='offline' else settings.llm_provider,
              'model':None if mode=='offline' else settings.llm_model,
              'prompt_version':provider.PROMPT_VERSION,'selected_ids':[c['id'] for c in cases]}
    try:
        with tempfile.TemporaryDirectory(prefix='insight-eval-') as temp:
            settings.analysis_store = Path(temp)/'history.sqlite3'
            if mode == 'live':
                settings.llm_cache_seconds = 0
                provider.clear_parse_cache()
            report['data_before'] = data_signature()
            for case in cases:
                row = {'id':case['id'],'category':case['category'],'question':case['question'],
                       'expected':case['expected'],'context':case['context']}
                try:
                    context = AnalysisIntent.model_validate(case['context']) if case['context'] else None
                    parsed = provider.parse_demo(case['question'],context) if mode=='offline' else provider.parse_question(case['question'],context)
                    errors = compare_parsed(parsed,case['expected'])
                    row.update(status='failed' if errors else 'passed',errors=errors,
                               observed=parsed.model_dump(mode='json'),usage=parsed._usage)
                except ServiceError as exc:
                    row.update(status='error',errors=[exc.code])
                parsing.append(row)
                item = {'id':case['id']}
                if case['expected']['status'] != 'ready':
                    item['status'] = 'not_applicable'
                else:
                    try:
                        gold = AnalysisIntent.model_validate(case['expected']['intent'])
                        with read_connection() as connection:
                            oracle = reference(gold,connection)
                        actual = execute_intent(gold,case['question'],uuid4(),'evaluation')
                        errors = result_errors(actual,oracle,case['checks'])
                        item.update(status='failed' if errors else 'passed',errors=errors,
                                    reference=oracle,checks=case['checks'],answer=actual['answer'],
                                    comparison=json_ready(actual['comparison']),warnings=actual['warnings'],
                                    row_count=len(actual['data']))
                    except ServiceError as exc:
                        item.update(status='error',errors=[exc.code])
                execution.append(item)
            report['data_after'] = data_signature()
    finally:
        settings.analysis_store, settings.llm_cache_seconds = old_store,old_cache
    report.update(parsing=parsing,gold_plan_execution=execution,parsing_summary=summary(parsing),
                  execution_summary=summary(execution),duration_seconds=round(monotonic()-started,2),
                  data_summary_unchanged=report['data_before']==report['data_after'])
    report['model_usage'] = {field:sum(r.get('usage',{}).get(field,0) for r in parsing)
                             for field in ('calls','prompt_tokens','completion_tokens','total_tokens')}
    report['interpretation'] = 'Parsing and gold-plan execution are independent; gold-plan success is not end-to-end model accuracy. No live SQL is executed from a failed model plan.'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(json_ready(report),ensure_ascii=False,indent=2)+'\n')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',type=Path,default=CASE_FILE)
    parser.add_argument('--mode',choices=('offline','live'),default='offline')
    parser.add_argument('--cases',nargs='+',default=[])
    parser.add_argument('--output',type=Path,default=ROOT/'data/artifacts/evaluation-offline.json')
    args = parser.parse_args()
    try:
        report = run_suite(load_cases(args.suite),args.mode,args.cases,args.output)
    except (ValueError,ServiceError) as exc:
        print(json.dumps({'error':exc.code if isinstance(exc,ServiceError) else str(exc)},ensure_ascii=False))
        return 2
    print(json.dumps({key:report[key] for key in ('mode','parsing_summary','execution_summary','model_usage','data_summary_unchanged')},ensure_ascii=False,indent=2))
    print(f'Report: {args.output}')
    summaries = (report['parsing_summary'],report['execution_summary'])
    return 1 if any(s['failed'] or s['errors'] for s in summaries) or not report['data_summary_unchanged'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
