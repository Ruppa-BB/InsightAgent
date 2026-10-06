# Day 1–5 学习复盘：从零售数据到受控查询 API

> 本篇保留 Day 5 时的学习快照；后续已实现内容见 [Day 6–14 导读](day06-14-mvp-implementation.md)。

这份笔记把项目最初五天的学习串成一条线：先准备可信的数据，再让 Python 服务连接数据库，最后把业务指标包装成一个受限、可调用的查询工具。它对应个人自学型 MVP 的早期基础；完整自然语言 Agent 还需要后续接入模型和编排。

## 五天做了什么

| 天数 | 主要工作 | 得到的结果 |
| --- | --- | --- |
| Day 1 | PostgreSQL 数据库与分析模型 | 建好 Online Retail II 的维度表、事实表，明确数据口径 |
| Day 2 | 原始数据清洗与转换 | 将源数据处理成可以导入维度表和事实表的 CSV |
| Day 3 | 导入与数据核验 | 将数据放入 PostgreSQL，用 SQL 核对行数、销售额、月度和客户结果 |
| Day 4 | Python 项目环境与 API | 使用 uv 管理环境和依赖，FastAPI 连上 PostgreSQL 并提供健康检查、趋势接口 |
| Day 5 | 语义定义与受控查询工具 | 定义指标/维度白名单，通过 API 参数化查询并返回结果 |

## Day 1：数据库和数据模型

### 为什么需要数据库

Online Retail II 是原始零售交易数据。应用需要可靠地保存它，并能按时间、客户、商品和地区进行查询。PostgreSQL 负责持久化和执行分析 SQL；Python 服务稍后通过数据库连接访问它。

### 事实表和维度表

- **事实表**记录业务事件和可聚合数值，例如订单、订单明细、数量、销售额。
- **维度表**保存用于描述和切分事实的实体，例如客户、商品、地区。
- 事实表通过 ID 关联维度表。查询销售额时读事实表；想按客户名称展示时，再关联客户维度表。

项目采用订单事实 `fact_sales_order` 和订单明细事实 `fact_sales_detail`，配合客户、商品、地区等维度表。销售额来自明细粒度，因此销售额要从订单明细汇总；订单数和客户数要使用去重计数。

### 数据口径

导入过程中，原始 `InvoiceDate` 被用于订单日期和 `confirmed_date`。分析时还固定了订单状态口径：只把 `completed` 订单纳入销售统计。数据集没有成本字段，因此只能分析销售额，不能据此计算真实毛利。

口径需要明确记录，是因为 Agent 将来必须能解释“这个数字怎么算的、包含什么、不包含什么”。

## Day 2：数据清洗和导入准备

原始文件不能总是直接用于分析。清洗脚本负责将源字段整理成项目数据模型能够接收的记录，识别取消订单或无效行，并生成与数据库表结构匹配的 CSV 文件。

清洗不是为了让数据“看起来整齐”，而是明确哪些记录进入分析、字段如何映射、不同业务实体如何获得键值。若源数据的订单号、客户号或商品描述不能直接作为项目内部关系，就需要在转换阶段建立稳定映射。

这一步的关键检查问题是：

- 原始记录数、清洗后记录数和被排除记录数是否能解释？
- 订单明细是否能关联到订单、商品和客户？
- 金额是否按业务定义计算？
- 导出文件的列和数据库表是否一致？

## Day 3：导入 PostgreSQL 和数据验证

### 导入与事务

CSV 通过 SQL 脚本导入 PostgreSQL。一次导入包含建表、加载维度和事实数据、建立关联等步骤。用事务包住导入操作时，如果中间步骤失败可以回滚，避免留下半成品数据。

首次导入时曾因客户维度重复键冲突而失败。根因是导入数据中客户键并非唯一，数据库主键约束阻止了重复记录。清理客户映射后重新导入，最终表行数为：

| 表 | 行数 |
| --- | ---: |
| `dim_region` | 41 |
| `dim_customer` | 5,881 |
| `dim_product` | 4,631 |
| `fact_sales_order` | 36,975 |
| `fact_sales_detail` | 805,620 |

