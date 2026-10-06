from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from backend.app.tools.schemas import DateRange, Dimension, Metric, SalesFilters


Strategy = Literal['auto', 'sales_decline', 'customer_contribution', 'product_mix']


class AnalysisIntent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    strategy: Strategy = 'auto'
    action: Literal['trend', 'compare', 'yoy']
    metric_code: Metric = 'sales_amount'
    period: DateRange
    previous_period: DateRange | None = None
    group_by: Dimension = 'month'
    filters: SalesFilters = Field(default_factory=SalesFilters)
    contribution_dimensions: list[Literal['customer', 'product', 'country', 'customer_product']] = Field(default_factory=lambda: ['customer', 'product', 'country'], min_length=1, max_length=4)

    @model_validator(mode='after')
    def validate_comparison(self) -> Self:
        if self.strategy in ('sales_decline','customer_contribution') and (self.action != 'compare' or self.metric_code != 'sales_amount'):
            raise ValueError('销售变化/客户贡献策略只支持销售额两期比较')
        if self.strategy == 'product_mix' and (self.metric_code != 'sales_amount' or self.action not in ('trend','compare')):
            raise ValueError('产品结构策略只支持销售额分布或两期比较')
        if len(set(self.contribution_dimensions)) != len(self.contribution_dimensions):
            raise ValueError('贡献维度不能重复')
        if self.action == 'yoy' and (self.metric_code != 'sales_amount' or self.group_by != 'month' or self.period.start_date.day != 1 or self.period.end_date.day != 1 or self.period.start_date.year <= 1):
            raise ValueError('同比当前只支持销售额、月份维度和完整日历月份，须有可表示的去年同期')
        if self.action == 'compare':
            if self.previous_period is None:
                raise ValueError('比较必须给出基准期间')
            if self.previous_period.end_date > self.period.start_date:
                raise ValueError('基准期间必须早于当前期间，且不能重叠')
        elif self.previous_period is not None:
            raise ValueError('趋势查询不应包含基准期间')
        if self.metric_code == 'customer_count' and self.group_by == 'customer':
            raise ValueError('客户数不支持按客户分组')
        return self


class ParsedQuestion(BaseModel):
    _usage: dict = PrivateAttr(default_factory=dict)
    model_config = ConfigDict(extra='forbid')
    status: Literal['ready', 'needs_clarification']
    intent: AnalysisIntent | None = None
    message: str = Field(default='', max_length=1000)

    @model_validator(mode='after')
    def check_status(self) -> Self:
        if self.status == 'ready' and self.intent is None:
            raise ValueError('ready 必须包含 intent')
        if self.status == 'needs_clarification' and (self.intent is not None or not self.message.strip()):
            raise ValueError('澄清响应必须有问题且不能包含可执行计划')
        return self


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None


class AnalysisFeedback(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    rating: Literal['helpful', 'not_helpful']
    reason: str = Field(default='', max_length=1000)
    correction: str = Field(default='', max_length=2000)


class DrillRequest(BaseModel):
    strategy: Strategy | None = None
    model_config = ConfigDict(extra='forbid')
    filters: SalesFilters | None = None
    metric_code: Metric | None = None
    group_by: Dimension | None = None
    period: DateRange | None = None
    previous_period: DateRange | None = None
    contribution_dimensions: list[Literal['customer', 'product', 'country', 'customer_product']] | None = Field(default=None, min_length=1, max_length=4)
