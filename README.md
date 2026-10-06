# InsightAgent

个人自学型数据分析 Agent MVP。最终产品以原始 PRD 为目标，当前使用 Online Retail II 跑通：提问 → 结构化计划 → 受控 PostgreSQL 工具 → 贡献核对 → 图表和有证据的回答 → 本地保存。

M1（N1～N10）已完成看板、异常、贡献下钻、反馈、历史与月报的学习闭环，见 [N10验收](docs/ACCEPTANCE_N10.md)。当前先扩展数据管理，制造业阶段暂缓。D1～D4资产目录与数据字典已完成，入口 `/data`，见 [目录验收](docs/DATA_CATALOG_D1_D4.md)；后续安排见 [路线与每日任务](docs/ROADMAP.md)。

## 启动

需要 Python 3.13+、uv、已导入数据的 PostgreSQL。已有 `.env` 不要覆盖。

```sh
uv sync
# 第一次配置才创建 .env：参考 .env.example，填入自己的 DATABASE_URL。
uv run uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 使用分析页面，http://127.0.0.1:8000/docs 查看 API 文档。当前为单用户本机应用，无登录或多租户隔离；仅监听本机。

当前本机已配置 `LLM_PROVIDER=deepseek`、`deepseek-flash` 并完成真实模型联调。未设置环境变量的新环境默认 `demo`，这是有限规则解析模式。示例：

- 为什么 2011 年 2 月销售额比 1 月下降？
- 2011 年销售额趋势
- 2011 年 2 月订单数比 1 月变化
- 完成分析后追问：再看英国 / 再看客户 18102 / 再看全部国家

演示解析器不认识的表达会要求澄清，不能悄悄忽略用户的筛选条件。新的问题可用“新的分析”清空上下文。会话追问继承最近一次成功分析的期间、指标和筛选；当前不保存尚未完成的澄清对话。

## 接入真实模型

在已有 `.env` 追加配置，重启服务。不要把密钥贴到聊天或提交 Git。

```dotenv
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=自己的密钥
LLM_MODEL=服务商支持JSON响应的模型名称
LLM_TIMEOUT_SECONDS=30
```

DeepSeek 使用 `LLM_PROVIDER=deepseek`、`LLM_BASE_URL=https://api.deepseek.com`、`LLM_MODEL=deepseek-flash`。密钥仅保存在忽略的 `.env`。

省钱设置：关闭思考模式，`LLM_MAX_OUTPUT_TOKENS=700`（默认上限），`LLM_CACHE_SECONDS=3600`；相同问题、上下文、模型配置在服务进程中缓存最多 128 条解析，重启后清除。缓存只复用计划，数据库每次重新查询。明确的英国/客户筛选追问本地处理，0 次模型调用。一次新问题通常 1 次调用，失败不自动重试或升级模型；最终回答和图表由工具结果生成，不再调用模型。响应 `model_usage` 记录本次 tokens 和是否命中缓存。700 是上限，不是每次固定消耗。

适配器使用 Chat Completions 的 JSON response_format；兼容服务商需支持此协议。模型只接收问题、上一轮结构化意图和输入 schema；不发送数据库凭证或查询结果。模型返回的结构化计划经 Pydantic 校验后才执行；不接受模型生成的 SQL。最终数字和解释来自确定性模板及工具结果。没有密钥、模型报错或计划无效时会明确报错，不会自动切换演示模式。

DeepSeek Flash 已通过两条真实问题解析与本机数据库主链验收。固定编排根据解析计划选择趋势或比较工具；自由多轮规划、任意问题和开放式分析仍不支持。

## 已实现能力

- 指标：销售额、去重订单数、去重客户数、销量、客单价、加权平均售价；独立月度销售额环比及同比工具。新增公式与覆盖规则见 [N3指标说明](docs/METRICS_N3.md)。
- 时间：开始日包含、结束日不包含，最长 5 个日历年，支持闰日边界。
- 维度：月、客户、商品、国家；筛选：国家名、客户编码、商品编码。
- 查询保护：固定 SQL 白名单、绑定参数、只读事务、默认 5 秒语句超时、最多 500 行及截断提示。
- 一次分析使用同一数据库快照；销售额按客户/商品/国家完整聚合贡献，各自对账。
- 新出现/消失对象均计入贡献；单维超过 10000 组、客户×商品组合超过 100000 组直接拒绝归因，避免漏算。图表只展示主要贡献，其余差额单列。
- 空记录、零基数、覆盖不完整、期间天数不同均提示；金额用 Decimal 计算，JSON 用字符串保留精度。
- 页面展示趋势、两期对比、贡献图、数据表、执行记录和 SQL。历史存储在本地 SQLite，可重新打开和导出 JSON/Markdown。

