"""Explicit drill changes inherit only validated saved intent, never model output SQL."""
from uuid import UUID

from backend.app.agent import store
from backend.app.agent.schemas import AnalysisIntent, DrillRequest
from backend.app.agent.service import execute_intent
from backend.app.errors import ServiceError


def derive_intent(parent: AnalysisIntent, change: DrillRequest) -> AnalysisIntent:
    data = parent.model_dump(mode='json')
    updates = change.model_dump(mode='json', exclude_none=True)
    data.update(updates)
    if change.strategy is None and (change.metric_code is not None and change.metric_code != parent.metric_code or change.contribution_dimensions is not None):
        data['strategy'] = 'auto'
    # filters is an explicit complete replacement, enabling clear/reset operations.
    if change.metric_code is not None and change.metric_code != parent.metric_code:
        if parent.action == 'yoy':
            data.update(action='trend', previous_period=None, group_by='month')
        if change.metric_code == 'customer_count' and data['group_by'] == 'customer':
            data['group_by'] = 'month'
        if change.metric_code != 'sales_amount':
            data['contribution_dimensions'] = ['customer','product','country']
    if change.previous_period is not None and parent.action != 'compare':
        raise ValueError('仅比较分析可以修改基准期间')
    return AnalysisIntent.model_validate(data)


def drill_analysis(analysis_id: UUID, change: DrillRequest) -> dict:
    saved = store.get(analysis_id)
    if saved is None:
        raise ServiceError('not_found','父分析不存在。',404)
    intent = derive_intent(AnalysisIntent.model_validate(saved['intent']), change)
    return execute_intent(intent, '连续下钻：'+intent.metric_code, UUID(saved['session_id']),
                          'structured', parent_id=analysis_id)
