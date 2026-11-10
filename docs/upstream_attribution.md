# 上游与本项目增量

本项目从 [m-peker/ecommerce-ai-agent](https://github.com/m-peker/ecommerce-ai-agent) 二次开发，基线 commit 为 `3d38d5ab7fa0f27bd5c28488afa354abaf2577b4`。阶段一在用户本机完成运行测试，后续继续在同一仓库实施，没有重新克隆覆盖。

上游提供 FastAPI、Streamlit、LangGraph 的基础项目结构、订单／物流示例和可选 Langfuse 接线。上游 README 声明 MIT；该基线树没有独立 LICENSE 文件，本文件保留来源与提交归属，不自行补写作者或版权年份。

| 复用部分 | 当前处理 |
| --- | --- |
| FastAPI／Streamlit／LangGraph 栈与目录职责 | 沿用，加入 Proposal 审核与工单工作台 |
| 用户已验证的供应商 model/base URL | 保持现有配置，默认测试不调用供应商 |
| 用户 Windows `.venv\Scripts\python.exe` 启动修复 | 保留，launcher 文案更新为 AftercarePilot |
| 订单／物流业务概念与编号示例 | 重新构造固定时刻合成 PG seed，物流明确标 simulated |
| 可选 Langfuse | 保留；初始化／flush 异常不阻塞核心 PG 工作流 |
| 旧 SQLite、七工具路径、直接 initiate_return | 删除，不作为当前能力或数据库回退 |
| 上游截图、架构图片、medium.md | 阶段四按用户指令移除，避免展示已弃用能力 |

本项目新实现包括五张 SQLAlchemy／PostgreSQL 业务表与 Alembic、签收窗口和整数金额规则、证据保存、结构化 Proposal、不可变 revision/hash 审核、后端行锁与原子工单事务、并发防重和提交对账、三个只读 FastMCP 工具及客户端、Streamlit 审批卡和真实 PG/MCP/FakeLLM 验证。

阶段五增加了 Dockerfile／三服务 Compose、显式部署初始化、单独 reader 角色、带哈希的依赖锁、官方镜像 digest、容器 API 地址、API 就绪反馈、隔离验收镜像与真实 HTTP 重启验证脚本。镜像 registry 解析、配置验证与本机进程验证已有记录；完整 Compose 实际启动结果以 [整理验收](repository_readiness.md) 为准。

这是内部合成数据演示。requested 表示售后申请已记录；没有支付退款、客户邮件、物流面单或真实生产接入。FakeLLM 测试验证规则、证据和控制流，不能当成 live 模型回答准确率、客户满意度或业务提升。
