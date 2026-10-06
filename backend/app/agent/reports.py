from backend.app.data_management.metric_versions import pinned_execution, evidence
"""Immutable monthly reports rendered from one recorded retail database snapshot."""
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, model_validator

from backend.app.agent import store
from backend.app.agent.execution import Run
from backend.app.db import read_connection
from backend.app.errors import ServiceError
from backend.app.serialization import json_ready
from backend.app.tools.analysis_tool import change_values, query_contributions
from backend.app.tools.dashboard_tool import query_dashboard
from backend.app.tools.quality_tool import month_is_covered
from backend.app.tools.schemas import DateRange, SalesFilters


class ReportRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    month: date

    @model_validator(mode='after')
    def full_month(self):
        if self.month.day!=1 or not 1<self.month.year<9999:
            raise ValueError('报告月份须为每月1日，并能表示前后月份。')
        return self


def escaped(value: object) -> str:
    text=str(value).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    for char in ('\\','|','[',']','*','_','`'):
        text=text.replace(char,'\\'+char)
    return text.replace('\r',' ').replace('\n',' ')


def amount(value: object) -> str:
    return '未知 / 无匹配记录' if value is None else f'{Decimal(str(value)):,.2f}'


def ratio(value: object) -> str:
    return '不可计算' if value is None else f'{Decimal(str(value)):.2%}'


def table(headers: list[str], rows: list[list]) -> list[str]:
    return ['| '+' | '.join(headers)+' |','| '+' | '.join('---' for _ in headers)+' |'] + [
        '| '+' | '.join(escaped(v) for v in row)+' |' for row in rows]


def render_markdown(report: dict) -> str:
    snapshot=report['snapshot']
    dashboard=snapshot['dashboard']
    sales=next(c for c in dashboard['cards'] if c['metric_code']=='sales_amount')
    period=dashboard['period']
    lines=[f"# {report['month'][:7]} 月度经营报告",'',
           f"来源：Online Retail II 零售样本；销售金额 GBP；只统计 completed 订单。",'',
           f"数据版本：{snapshot.get('dataset_source',{}).get('version_id','历史未登记')}；导入批次：{snapshot.get('dataset_source',{}).get('batch_id') or '原基线无批次登记'}。",'',
           f"期间：{period['start_date']} 至 {period['end_date']}（结束日不含）。",'',
           f"报告ID：{report['id']}；生成时间：{report['created_at']}；模板：{report['template_version']}。",'',
           f"快照SHA-256：{report['snapshot_sha256']}", '',
           '## 1. 摘要','',f"当月销售额 **{amount(sales['value'])} GBP**；相对上月变化 **{ratio(sales['change_ratio'])}**。",
           '本报告只描述已观测金额和配置阈值下的变化，不推断经营因果。','',
           '## 2. 指标','']
    lines+=table(['指标','单位','当月','上月','环比','不可计算原因'],[
        [c['name'],c['unit'],amount(c['value']),amount(c['previous']),ratio(c['change_ratio']),c['null_reason'] or '—'] for c in dashboard['cards']])
    lines+=['','## 3. 销售','',f"上月销售额：{amount(sales['previous'])} GBP。"]
    comparison=snapshot['comparison']
    lines+=[f"两期销售额差额：{amount(comparison['delta'])} GBP。" if comparison else '基期覆盖不足或没有匹配记录，不生成强比较结论。','']
    lines+=table(['月份','销售额 GBP'],[[r['dimension'],amount(r['value'])] for r in dashboard['trend']])
    for index,dimension,name in ((4,'customer','客户'),(5,'product','产品')):
        lines+=['',f'## {index}. {name}','',f'当月销售额Top 10{name}（仅排行，不是完整贡献）：','']
        lines+=table(['对象','销售额 GBP'],[[r['dimension'],amount(r['value'])] for r in dashboard['rankings'][dimension]])
        attribution=next((a for a in snapshot['attributions'] if a['dimension']==dimension),None)
        if attribution:
            lines+=['',f"完整对账：{attribution['group_count']}组；全部变化合计 {amount(attribution['delta_sum'])} GBP；其余对象变化合计 {amount(attribution['other_delta'])} GBP。",'']
            lines+=table(['贡献对象','上月 GBP','当月 GBP','变化 GBP','状态'],[
                [r['dimension'],amount(r['previous']),amount(r['current']),amount(r['delta']),{'new':'新增','lost':'消失','continuing':'持续'}[r['status']]]
                for r in attribution['negative']+attribution['positive']])
        else:
            lines+=['','基期不完整或缺少数据，未执行完整贡献对账。']
    checks=next(r['checks'] for r in dashboard['detection']['rows'] if str(r['month'])==report['month'])
    lines+=['','## 6. 异常','', '仅展示报告月份；阈值触发是变化提醒，不代表业务原因。移动平均只使用过去连续完整月份。','']
    lines+=table(['方法','状态','当前 GBP','基线 GBP','变化','阈值','基线月份','不可判断原因'],[
        ['环比下降' if c['method']=='mom_drop' else '移动平均偏离',c['status'],amount(c['observed_value']),amount(c['baseline']),ratio(c['change_ratio']),ratio(c['threshold']),', '.join(str(m) for m in c['baseline_months']),c['reason'] or '—'] for c in checks])
    lines+=['','## 7. 发现','']
    if comparison:
        lines+=[f"- 可复算的两期差额为 {amount(comparison['delta'])} GBP。客户、产品和国家维度分别解释该差额，不能跨维度相加。"]
        for attribution in snapshot['attributions']:
            rows=attribution['negative'] if Decimal(str(comparison['delta']))<0 else attribution['positive'] if Decimal(str(comparison['delta']))>0 else []
            if rows:
                lines+=[f"- { {'customer':'客户','product':'产品','country':'国家'}[attribution['dimension']]}方向最大贡献：{escaped(rows[0]['dimension'])}，变化 {amount(rows[0]['delta'])} GBP。"]
    else:
        lines+=['- 数据不足，不能可靠定位两期变化来源。']
    lines+=['','## 8. 风险','', '- 成本、毛利、税额事实缺失，不计算或猜测毛利。',
            '- 日期范围完整不证明逐日数据完整；没有可信记录缺失率。',
            '- 未进行季节性校正或日均归一化；新增/消失仅指所选两期有无记录，不证明客户获取或流失。']
    lines += ['指标口径版本：'+', '.join(f"{code} v{item['version']}" for code,item in snapshot.get('metric_definitions',{}).items()), '']
    lines += ['- '+escaped(w) for w in snapshot['warnings']]
    lines+=['','## 附件：来源与查询证据','',
            '报告重开与下载使用保存的快照，不重新查询业务数据库。质量版本是统计指纹；以下哈希校验报告快照内容，不是整个数据库逐行版本。','',
            '```json',json.dumps({'snapshot_sha256':report['snapshot_sha256'],'run_id':report['run_id'],'evidence':snapshot},ensure_ascii=False,indent=2).replace('`','\\u0060'),'```','']
    return '\n'.join(lines)


