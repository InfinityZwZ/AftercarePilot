# 合成评测与复现

- [公开结果与边界](public/report.md)
- [机器可读指标](public/metrics.json)
- [逐项分母](public/metric-details.md)
- [测试环境与命令](../docs/testing.md)

`v1/cases.jsonl`是原始120条合成目录，`v1/frozen.json`和审核记录保留原预期；没有覆盖原始失败成绩。`public/cases.jsonl`为原目录加三条已单独审核修订的组合回归快照，`approved-dataset.json`绑定完整审核内容。两者都不是生产客户数据。

开发40条（25模型、15工程），测试80条（50模型、30工程）。场景家族不跨划分；真模型只看问题和工具结果，不读标准答案。测试集用于修复后仅作回归，不能重新称未见评测。

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate validate
.\.venv\Scripts\python.exe evaluation/public/reproduce.py --mode offline --split dev --run-id dev-local-01
.\.venv\Scripts\python.exe evaluation/public/reproduce.py --mode offline --split test --run-id test-local-01
```

离线运行仍需独立真实PG和真实只读MCP，不调用模型。真实模型模式需要自己的配置和预算，可能收费；面试演示无需运行live。原始轨迹、历史逐轮结果、个人评分原件、源码快照及累计预算账本保留在本机忽略目录，不作为GitHub上传内容。
