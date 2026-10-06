# N3：零售指标与月度同比

2026-10-03，完成新增销量、客单价、加权平均售价和销售额同比。它们采用当前已清洗的Online Retail II有效订单口径，金额单位GBP，不声明已支持制造业成本和毛利。

## 指标定义

| 指标代码 | 含义与公式 | 单位 | 空值处理 |
|---|---|---|---|
| sales_quantity | SUM(quantity)，有效订单商品件数 | items | 无记录聚合返回0，仍提示无匹配数据 |
| average_order_value | SUM(sales_amount) / COUNT(DISTINCT order_id) | GBP/order | 无订单或分母0返回null |
| average_selling_price | SUM(sales_amount) / SUM(quantity)，数量加权 | GBP/item | 无件数或分母0返回null |
| sales_yoy | (本月销售额−去年同月销售额) / 去年同月销售额 | ratio | 覆盖不足、缺月或基期0返回null |

前三个支持month/customer/product/country分组及现有筛选。各组平均值由该组总额与分母重算，不能直接相加或把各组均价简单平均。销量不等于去重订单数；平均售价不是商品单价的简单平均。清洗已排除取消/无效记录，因此不把这些指标称为包含全部退货的净收入。

精确结果保留数据库numeric/Python Decimal，图表按需要展示两位小数。平均值比较可以显示金额差与变化比例；没有有效分母的一期会让比较不可计算，不把null转成0。目前完整维度贡献仍只支持销售额，不把平均值或去重指标做加总贡献。

## 使用

自然语言示例：

- 2011年1月销量是多少
- 2011年1月客单价是多少
- 2011年1月加权平均售价是多少
- 2011年2月客单价比1月变化
- 2011年销售额同比趋势

基础查询通过`POST /api/query`，metric_code使用前三个代码。`GET /api/metrics`列出新增定义。

同比通过`POST /tools/sales-yoy`：

```json
{
  "start_date": "2011-01-01",
  "end_date": "2012-01-01",
  "filters": {"country": "United Kingdom"}
}
```

起止须为每月1日、左闭右开；至少从公元2年开始，查询期最多5年。同比按去年同一日历月份，不按365天偏移，因此正确处理闰年。当前同比仅支持销售额、月份维度。

Agent结构化意图使用`action: "yoy"`、`metric_code: "sales_amount"`、`group_by: "month"`、`previous_period: null`。工具自动获取去年同期，不让模型自行生成同比SQL。返回每月当前/同期金额、比例、baseline_month及null_reason，同时保存查询证据。页面新增客单价和同比示例，图表显示百分比，明细显示双方金额和中文状态；null展示“—”。

## 同比边界

- incomplete_period：当前月或去年同月超出全局实际覆盖边界。
- missing_month：处于覆盖内，但筛选后至少一期无匹配月。
- zero_baseline：去年同月有记录，聚合销售额为0。

先检查期间覆盖，再检查缺月和零分母。全局覆盖范围完整不代表逐日无缺失或特定客户/国家数据完整；完整质量报告留到N4。现有环比工具保持原行为，其完整月份质量约束也在N4进一步统一。

## 验收

真实2011年1月：

| 项目 | 核对结果 |
|---|---:|
| 销量 | 349147 |
| 订单数 | 987 |
| 销售额 | 569445.04 GBP |
| 客单价 | 576.9453292806484296 GBP/order |
| 加权平均售价 | 1.6309607128229657 GBP/item |
| 去年同月销售额 | 557319.06 GBP |
| 销售额同比 | 约+2.18% |

2011年12月只覆盖至12月9日，金额仍可查看，同比必须为null并注明覆盖不足。2005年空范围的平均值聚合为null；不会产生虚假的0均价。

- 75个工程测试通过，含6个真实PostgreSQL集成测试；覆盖原N2反馈兼容、新指标、平均值空比较、同比缺月/零基数/部分月/闰年和筛选传递。
- N3独立8题：规则解析8/8、标准意图执行8/8；逐题参考SQL与结果见`evaluation/n3-offline-baseline.json`。
- DeepSeek仅新增2题（客单价、同比）真实解析2/2，共1191 tokens，未自动重试；标准执行另行统计。见`evaluation/n3-live-sample.json`。
- N1原28题回归仍为规则23/28、标准执行20/20，5个既有规则能力缺口保留。
- 浏览器已验证同比百分比图、全年明细、去年同期金额及12月不可计算状态。实际保存记录ID：04acd173-1fe1-4690-8b82-fff1c8944257；页面验收由结构化工具执行，未额外调用模型。截图位于`data/artifacts/n3-yoy-preview.png`。

运行新增业务评测：

```sh
uv run python -m scripts.evaluate_agent --suite tests/evaluation/metrics-n3.json \
  --output data/artifacts/evaluation-n3.json
```

评测器增加独立同比参考计算与空值比较支持，原28题与基线文件保留；没有用新指标题覆盖旧题。下一任务N4：质量、清洗统计与覆盖报告。