@pinned_execution
def generate_report(request: ReportRequest) -> dict:
    run=Run()
    run.record['strategy']='monthly_report'
    run.record['intent']={'month':request.month.isoformat()}
    try:
        with read_connection() as connection:
            source=getattr(connection,'info',{}).get('dataset_source',{'version_id':'baseline-v1'})
            dashboard=run.call('dashboard_snapshot',lambda:query_dashboard(request.month,connection))
            sales=next(c for c in dashboard['cards'] if c['metric_code']=='sales_amount')
            before=DateRange(start_date=dashboard['previous_month'],end_date=request.month)
            current=DateRange(**dashboard['period'])
            attributions=[]
            comparison=None
            warnings=list(dashboard['warnings'])
            eligible=(month_is_covered(before.start_date,dashboard['quality']['summary'])
                      and sales['value'] is not None and sales['previous'] is not None)
            if eligible:
                comparison=change_values(Decimal(sales['previous']),Decimal(sales['value']))
                for dimension in ('customer','product','country'):
                    attributions.append(run.call('contribution:'+dimension,lambda:query_contributions(
                        connection,dimension,before,current,SalesFilters(),Decimal(sales['previous']),Decimal(sales['value']))))
            else:
                warnings.append('上月覆盖不足或两期缺少数据，不生成强比较结论和贡献。')
        snapshot=json_ready({'metric_definitions':evidence(['sales_amount','order_count','customer_count','sales_quantity','average_order_value','average_selling_price']), 'dataset_source':source,'dashboard':dashboard,'comparison':comparison,'attributions':attributions,'warnings':warnings})
        signature=sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        report={'id':str(uuid4()),'month':request.month.isoformat(),'created_at':datetime.now(timezone.utc).isoformat(),
                'title':request.month.strftime('%Y-%m')+' 月度经营报告','template_version':'retail-monthly-v3',
                'snapshot':snapshot,'snapshot_sha256':signature,'run_id':run.record['id']}
        run.record['report_id']=report['id']
        run.call('verify',lambda:verify_snapshot(snapshot))
        # The Markdown is stored once; old reports never change when templates change.
        report['execution']=run.snapshot()
        report['markdown']=run.call('render_report',lambda:render_markdown(report))
        report['execution']=run.snapshot()
        store.save_business_report(report)
        run.record['report_id']=report['id']
        run.finish('completed')
        return report
    except Exception as exc:
        public=(exc if isinstance(exc,ServiceError) else
                ServiceError('storage_unavailable','报告保存失败，请检查本地目录权限和空间。',503) if isinstance(exc,sqlite3.Error) else
                ServiceError('report_failed','月度报告生成失败，已停止。',500))
        run.finish('failed',error=public)
        if public is not exc:
            raise public from exc
        raise


def verify_snapshot(snapshot: dict) -> None:
    comparison=snapshot['comparison']
    for a in snapshot['attributions']:
        if not a['reconciled'] or Decimal(str(a['delta_sum']))!=Decimal(str(comparison['delta'])):
            raise ServiceError('reconciliation_failed','报告贡献与销售额变化不一致，已停止保存。',409)


def get_report(report_id: UUID) -> dict:
    report=store.get_business_report(report_id)
    if report is None:
        raise ServiceError('not_found','经营报告不存在。',404)
    return report
