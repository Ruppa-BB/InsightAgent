"""A bounded sequential tool runner: no retries, generated code, or budget reset."""
from contextvars import ContextVar
from datetime import datetime, timezone
from time import monotonic
from typing import Callable, TypeVar
from uuid import uuid4

from backend.app.agent import store
from backend.app.agent.schemas import AnalysisIntent
from backend.app.config import settings
from backend.app.errors import ServiceError

T = TypeVar('T')
MODEL_GATE: ContextVar[Callable[[], None] | None] = ContextVar('agent_model_gate', default=None)

STRATEGIES = {
    'sales_decline': '销售变化：两期比较与所选维度完整对账',
    'customer_contribution': '客户贡献：两期比较与客户维度完整对账',
    'product_mix': '产品结构：商品销售额分布或商品变化贡献',
}


def select_strategy(intent: AnalysisIntent) -> tuple[AnalysisIntent, str]:
    selected = intent.strategy
    if selected == 'auto':
        selected = ('sales_decline' if intent.action == 'compare' and intent.metric_code == 'sales_amount' else
                    'product_mix' if intent.action == 'trend' and intent.metric_code == 'sales_amount' and intent.group_by == 'product' else 'basic_query')
    data = intent.model_dump(mode='json')
    if selected == 'customer_contribution':
        data['contribution_dimensions'] = ['customer']
    elif selected == 'product_mix':
        if intent.action == 'trend':
            data['group_by'] = 'product'
        else:
            data['contribution_dimensions'] = ['product']
    return AnalysisIntent.model_validate(data), selected


class Run:
    def __init__(self) -> None:
        self.reserved_calls = 0
        self.started = monotonic()
        self.record = {'id': str(uuid4()), 'created_at': datetime.now(timezone.utc).isoformat(), 'status':'running',
                       'strategy':None,'steps':[], 'steps_used':0,'model_calls':0,'tokens':0,'retries':0,
                       'tokens_status':'known',
                       'limits':{'steps':settings.agent_max_steps,'duration_ms':settings.agent_max_duration_ms,
                                 'model_calls':settings.agent_max_model_calls,'tokens':settings.agent_max_tokens},
                       'error':None,'analysis_id':None}
        store.save_run(self.record)

    def guard(self) -> None:
        if self.record['steps_used'] >= self.record['limits']['steps']:
            raise ServiceError('step_budget_exceeded','分析步骤预算已用尽，未继续执行。',429)
        if (monotonic()-self.started)*1000 >= self.record['limits']['duration_ms']:
            raise ServiceError('run_timeout','分析累计时间已达到预算，未继续执行。',504)

    def call(self, name: str, operation: Callable[[], T]) -> T:
        self.guard()
        self.record['steps_used'] += 1
        start = monotonic()
        step = {'number':self.record['steps_used'],'tool':name,'status':'running','duration_ms':0,'error':None}
        self.record['steps'].append(step)
        store.save_run(self.record)
        try:
            value=operation()
            if (monotonic()-self.started)*1000 >= self.record['limits']['duration_ms']:
                raise ServiceError('run_timeout','分析累计时间已达到预算，已停止生成结论。',504)
            step['status']='completed'
            return value
        except Exception as exc:
            step['status']='failed'
            step['error']=exc.code if isinstance(exc,ServiceError) else 'tool_failed'
            raise
        finally:
            step['duration_ms']=round((monotonic()-start)*1000)
            store.save_run(self.record)

    def model_gate(self) -> None:
        if self.record['model_calls'] >= self.record['limits']['model_calls']:
            raise ServiceError('model_budget_exceeded','本次运行不允许继续调用模型，请使用明确参数分析。',429)

        self.record['tokens_status']='unknown_until_response'
        self.record['model_calls'] += 1
        self.reserved_calls += 1
        store.save_run(self.record)

    def usage(self, usage: dict) -> None:
        self.record['model_calls'] += max(0,usage.get('calls',0)-self.reserved_calls)
        self.reserved_calls = 0
        if 'total_tokens' in usage:
            self.record['tokens_status']='reported'
        self.record['tokens'] += usage.get('total_tokens',0)
        if self.record['model_calls'] > self.record['limits']['model_calls'] or self.record['tokens'] > self.record['limits']['tokens']:
            raise ServiceError('model_budget_exceeded','模型累计用量超过预算，未执行数据库分析。',429)

    def snapshot(self, status: str='completed') -> dict:
        return {**self.record,'status':status,'duration_ms':round((monotonic()-self.started)*1000)}

    def finish(self, status: str, analysis_id: str | None=None, error: ServiceError | None=None) -> None:
        self.record=self.snapshot(status)
        self.record['analysis_id']=str(analysis_id) if analysis_id else None
        if error:
            self.record['error']={'code':error.code,'message':error.message}
            error.run_id=self.record['id']
        store.save_run(self.record)
