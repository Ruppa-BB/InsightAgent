# InsightAgent

半导体销售分析项目。当前目标是个人自学型 MVP：用一条真实可运行的销售下降归因链路，学习数据建模、指标语义层、NL2SQL、受控 Agent 和可视化。
后续技术栈：FastAPI、Vue3 + TypeScript + Vite、PostgreSQL。

## 目录

```text
InsightAgent/
├── backend/              # 后续 FastAPI 应用与依赖配置
├── frontend/             # 后续 Vue3 工程
├── data/
│   ├── raw/              # 原始外部数据
│   ├── seed/             # 小规模固定种子数据
│   ├── generated/        # 生成的数据文件（不纳入 Git）
│   └── scripts/          # 后续可重复运行的数据生成与导入脚本
├── database/schema.sql   # 六张核心表、约束、索引
├── docs/DATA_MODEL.md    # 正式模型、关系及指标口径
└── tests/                # 后续数据库、后端测试
```

目录中的 `.gitkeep` 用于保留空目录。本阶段尚无可运行的后端或前端应用。

## 初始化数据库

需要 PostgreSQL 14+ 和 psql。先在本机启动 PostgreSQL，再执行：

```sh
cd /Users/husaifei/InsightAgent
createdb insight_agent
psql -X -v ON_ERROR_STOP=1 -d insight_agent -f database/schema.sql
psql -X -d insight_agent -c '\dt'
```

远程数据库可改用 `psql -d "$DATABASE_URL"`，连接信息通过环境变量传入。
`schema.sql` 在空数据库执行一次，整体处于事务中，遇错回滚；不删除已有数据。
重复执行会因表已存在而失败，后续变更使用迁移文件。

## 学习 MVP 范围

先实现 PostgreSQL 本地数据、5～8 个指标、单用户、一个 LLM、SQL/Metric/Metadata/Visualization Tool、客户/产品/区域归因、图表、会话追问、Trace 和 JSON/Markdown 保存。RAG、PDF、四种外部数据接入、企业权限、Python 任意执行、双模型和完整 Dashboard 后续再做。

当前目标周期为 2 周、14 个工作日，单人每天约 3～5 小时。验收问题是：输入“为什么 2011 年 2 月销售额比 1 月下降？”，系统能真实查询、对账、解释并保存结果。

## 下一步：数据生成

依次导入：区域 → 客户/产品/日期 → 订单 → 订单明细。
建议默认生成 800 客户、300 产品、15 区域。当前公开数据 Demo 使用 2011 年 1 月和 2 月；未来切换模拟数据时再配置目标月份。
先用小样本验证，再扩展订单与明细规模。生成器固定随机种子，记录配置和异常真值。
生成前先按正式数据模型补充订单确认日期迁移，销售期间统一按确认日期统计；现有schema尚不具备该字段。插入日期维度时只提供 `date_id`、可选 `is_holiday`；插入明细时不传四个自动计算指标。
业务编号使用 customer_code/product_code/region_code，关联使用数据库 ID。

## 导入 Online Retail II

当前学习 MVP 使用 UCI Online Retail II。原始文件放在 `data/raw/online_retail_ii/online_retail_II.xlsx`，执行：

```sh
python3 data/scripts/import_online_retail.py \
  --input data/raw/online_retail_ii/online_retail_II.xlsx \
  --output data/generated/online_retail_ii
```

脚本会合并两个工作表，排除取消订单、负数量、缺失客户和无效金额，并输出六个 CSV 和 `import_report.json`。该数据没有独立的订单确认时间，当前学习假设将 `InvoiceDate` 的日期同时映射为 `order_date` 和 `confirmed_date`，报告中会记录这个假设。国家暂时作为区域维度，成本和毛利尚未从公开数据中得到，需要后续单独补充或暂不作为验收指标。

## 验证状态

已检查表依赖、字段、约束和指标公式。当前环境没有 PostgreSQL/psql，
尚未在 PostgreSQL 实例中执行；上述初始化命令是下一步执行验证入口。

## 需求与设计入口

- [产品需求摘要](docs/PRD.md)：原始PRD的MVP范围及两类验收门禁。
- [需求追踪表](docs/REQUIREMENTS_TRACEABILITY.md)：原文章节、页面、接口建议及验收路径。
- [设计决策](docs/DECISIONS.md)：产品要求、设计约定与实现建议的区别。
- [数据模型](docs/DATA_MODEL.md)、[系统架构](docs/ARCHITECTURE.md)、[Agent设计](docs/AGENT_DESIGN.md)。
- [开发计划](docs/DEVELOPMENT_PLAN.md)：18工作日主链目标、完整范围工作包及依赖。
- [本次修订说明](docs/REVISION_NOTES.md)。

当前目录尚未初始化Git仓库。文档描述目标设计，README和实际运行证据描述完成状态；本次修订不代表代码功能已实现。