### 用 SQL 做验收

数据导入成功不代表数据正确。用 SQL 独立核对事实表行数、涉及订单数、总销售额、月度销售额和客户销售额。明细总销售额是 £17,743,429.16，36,975 个订单都有明细；月度与客户查询结果也能正常返回。

这些 SQL 查询构成后续 Agent 的可复算依据：工具返回的结果应与直接查询 PostgreSQL 一致。

## Day 4：uv、FastAPI 和数据库连接

### uv 和虚拟环境

`uv` 用来管理 Python 项目的依赖和虚拟环境。`.venv` 是项目自己的 Python 包环境；激活后运行 `python` 或 `uv run` 会使用这个项目环境中的解释器和包。这样项目依赖与系统 Python 分开，也可以通过 `pyproject.toml` 和 `uv.lock` 记录依赖，便于重建环境。

项目使用 `uv add` 管理 FastAPI、SQLAlchemy、psycopg、pydantic-settings 等依赖，用 `uv run` 在项目环境中执行 Python 命令。

### FastAPI 的工作方式

FastAPI 用路由把 HTTP 方法和 URL 映射到 Python 函数。例如 `@app.get("/health")` 表示收到 GET `/health` 时执行对应函数。函数返回的字典或列表会被序列化为 JSON。FastAPI 也会根据路由和 Pydantic 模型生成 `/docs` 交互式 API 文档。

Day 4 加入了 `/health`、`/health/db` 和 `/sales/trend`。前两个检查应用和数据库是否可用；趋势接口执行月度销售 SQL 并返回 JSON。

### Engine、连接和数据库驱动

SQLAlchemy **Engine** 是应用访问数据库的入口和连接资源管理器。它根据数据库 URL 和 psycopg 驱动连接 PostgreSQL，并管理连接池。调用 `engine.connect()` 临时取出一个连接；使用 `with` 块后连接会被归还。psycopg 是 PostgreSQL 驱动，SQLAlchemy 在其上提供统一的数据库执行接口。

连接地址和密码放在 `.env` 的 `DATABASE_URL` 中，不应提交到 Git。`pydantic-settings` 从环境配置读取该值。

## Day 5：语义指标、SQL 白名单和查询 API

### 指标：要计算什么

指标是业务上关心的数值及其定义。项目目前的 SQL 指标有：

| 指标代码 | 计算方式 | 含义 |
| --- | --- | --- |
| `sales_amount` | `SUM(d.sales_amount)` | 销售额 |
| `order_count` | `COUNT(DISTINCT o.order_id)` | 去重订单数 |
| `customer_count` | `COUNT(DISTINCT o.customer_id)` | 去重客户数 |

指标的业务描述、单位、默认过滤和可用维度定义在 `backend/app/semantic/metrics.py`；实际 SQL 表达式在 `backend/app/tools/sql_allowlist.py` 的 `METRIC_SQL`。`sales_mom` 已有文字语义定义，但 SQL 计算还没有实现。

### 维度：从什么角度拆分指标

维度是分析和分组的角度，例如月份、客户、商品、国家。指标回答“算什么”，维度回答“按什么角度分开看”。销售额按月就是每个月分别汇总销售额；不指定维度则是日期范围内的一个总数。

`DIMENSION_SQL` 为每个维度规定 SQL 字段表达式和所需 JOIN。月份直接取订单日期，不需 JOIN；客户、商品和国家需要关联各自维表。商品分组包含商品编码和名称，避免不同 SKU 因描述相同而合并。

### Pydantic：校验工具输入

`SalesQueryRequest` 描述查询工具接受的输入：指标代码、开始日期、结束日期、可选分组维度和行数上限。Pydantic 会把日期字符串解析为 Python `date`，并检查 `group_by` 是否属于允许项、`limit` 是否在 1 到 500 之间。

### 白名单：限制可执行的 SQL 结构

构造 SQL 前，程序检查 `metric_code` 在不在 `METRIC_SQL`，检查 `group_by` 在不在 `DIMENSION_SQL`。只有允许项能被选中。未来 LLM 可以提出参数，但不能让它任意编写 SQL；后端白名单是执行边界。

