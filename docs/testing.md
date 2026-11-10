# 测试与评测

测试源码是项目可靠性设计的一部分，不随仓库整理删除。目录结构保持稳定，评测器会复用 `tests.fakes` 及明确允许的工程测试。

| 分类 | 主要文件 | 验证目标 |
| --- | --- | --- |
| 图与证据 | `tests/test_agent.py`、`test_evidence_completion.py` | 正确消息循环、预算、引用绑定 |
| 业务规则与数据库 | `test_policy.py`、`test_pg_constraints.py` | 截止边界、金额、约束 |
| 审批与事务 | `test_proposal_approval.py`、`test_request_workflow.py`、`test_transaction_execution.py` | 旧方案拦截、回滚、并发、未知提交 |
| MCP | `test_mcp_integration.py`、`test_mcp_api.py` | 真实 stdio、只读权限、错误传播 |
| 页面与管理 | `test_streamlit_workflow.py`、`test_request_management.py` | 草稿、归档、清除保护、按需查询 |
| 运行与演示 | `test_launcher.py`、`test_deployment.py`、`test_demo_local.py` | 启动恢复、隔离演示与进程保护 |
| 评测设施 | `test_evaluation.py`、`test_evaluation_closure.py` | 数据隔离、判分、脱敏、预算 |

表中后续文件名均位于 `tests/`。测试默认禁止真实模型调用；真实数据库测试必须显式配置独立 `_test` 库与不同应用/reader 角色，通过随机 schema 隔离。缺配置会失败，不自动使用业务库、不退回 SQLite。

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=short
.\.venv\Scripts\python.exe -m scripts.evaluate_offline
.\.venv\Scripts\python.exe -m scripts.evaluate validate
```

测试连接使用进程环境 `TEST_DATABASE_URL`、`TEST_READONLY_DATABASE_URL`，或本地忽略的 `.local/test-database.env`。部署类测试额外需要独立 bootstrap 连接，创建后仅清理自身命名的临时数据库与角色。不要将正式库地址用作测试地址。

## 两份场景数据的区别

- `evaluation/v1`：原始已审核冻结集，120条；原始正式成绩不被后续修复覆盖。
- `evaluation/public`：原冻结集加三条已单独审核修订的组合回归快照；包含审批绑定与复现入口。它是已接触回归集，不是新未见集。

```powershell
# 独立PG测试环境准备好后，运行无模型费用回归；使用新的run-id。
.\.venv\Scripts\python.exe evaluation/public/reproduce.py --mode offline --split dev --run-id dev-local-01
.\.venv\Scripts\python.exe evaluation/public/reproduce.py --mode offline --split test --run-id test-local-01
```

`reproduce.py --mode live` 会使用自己的模型并消耗额度，需理解并准备评测预算账本；README 演示无需此命令。原始模型轨迹、个人审核原件、预算账本和完整源码快照只留本地，不上传。公开指标见[评测报告](../evaluation/public/report.md)，本次整理的运行检查见[整理验收](repository_readiness.md)。
