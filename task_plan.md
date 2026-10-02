# 文档整体修订计划

目标：以用户提供的 PRD v1.0 为来源，修订 docs 和 README，保持需求、设计、验收及排期一致。

- [x] 核对原始 PRD、现有文档及 schema
- [x] 修订需求来源、销售口径、页面交付与版本方案
- [x] 重排开发依赖并补充需求追踪表
- [x] 独立阅读审查、链接和一致性检查

约定：文档阶段已完成；Day1～3 已开始执行。当前两周计划使用 UCI Online Retail II，确认日期采用 InvoiceDate 的明确学习假设。

检查记录：当前目录没有 Git 仓库，不能提供 git diff；通过文件内容和链接检查验证修订。

完成：docs七份原文档和README修订，新增需求追踪表及修订说明。24处本地链接通过检查，独立读者未发现实质矛盾，措辞建议已修正。

Day1～3进度：schema 增加 confirmed_date；新增 Online Retail II 清洗导入脚本；已生成六个 CSV 和 import_report.json；下一步是 PostgreSQL 导入或无数据库环境下继续完成对账 SQL。
