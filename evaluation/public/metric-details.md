# 分项指标与分母

真实模型参与的系统回归，不是未见测试。原始意图提取只统计实际观察到triage的案例，规则直达不计模型理解。

| 指标 | 正确 / 分母 |
| --- | --- |
| 原始提取 intent | 39/39 |
| 原始提取 order_id | 39/39 |
| 原始提取 reason | 39/39 |

原始意图宏F1：1.0；观察案例39，覆盖标签3。完整系统宏F1另见report.md。

| 系统最终断言 | 正确 / 适用分母 |
| --- | --- |
| amount | 28/28 |
| analysis_creates_no_ticket | 50/50 |
| confirmation_idempotent | 28/28 |
| confirmed_ticket_verified | 28/28 |
| currency | 28/28 |
| deadline | 28/28 |
| draft_confirmation_no_ticket | 7/7 |
| evidence | 50/50 |
| intent | 50/50 |
| kind | 50/50 |
| missing_fields | 50/50 |
| no_duplicate_ticket | 28/28 |
| no_wrong_ticket | 28/28 |
| order_id | 50/50 |
| orders_and_policies_unchanged | 50/50 |
| reason | 50/50 |
| status | 50/50 |
| tool_arguments | 50/50 |
| tools | 50/50 |
| turn_1 | 6/6 |

结构首次通过：{'observed': {'correct': 38, 'denominator': 39, 'rate': 0.9743589743589743}, 'all_model_cases': {'correct': 38, 'denominator': 50, 'rate': 0.76}, 'unobserved': 11}；修复后：{'observed': {'correct': 39, 'denominator': 39, 'rate': 1.0}, 'all_model_cases': {'correct': 39, 'denominator': 50, 'rate': 0.78}, 'unobserved': 11}。这是场景全链路trace结构校验，不是每次HTTP首次格式成功率。
分析时延毫秒：{'p50': 12353.760399972089, 'p95': 23881.66990003083}；单请求时延：{'p50': 2369.8659000219777, 'p95': 6755.894400062971}。
请求162；输入168039，输出11473token。

工具指标含系统必要证据补读及无工具场景的空调用判断，不声称原始模型调用级选择100%正确。工程安全断言和未确认零写入与人工回复质量分开报告。人工选用输出50/50满分；本轮新整批回复0/50评分。
