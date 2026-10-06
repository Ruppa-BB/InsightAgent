from calendar import monthrange
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Metric = Literal['sales_amount', 'order_count', 'customer_count', 'sales_quantity', 'average_order_value', 'average_selling_price']
Dimension = Literal['month', 'customer', 'product', 'country']


class DateRange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start_date: date
    end_date: date

    @model_validator(mode='after')
    def check_date_range(self) -> Self:
        if self.start_date >= self.end_date:
            raise ValueError('start_date 必须早于 end_date')
        # Python date only represents years through 9999.
        if self.start_date.year <= 9994:
            year = self.start_date.year + 5
            last_day = monthrange(year, self.start_date.month)[1]
            boundary = self.start_date.replace(year=year, day=min(self.start_date.day, last_day))
            if self.end_date > boundary:
                raise ValueError('查询日期范围不能超过 5 年')
        return self


class SalesFilters(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    country: str | None = Field(default=None, min_length=1, max_length=100)
    customer_code: str | None = Field(default=None, min_length=1, max_length=20)
    product_code: str | None = Field(default=None, min_length=1, max_length=20)


class SalesQueryRequest(DateRange):
    metric_code: Metric
    group_by: Dimension | None = None
    limit: int = Field(default=100, ge=1, le=500, strict=True)
    filters: SalesFilters = Field(default_factory=SalesFilters)


class SalesYoYRequest(DateRange):
    filters: SalesFilters = Field(default_factory=SalesFilters)


class AnomalyRequest(DateRange):
    filters: SalesFilters = Field(default_factory=SalesFilters)
    mom_drop_threshold: float = Field(default=0.20, gt=0, le=1, allow_inf_nan=False)
    moving_average_threshold: float = Field(default=0.20, gt=0, le=5, allow_inf_nan=False)
    moving_average_window: int = Field(default=3, ge=2, le=12, strict=True)

    @model_validator(mode='after')
    def calendar_months(self) -> Self:
        if self.start_date.day != 1 or self.end_date.day != 1 or self.start_date.year <= 1:
            raise ValueError('异常检测使用完整月份范围，起止日期须为每月1日，且需有可表示的历史月份。')
        return self
