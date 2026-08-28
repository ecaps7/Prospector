# Prospector 实现路线图

- **版本**：2.0
- **依据**：[设计文档](../design.md) §11、[M0 实现设计](./m0.md)、[M1 实现设计](./m1.md)、[CLI 文档](../cli.md)
- **用途**：定义里程碑范围、依赖顺序和完成出口。机制细节由设计文档与对应实现设计负责，本文不建立第二套合同。

---

## 1. 总览

| 里程碑 | 状态 | 唯一目标 |
|--------|------|----------|
| M0 | **已实现** | 建立可恢复、可观测、可测试的工程基座 |
| M1 | **已实现** | 一次交付完整单机深研系统与 CLI |
| M2 | 未开始 | 增加沙箱计算、本地文档和图表能力 |
| M3 | 未开始 | 建立可重复评测、人工真值门禁和看板 |
| M4 | 未开始 | 将已验证的单机系统扩展为多用户运行时 |

```mermaid
flowchart LR
    M0["M0 工程基座<br/>已实现"] --> M1["M1 深研智能体核心 + CLI<br/>已实现"]
    M1 --> M2["M2 计算与本地文档"]
    M2 --> M3["M3 评测基建"]
    M3 --> M4["M4 多用户运行时"]
```

---

## 2. 排期原则

1. **先锁定不可逆数据，再接控制流**：首次抓取起就保存 Document 快照和精确 Excerpt；checkpoint 从 M0 起常开。
2. **研究主图一次成形**：M1 合并主干只接受 Planner 决策环、并行 Worker、Verifier/Replan、Claim 验证和成文审计组成的完整图，不存在单 Worker、无 Verifier 等可交付中间形态。
3. **执行承诺只来自 Plan**：Brief 负责展开研究空间；Planner 负责取舍并形成版本化 Plan；Verifier 对照 Plan 检查履约，并以 Brief 检查偏题。
4. **预算与验证分离**：`effort` 映射 Planner 决策轮，以及各研究阶段的并发数与 Worker 决策轮；工具调用总数、token 和累计工具调用只计量。停止研究不能绕过 Research Verifier。
5. **验收必须可度量**：M1–M2 使用机器检查和行为测试；M3 建成人工真值集后才启用定量质量门禁。
6. **运行时后置**：先验证单机研究逻辑，再在 M4 引入多进程、队列和多用户调度。

---

## 3. 代码组织

```text
prospector/
├── migrations/                 # Alembic
├── docs/
├── src/prospector/
│   ├── schemas/                # Pydantic 合同，零业务逻辑
│   ├── store/                  # PostgreSQL、对象存储、checkpoint
│   ├── tools/                  # 外部能力适配与录制钩子
│   ├── agents/                 # LLM 判断
│   ├── deterministic/          # 预算、门禁、引用、血缘检查；LLM 禁入
│   ├── flow/                   # LangGraph 主图
│   ├── runtime/                # HITL、时间线、进程入口
│   ├── api/                    # 单进程 FastAPI + SSE
│   ├── obs/                    # 日志、trace、usage
│   ├── reporting/              # 确定性渲染
│   └── cli/                    # HTTP/SSE 瘦客户端
└── tests/
```

依赖方向固定为：

```text
schemas
   ↑
store / tools / deterministic / obs
   ↑
agents
   ↑
flow
   ↑
api / runtime / cli
```

`deterministic/` 不调用 LLM，`flow/` 不导入 `runtime/`。目录按架构边界组织，不按里程碑拆包。

---

## 4. 已完成里程碑

### M0：工程基座

实现设计：[m0.md](./m0.md)

Python 工程骨架、CI、Alembic、PostgreSQL 与 LangGraph PG checkpointer、structlog、OpenTelemetry、对象存储，以及空流程 kill/resume 集成验证。

完成标准：checkpoint 恢复不重跑已完成节点；日志可关联 trace/span；对象存储读写和 CI 门禁通过。

### M1：深研智能体核心 + CLI

实现设计：[m1.md](./m1.md)（含分阶段实现记录、数据模型、主图与验收清单）