### 参数化查询：把 SQL 和数据值分开

查询把日期和行数写成占位符：

```sql
o.confirmed_date >= :start_date
AND o.confirmed_date < :end_date
LIMIT :limit
```

执行时单独传入参数字典。数据库会把它们作为值处理，而不是 SQL 代码。指标表达式和维度表达式属于 SQL 结构，不能用普通值参数替代，所以由代码中的固定白名单控制。

日期采用左闭右开范围。查 2011 年第一季度，开始值为 `2011-01-01`，结束值为 `2011-04-01`；包含 1、2、3 月，不包含 4 月。

### SQLAlchemy 执行与结果转换

`build_sales_query()` 根据已校验的指标和维度生成 `text()` SQL。`run_sales_query()` 组织绑定参数，用 `engine.connect()` 取得连接，再调用 `connection.execute(query, params)`。结果通过 `result.mappings()` 按列名读取，再转成普通字典。

销售额使用 `SUM` 汇总；`COUNT(DISTINCT ...)` 计算去重订单数或客户数；有分组时使用 `GROUP BY` 聚合，`ORDER BY` 稳定排序。订单与明细事实表通过 `order_id` 关联；只统计 `completed` 订单。

### FastAPI 查询路由和 JSON

`POST /tools/sales-query` 接受查询 JSON。FastAPI 将请求体解析为 `SalesQueryRequest`，然后路由调用 `run_sales_query()` 并返回结果。可以在 `/docs` 的 Try it out 中测试。

Python 的 `date` 和数据库金额 `Decimal` 会被 FastAPI 序列化为 JSON 字符串，例如 `"2011-01-01"`、`"569445.04"`，这是正常响应格式。

## Day 5 实际验证

请求按月统计 2011-01-01（含）到 2011-04-01（不含）的销售额，返回：

| 月份 | 销售额（GBP） |
| --- | ---: |
| 2011-01 | 569445.04 |
| 2011-02 | 447137.35 |
| 2011-03 | 595500.76 |

这与此前直接查询数据库的月度结果一致。到这里，请求校验、白名单 SQL 生成、参数绑定、数据库执行、FastAPI 返回 JSON 的链路已经打通。

## 五天连起来看

```text
Online Retail II 原始数据
  → 清洗和字段映射
  → 导入 PostgreSQL 事实表/维度表
  → SQL 核验数据口径和结果
  → FastAPI + SQLAlchemy 访问数据库
  → 语义指标与维度定义
  → 校验请求并从白名单生成参数化 SQL
  → 返回查询结果 JSON
```

每一步都在为 Agent 铺路：如果原始数据与口径不可信，回答就不可信；如果没有语义目录，模型不知道指标含义；如果没有受控工具，模型不能安全地查真实数据。

## 当前阶段边界

现在完成的是“可控的指标查询 API 工具”，还不是完整的自然语言 Agent。调用者仍要明确给出 `metric_code` 和 `group_by`，LLM 尚未负责解析问题和选择工具；`sales_mom` SQL、归因核对、图表、结果保存和自然语言证据回答也属于后续任务。无分组聚合查询天然只返回一行，无需额外 LIMIT。日期空值会被范围比较过滤掉；后续仍需补充数据覆盖与缺失提示。

## 复习顺序

1. 从 Day 1 看数据模型：数值存在哪里，客户/商品/地区如何关联。
2. 从 Day 2–3 看清洗和验数：哪些数据进入分析，如何证明导入正确。
3. 从 Day 4 看 `/sales/trend`：FastAPI 怎样连数据库并返回 JSON。
4. 从 Day 5 看 `SalesQueryRequest`、指标/维度白名单、查询执行和新路由。
5. 打开 `/docs` 改日期、指标或分组，预测结果后再实际执行。

## 一句话记忆

前四天让数据和后端 API 准备就绪；第五天让后端能按受控指标与维度查询数据。接下来再学习如何让 LLM 理解自然语言、选择这个工具，并用工具结果给出可核对的分析。