贡献描述“哪些客户/商品/国家的金额变化解释了总差额”，不证明促销、流失等业务因果。不同维度解释同一个差额，不能跨维度相加。订单数/客户数为去重指标，不提供跨商品加总归因。

新增提问示例：`2011年1月销量是多少`、`2011年1月客单价是多少`、`2011年1月加权平均售价是多少`、`2011年销售额同比趋势`。同比没有完整同期数据时返回空值并提示。

## 数据质量与变化检测

页面顶部“质量与异常”提供当前质量检查、历史清洗数量、月度覆盖和两种销售变化检测，可配置阈值与窗口并下载当次JSON快照。当前规则失败会停止可信分析；不完整月份不参与异常判断。缺失率保持未知，不把无交易天数当缺失。详情见 [N4/N5说明](docs/QUALITY_ANOMALIES.md)。

## 分析反馈

分析结果下方可以选择“有帮助 / 无帮助”，填写原因与修正建议。反馈按分析ID保存，历史重开可查看和更新；重复保存相同内容不会增加修订。反馈仅用于后续评估，不自动修改SQL、口径或结论，也不调用模型。接口及验收见 [N2反馈说明](docs/FEEDBACK.md)。

## 标准问题评测

N1已建立28题基线，规则解析、标准意图执行和真实模型小样本分别统计。运行 `uv run python -m scripts.evaluate_agent`，不调用模型；当前5个规则解析失败项如实保留，完整离线运行会返回退出码1。题目、参考SQL、用量及真实联调方法见 [评测说明](docs/evaluation/README.md)。

## API

| 接口 | 用途 |
| --- | --- |
| `GET /health`、`GET /health/db` | 应用/数据库检查 |
| `GET /api/metrics`、`GET /api/metadata` | 指标与数据覆盖 |
| `POST /api/query` | 查询，返回行数、截断状态、SQL、参数与来源 |
| `POST /tools/sales-query` | 兼容早期列表响应，响应头 X-Result-Truncated 指示截断 |
| `POST /tools/sales-mom` | 完整月份销售额环比，输入 start_date/end_date |
| `GET /sales/trend` | 兼容早期月度三指标接口 |
| `POST /api/analyze` | question 和可选 session_id，自然语言分析 |
| `POST /api/analyze/structured` | 直接提交结构化意图，不调用模型 |
| `GET /api/analyses` | 最近 30 条分析 |
| `GET /api/analyses/{id}` | 读取结果 |
| `GET /api/analyses/{id}/export/{json或md}` | 导出 |

结构化请求示例：

```json
{
  "action": "compare",
  "metric_code": "sales_amount",
  "period": {"start_date": "2011-02-01", "end_date": "2011-03-01"},
  "previous_period": {"start_date": "2011-01-01", "end_date": "2011-02-01"},
  "filters": {"country": "United Kingdom"}
}
```

输入错误返回 422；不存在的会话/记录返回 404；数据库不可用返回 503；语句超时返回 504；模型错误返回 502；对账失败返回 409。错误响应不暴露数据库凭证。

## 验证

```sh
uv run python -m pytest -q
# 显式连接真实本机 PostgreSQL；业务数据不会被修改。
INSIGHT_DB_TESTS=1 uv run python -m pytest tests/test_database_integration.py -q
# 实际运行主问题并保存一条分析记录：
uv run python -m scripts.verify_mvp
```

基准：2011 年 1 月 £569,445.04，2 月 £447,137.35，差额 -£122,307.69，变化约 -21.48%。详细状态见 [MVP 验收记录](docs/MVP_VALIDATION.md)。

## 数据准备（已有库跳过）

先在空数据库执行 `database/schema.sql`；原始 Excel 放在 `data/raw/online_retail_ii/online_retail_II.xlsx`。清洗脚本依赖 pandas/openpyxl，可用独立临时环境执行：

```sh
uv run --with pandas --with openpyxl python data/scripts/import_online_retail.py \
  --input data/raw/online_retail_ii/online_retail_II.xlsx \
  --output data/generated/online_retail_ii
psql -X -v ON_ERROR_STOP=1 -U postgres -d insight_agent -f database/import_online_retail.sql
```

清洗取消订单、无效金额、缺失客户等记录。InvoiceDate 同时映射 order_date 和 confirmed_date，币种 GBP。没有真实成本数据，禁止用填充成本计算毛利。已导入 36,975 订单、805,620 明细，总销售额 £17,743,429.16。数据库模型保留六表；应用代码中取销售来源为订单/明细和三个实体维度。

## 代码阅读路径

