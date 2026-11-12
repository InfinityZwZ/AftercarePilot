# AftercarePilot 售后处理 Agent 工作台

基于开源电商 Agent 二次开发的本地售后工作台，将客户问题转为有订单与政策证据的处理方案，经操作员确认后创建售后申请。

**流程：新建请求 → 分析问题 → 查看与修改方案 → 确认并创建工单 → 核验结果。**

技术栈：Python · LangGraph · Pydantic · MCP / FastMCP · FastAPI · PostgreSQL · SQLAlchemy / Alembic · Streamlit。

本项目当前没有公开在线体验地址。无需租赁服务器即可浏览源码与截图，或在本机运行演示。它是本地单人工作台，不提供真实退款、消息发送、物流执行或多人权限体系。

## 界面与操作

V1.1.1 在请求详情页增加审核跳转，将订单查找与补录放在同一首屏，并标注客户原话、AI 提取、业务查询和规则判断的来源。申请原因可能与原话不一致时，页面提示人工核对。

截图来自 1440 × 900 浏览器验收，使用隔离 PostgreSQL、合成订单、FakeLLM、固定 Clock 和真实只读 MCP。

| 可建单：核对资格与依据 | 可建单：确认操作后果 |
| --- | --- |
| ![待确认建单](docs/screenshots/v1.1.1/A-eligible-first.png) | ![建单确认弹层](docs/screenshots/v1.1.1/A-confirmation-dialog.png) |

| 未签收：仅确认回复草稿 | 缺少订单号：查找并补录 |
| --- | --- |
| ![待确认回复及来源提示](docs/screenshots/v1.1.1/B-ineligible-first.png) | ![待补充订单号](docs/screenshots/v1.1.1/C-missing-order-first.png) |

回复草稿始终标记“尚未发送”。申请已受理只表示工单已记录，不代表退款或退货已经完成。提交响应不确定时查询原结果，不自动重新申请。

## 架构与可信边界

```mermaid
flowchart LR
  UI[Streamlit 工作台] --> API[FastAPI 认证与业务入口]
  API --> PS[ProposalService 版本与方案]
  PS --> AG[LangGraph 只读分析]
  AG --> MCP[stdio MCP 子进程]
  MCP -->|reader 只读角色| PG[(PostgreSQL 五张业务表)]
  PS -->|问题 方案 审计| PG
  API --> AS[AftersalesService 确认与建单]
  AS -->|事务重读 审批 工单 成功审计| PG
```

- **受控 Agent**：triage、decide、tools、validate 四类节点；正确保留 AIMessage / ToolMessage 配对，限制调用次数和时间。
- **三个只读工具**：get_order、get_return_policy、check_return_eligibility。资格和金额由代码判定，模型不能自批或建单。
- **结构与证据**：Pydantic 约束输出，校验引用、参数和快照；必要只读证据最多补齐一次，不伪造工具结果。
- **不可变版本**：保存草稿修改生成新 revision，审批绑定内容 hash，旧页面和被替代版本不能执行。
- **事务与防重**：锁方案、订单和政策，确认时重读实时事实；本次审批、工单、执行状态和成功审计同事务提交。唯一约束与幂等回读保护重复请求。
- **请求管理**：归档、恢复、当页批量整理、受保护的单条清除；游标分页、部分索引、历史证据按需加载。

[详细架构](docs/architecture.md) · [测试设计](docs/testing.md)

## 可核验的评测结果

V1.1.1 使用 FakeLLM 和隔离 PostgreSQL 完成全量工程回归，412 项通过，1 项既有 Authlib 弃用警告。隔离浏览器验证了建单并回读、仅确认回复、补录后生成新版三条流程。

下表保留 2026 年 11 月 7 日归档的评测结果。

| 指标 | 结果 | 限制 |
| --- | --- | --- |
| 合成场景 | 120条，Dev40 / Test80 | 不是120条真实模型测试 |
| 历史工程回归 | 388/388通过 | FakeLLM＋真实PG/MCP/页面，含参数化 |
| 完整业务回归 | 50/50通过 | 39条调用模型，11条规则直达 |
| 场景结构首次校验 | 38/39，97.4% | 不是HTTP逐请求首次成功率 |
| 修复后结构校验 | 39/39 | 不等于全部新回复已人工评分 |
| 建单结果及幂等防重 | 28/28 | 适用建单场景分母 |
| 完整分析耗时 | P50 12.35秒 / P95 23.88秒 | 含规则路径和多轮，不是线上SLA |

模型配置为 GLM-5.3 低思考。系统规则、必要证据补读和受限纠正参与结果，测试集已用于诊断修复，不能称未见准确率或“模型准确率100%”。历史选用回复50条已获认可，新整批50条回复尚未整体人评。

[公开评测摘要](evaluation/public/report.md) · [机器可读指标](evaluation/public/metrics.json) · [场景与复现](evaluation/README.md)

## 本机快速体验

准备 Python 3.13、运行中的本地 PostgreSQL 17 和依赖。演示不需要自己的模型 API Key：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 新安装PG的用户设置自己的本地bootstrap连接；不要提交真实密码。
$env:SHOWCASE_BOOTSTRAP_DATABASE_URL='postgresql+psycopg://postgres:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/postgres'
.\.venv\Scripts\python.exe -m scripts.demo_local start --open
```

已有本项目本地 bootstrap 凭据时可省略上述环境变量。浏览器地址为 `http://127.0.0.1:18701`，示例输入：**ORD-1009 的蓝牙音箱坏了，我想退货。** FakeLLM只演示规定流程，不用于任意自然语言测试。

```powershell
.\.venv\Scripts\python.exe -m scripts.demo_local status
.\.venv\Scripts\python.exe -m scripts.demo_local stop
# 确需重新体验同订单时，先停止，再显式清除仅本入口拥有的演示数据。
.\.venv\Scripts\python.exe -m scripts.demo_local reset --confirm-reset
```

[完整运行说明](docs/local_run.md) · [两分钟演示步骤](docs/demo.md)

## 测试与目录

```powershell
# 先配置独立_test数据库与app/reader角色，缺配置会明确失败。
.\.venv\Scripts\python.exe -m pytest -q --tb=short
.\.venv\Scripts\python.exe -m scripts.evaluate_offline
```

| 目录 | 内容 |
| --- | --- |
| src/ | Agent、工具、领域规则、API、事务服务和页面 |
| tests/ | 单元、真实PG/MCP、页面和故障回归 |
| alembic/ | 五表与管理字段索引迁移 |
| scripts/ | 启动、演示、检查和评测入口 |
| evaluation/ | 原冻结集、公开回归快照与精选指标 |
| docs/ | 架构、运行、测试与截图 |

用 `AftercarePilot.code-workspace` 打开 VS Code 可隐藏本地缓存与运行目录。依赖、数据库、密钥、原始模型轨迹及个人面试材料不上传。容器配置已提供，完整Docker运行和数据卷恢复仍待引擎实测，不冒称生产部署。

## 来源与二次开发

基于 [m-peker/ecommerce-ai-agent](https://github.com/m-peker/ecommerce-ai-agent)，上游基线 `3d38d5ab7fa0f27bd5c28488afa354abaf2577b4`。二次开发涉及PG统一事实、三个只读MCP工具、结构化方案、人工确认与事务工单、请求管理及评测闭环。

上游README声明MIT，但基线树缺少独立LICENSE文件。本仓库保留来源说明，不编造版权归属或年份；此说明不替代缺失的许可原文。详见[上游与增量](docs/upstream_attribution.md)。