研究图从冻结 Brief 运行到终态 `draft_rendered`，产出 `verified` 或 `partial` 的 Markdown/JSON。已交付：

1. interactive 与 brief-direct 产生同一 Research Brief 输入快照并进入同一主图。
2. Planner 每轮强制输出 `dispatch` / `reflect` / `finish`，记录决策日志并生成版本化 Plan。
3. 运行时按 `Brief.effort` 注入 Planner 决策轮、批次并发与 Worker 决策轮，并把可用动作、工具和具体上限直接反馈给模型（具体数值见 [m1.md](./m1.md)）。
4. 并行通用 Research Worker 只使用 `web_search` / `web_fetch` / `save_findings`，联网来源统一使用持久化 Exa highlights。
5. Document 快照、Excerpt、Assertion、Plan、Verifier run、Claim 与报告产物全链路落库。
6. Research Verifier 检查 Plan 承诺、Brief 偏题、缺口与冲突；可补缺口在仍有决策预算时回到 Planner 形成新 Plan 版本。
7. Report Verifier 逐句验证；初稿与句级修订全量重验，阶段二失败后的修订只重跑整篇核验；修订触顶后失败句保留在 `partial` 产物中且不生成已验证引用角标。
8. 单进程 API、PG 事件 SSE、usage/span 和 CLI `ask → attach → report` 闭环。

完成标准（逐条判据见 [m1.md](./m1.md) §12）：端到端产出带引用报告且权威链机器校验 100%；多 Worker 并行不串号；预埋缺口产生 Plan 新版本并补搜；空手 `finish` 被拒绝；决策轮耗尽且零 Excerpt 直接失败；修订触顶产出 `partial` 并显式列出失败 statement；CLI 可提交、跟踪并查看或导出报告。

---

## 5. 后续里程碑

均为设计草案，尚无实现。

| 里程碑 | 范围 | 完成标准 | 设计 |
|--------|------|----------|------|
| **M2** 计算与本地文档 | Data Worker 沙箱、Computation 输入血缘与复现检查、computed Claim；PageIndex 入库建树与 `kb_*` 三原语；FigureSpec 确定性图表渲染 | 无 Excerpt 血缘的输入不能计算；计算可复现；本地文档 locator 完整；FigureSpec 不含未绑定字面数值 | [设计 §3.4 / §4.10 / §5.5](../design.md) |
| **M3** 评测基建 | 题库、录制回放磁带、人工真值集、裁判校准、`eval_run` 与评估看板。**M4 的前置门** | 同一磁带下不同系统版本可比较；Claim 忠实率由人工真值衡量；质量与成本门禁可执行 | [docs/future/eval.md](../future/eval.md) |
| **M4** 多用户运行时 | API 多副本、单写者 Orchestrator、Worker 池、RabbitMQ 三队列、幂等消费、Redis Stream SSE、跨进程 trace | 多用户并发公平；任意进程崩溃后任务可恢复；SSE 可跨副本回放；跨进程 trace 可关联 | [docs/future/runtime-scaleout.md](../future/runtime-scaleout.md) |

M2 的计算链与本地文档链相互独立，可按人力并行或对调。三项研究限制在 M4 仍由任务与 Planner 状态执行；PG 保存权威 usage，Redis 不参与研究终止判断。

---

## 6. 全程硬规则

| 规则 | 起点 |
|------|------|
| checkpoint 常开，图状态只存可序列化数据 | M0 |
| Document 快照与精确 Excerpt 从首次抓取起不可省略 | M1 |
| Brief 不是覆盖合同，Plan 才是执行合同 | M1 |
| Planner 决策轮、按 effort 注入的批次并发与 Worker 决策轮是研究硬闸 | M1 |
| 停止研究不能绕过 Verifier、Claim 验证或成文审计 | M1 |
| Computation 必须记录代码、输入血缘和输出 | M2 |
| 定量忠实率门禁必须以人工真值为依据 | M3 |
| 研究逻辑不依赖多用户运行时组件 | 全程 |

里程碑范围变化时，同步修改设计文档的里程碑表和对应实现设计；不得通过保留旧合同兼容分支解决文档冲突。