```text
backend/app/config.py                 环境配置
backend/app/db.py                     只读数据库快照与超时
backend/app/semantic/metrics.py       指标语义
backend/app/tools/schemas.py          输入模型
backend/app/tools/sql_allowlist.py    安全查询与截断提示
backend/app/tools/analysis_tool.py    完整贡献与对账
backend/app/tools/mom_tool.py         日历月环比
backend/app/agent/provider.py         规则/真实模型意图解析
backend/app/agent/service.py          规划、工具调用、验证、回答
backend/app/agent/store.py            本地历史与导出
backend/app/main.py                   FastAPI 路由
backend/app/static/                   无构建依赖的最小页面
```

当前前端为 FastAPI 托管的 HTML/CSS/JavaScript，后续再迁移 Vue。业务库只读，分析历史写入 `data/artifacts/analyses.sqlite3`（Git 忽略）。`.venv`、`.env`、原始数据不提交；`pyproject.toml`、`uv.lock` 应跟随代码版本。

## 资料库

- [Day 1–5 学习复盘](docs/learning-notes/day01-05-project-learning-review.md)
- [后续 MVP 代码导读](docs/learning-notes/day06-14-mvp-implementation.md)
- [第一阶段开发计划](docs/DEVELOPMENT_PLAN.md)
- [产品需求](docs/PRD.md)、[需求追踪](docs/REQUIREMENTS_TRACEABILITY.md)
- [数据模型](docs/DATA_MODEL.md)、[最终架构设计](docs/ARCHITECTURE.md)、[Agent 设计](docs/AGENT_DESIGN.md)

文档中的最终产品设计不等于当前 MVP 已实现范围。

### N6 经营总览

首页自动显示最新完整月份的六项指标、最近月度销售趋势、当月Top客户/商品与变化提醒。点击提醒可直接比较上月并核对贡献，无模型调用。口径与验收见 [经营总览说明](docs/DASHBOARD_N6.md)。

### N7 连续下钻

分析页可修改期间/筛选/指标、按贡献对象下钻、启用客户×商品组合贡献并返回父分析。明确参数及精确追问不调用模型，组合独立对账。详见 [N7说明](docs/DRILL_N7.md)。

### N8 受控策略与运行记录

新增销售变化、客户贡献、产品结构策略；每次分析累计步骤/耗时/模型用量，失败停止并记录。分析页可选择策略并展开「运行策略、预算与步骤」。详见 [N8说明](docs/STRATEGIES_N8.md)。

### N9 月度经营报告

点击「经营报告」，按完整月份生成八部分月报，保存同次报告查询的数据快照。可重开、下载Markdown及JSON，重复生成保留新版本，不调用模型。详见 [N9说明](docs/REPORTS_N9.md)。

## 最新验收

2026-10-03：121项测试通过（13项真实数据库集成）；N1标准计划执行20/20、规则解析23/28，仍有5个已知表达缺口；N3指标题解析和执行各8/8。完整证据和限制见 [N10验收](docs/ACCEPTANCE_N10.md)。工程测试数量不代表自然语言准确率，零售学习版不代表最终制造业PRD全范围完成。

D5～D7质量管理已完成：入口 `/data/quality`，支持配置版本、检查历史、有限问题样例与同登记版本比较。通知阈值不放宽Agent门禁；全量130测试通过（15数据库集成），见 [质量管理说明](docs/QUALITY_MANAGEMENT_D5_D7.md)。

D8～D11导入批次已完成：入口 `/data/imports`，本地约定结构Excel/CSV登记、指纹去重、独立进程隔离导入与核验发布。分析页可选择已发布版本，分析和月报记录批次来源；全量136测试通过（16数据库集成）。原零售基线保留，详见 [批次验收与边界](docs/IMPORT_BATCHES_D8_D11.md)。

D12～D14指标版本已完成：入口 `/data/metrics`，支持草稿、发布、停用与不可变历史；分析/月报保存实际口径并可回到原版本。固定计算模板，145项测试通过（17数据库集成）；见 [指标治理说明](docs/METRIC_MANAGEMENT_D12_D14.md)。下一D15～D16血缘。

D15～D16数据血缘已完成：入口 `/data/lineage`，按数据/口径版本追溯源文件、批次、五表与保存的分析/月报，查看上下游影响；缺少登记明确标记。见 [血缘说明与验收](docs/LINEAGE_D15_D16.md)。下一D17～D18质量问题闭环。

D17～D18质量问题闭环已完成：入口 `/data/issues`，自动建单与去重、处理说明、人工豁免、原版本复检和有证据关闭；失败复检不能关闭，人工豁免不解除 Agent 门禁。见 [质量问题说明](docs/QUALITY_ISSUES_D17_D18.md)。D19～D20整体验收与交付整理已完成，见 [整体验收](docs/DATA_MANAGEMENT_ACCEPTANCE_D19.md) 与 [启动演示手册](docs/DATA_MANAGEMENT_HANDOFF_D20.md)。完整回归163项通过（20数据库集成）。
