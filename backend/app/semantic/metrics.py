METRICS = {
    "sales_amount": {
        "name": "销售额",
        "description": "订单明细中的商品销售收入",
        "formula": "SUM(fact_sales_detail.sales_amount)",
        "unit": "GBP",
        "default_filter": "fact_sales_order.order_status = 'completed'",
        "dimensions": ["month", "customer", "product", "country"],
    },
    "order_count": {
        "name": "订单数",
        "description": "有效订单的去重数量",
        "formula": "COUNT(DISTINCT fact_sales_order.order_id)",
        "unit": "orders",
        "default_filter": "fact_sales_order.order_status = 'completed'",
        "dimensions": ["month", "customer", "product", "country"],
    },
    "customer_count": {
        "name": "客户数",
        "description": "有效订单中的去重客户数量",
        "formula": "COUNT(DISTINCT fact_sales_order.customer_id)",
        "unit": "customers",
        "default_filter": "fact_sales_order.order_status = 'completed'",
        "dimensions": ["month", "product", "country"],
    },
    "sales_quantity": {'name': '销量', 'description': '有效订单中的商品件数之和', 'formula': 'SUM(fact_sales_detail.quantity)', 'unit': 'items', 'default_filter': "fact_sales_order.order_status = 'completed'", 'dimensions': ['month', 'customer', 'product', 'country'], 'zero_division': 'not_applicable'},
    "average_order_value": {'name': '客单价', 'description': '销售额除以有效去重订单数，不按订单明细数计算', 'formula': 'SUM(sales_amount) / COUNT(DISTINCT order_id)', 'unit': 'GBP/order', 'default_filter': "fact_sales_order.order_status = 'completed'", 'dimensions': ['month', 'customer', 'product', 'country'], 'zero_division': 'return_null'},
    "average_selling_price": {'name': '加权平均售价', 'description': '销售额除以商品件数，按数量加权，不是单价的简单平均', 'formula': 'SUM(sales_amount) / SUM(quantity)', 'unit': 'GBP/item', 'default_filter': "fact_sales_order.order_status = 'completed'", 'dimensions': ['month', 'customer', 'product', 'country'], 'zero_division': 'return_null'},
    "sales_yoy": {'name': '销售额同比', 'description': '本月销售额相对去年同月的变化比例，缺少完整期间数据或零基数返回null', 'formula': '(本月销售额 - 去年同月销售额) / 去年同月销售额', 'unit': 'ratio', 'dimensions': ['month'], 'zero_division': 'return_null'},
    "sales_mom": {
        "name": "销售额环比",
        "description": "本月销售额相对上月销售额的变化比例",
        "formula": "(本月销售额 - 上月销售额) / 上月销售额",
        "unit": "ratio",
        "default_filter": "fact_sales_order.order_status = 'completed'",
        "dimensions": ["month"],
        "zero_division": "return_null",
    },

}


def get_metric(metric_code: str) -> dict | None:
    # A pinned execution sees exactly the definitions captured at its start.
    from backend.app.data_management.metric_versions import PINNED
    from backend.app.errors import ServiceError
    definitions = PINNED.get()
    metric = (definitions if definitions is not None else METRICS).get(metric_code)
    if metric and not metric.get('enabled', True):
        raise ServiceError('metric_disabled', f'指标 {metric_code} 已停用。', 409)
    return metric
