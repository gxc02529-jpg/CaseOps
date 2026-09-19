# CaseOps 代码复用与业务改造说明

本项目采用“复用稳定模式、重写业务语义”的方式补齐源码。这样可以解释现有 RAG 与 Agent 代码如何演进为工单系统，同时避免把物流问答或旅游 Agent 直接改名后伪装成新业务。

## 复用判断

| 来源 | 复用内容 | CaseOps 落点 | 改造原因 |
| --- | --- | --- | --- |
| KnowForge `qa_core/governance/data_scope.py` | 不可变数据域、租户过滤、检索边界 | `governance/scope.py` | 工单还需要订单范围与审批角色，不能照搬知识库 dataset 语义 |
| KnowForge `qa_core/retrieval/ranking.py` | 候选去重、分数排序、reranker 注入 | `retrieval/hybrid.py` | 保留纯逻辑可测试性，替换为工单证据实体与订单范围过滤 |
| KnowForge `qa_core/pipeline/citations.py` | 答案必须带可追溯依据 | `providers/openai_compatible.py` | 结构化输出只允许引用本轮已检索 evidence ID |
| Agent 示例 `03-agent/05multi_agent.py` | 专业节点分工与结果汇总 | `workflow/graph.py` | 从演示式并行 Agent 改成可审计的条件状态机 |
| A2A 串行示例 | 明确任务状态和节点顺序 | `application/service.py` | 工单需要失败可定位、审批可暂停、每个用户独立派单 |

## 新写的业务能力

- 语义、组件、版本、时间窗口联合判断同源工单。
- `SINGLE`、`PIPELINE`、`CLARIFY` 三种执行模式。
- 退款、删数、改权等敏感动作的人工审批闸门。
- “研判合并、处置拆分”的案件与工单关系。
- 关单生成候选知识，人工审核后发布到检索集合。
- 工单、案件、审批和知识事件的统一审计轨迹。

## 生产替换点

| 本地参考实现 | 生产实现建议 |
| --- | --- |
| `InMemoryCaseStore` | PostgreSQL Repository，按 `infra/postgres/schema.sql` 落表 |
| `HybridRetriever` 的词项与重叠分数 | Milvus HNSW + BM25 + BGE-M3，再接 BGE-Reranker |
| `RuleBasedDiagnoser` | `OpenAICompatibleDiagnoser` 对接 DeepSeek |
| 进程内对象状态 | Redis 保存幂等键、短期状态与知识版本 cache epoch |
| 请求头模拟身份 | API 网关验证 JWT 后注入可信租户、角色与订单范围 |

仓库刻意把这些边界做成可替换组件，而不是在本地测试中启动大型模型与中间件。这使核心业务规则可快速验证，也让基础设施接入保持独立。

