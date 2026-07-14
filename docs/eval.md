# Prospector 评测设计

- **版本**：1.1
- **日期**：2026-07-14
- **状态**：M3 实现合同；M1 只预留数据与录制接口
- **依据**：[设计文档](./design.md)、[M1 实现设计](./implementations/m1.md)、[路线图](./implementations/roadmap.md)

---

## 1. 目标

评测回答四个问题：

1. 报告中的事实 Claim 是否忠实于引用的 Excerpt。
2. 最终证据和报告是否履行最终 Plan 的执行承诺。
3. Planner 的取舍和最终报告是否仍对齐用户确认的 Brief。
4. 质量提升是否以可接受的 token 和工具成本取得。

Brief 是研究输入快照，不是覆盖合同。评测不得把 Scope 展开的候选方向重新解释为运行时必达清单。执行履约只对照版本化 Plan；Brief 只用于判断偏题和遗漏核心问题。

---

## 2. 评测原则

### 2.1 裁判与选手分离

系统自己的 Planner、Verifier 和 Claim Verifier 都不能充当最终真值。发布门禁的真值来源只能是：

- 确定性程序可判的不变量；
- 人工标注的 Excerpt—Claim 对；
- 人工审定的 Brief 核心问题与意图；
- 经人工校准通过的模型裁判。

### 2.2 稳定世界

不同系统版本必须在同一题目、同一冻结 Brief、同一工具磁带下比较。磁带未命中显式失败，不允许临时出网改变评测世界。

### 2.3 指标分层

机器不变量、人工真值、模型裁判和过程诊断分开记录。过程指标只用于归因，不得替代正确性门禁。

---

## 3. 四层评测

### 3.1 A：确定性不变量

每次运行直接检查：

| 检查 | 通过条件 |
|------|----------|
| 引用权威链 | Claim→ClaimEvidence→Excerpt→Document version 外键和哈希全部有效 |
| Claim 成文门 | 未通过验证的事实 Claim 不进入报告 |
| Excerpt 精确性 | Excerpt 是对应 Document 快照的确定性切片 |
| Plan 版本 | 任务只能通过 Planner 决策或 Verifier 触发的 Replan 进入新版本 |
| 空手结束 | 零 Excerpt 时 `finish` 被拒绝；决策轮耗尽则失败 |
| derived 深度 | ClaimPremise 深度不超过 2 |
| 冲突处置 | 高优先级冲突存在覆盖它的 ConflictResolution |
| FigureSpec | M2 起只绑定已验证 Claim 或 Computation，不含未绑定字面数值 |

这些是 bug 指标，任何一项失败都直接阻断发布。

### 3.2 B：人工真值与语义评测

#### Claim 忠实度

人工标注 `(claim, excerpt)` 是否支持、是否矛盾、是否证据不足。经该集合校准的裁判运行在完整评测集上，输出 `claim_faithfulness`。NFR-2 的 ≥95% 从 M3 起以此为依据。

#### Plan 承诺履约

对每次运行的最终 Plan，逐项判断：

- 对应 ResearchTask 是否实际执行；
- `expected_evidence` 是否有落库证据支撑；
- 报告是否使用或明确说明未完成的承诺；
- Verifier 是否漏掉不可接受的重大缺口。

输出 `plan_commitment_coverage` 和 `major_gap_recall`。Plan 是运行时执行合同，因此评测读取该次运行实际生成的最终 Plan，不在题库里预造固定任务清单。

#### Brief 对齐

建题时人工标注 Brief 的核心问题、决策目的、边界与明确排除项；这些标注只存在于评测集。裁判比较最终 Plan 和报告，判断：

- 是否偏离核心问题；
- 是否遗漏会改变答案的核心关切；
- 是否把候选探索方向机械当成全部必达；
- 是否违反范围边界。

输出 `brief_alignment`。该指标不产生运行时硬清单。

#### 报告质量

候选版本与基线版本做成对比较，交换 AB/BA 位置各评一次；不一致计平。输出 `win_rate_vs_baseline`，只作观察指标。

### 3.3 C：组件 Golden Set

| 组件 | 固定输入 | 判断点 |
|------|----------|--------|
| Scope | 原始问题与澄清答案 | Brief 是否具体、开放且保持用户意图 |
| Planner | Brief、任务台账、断言投影、决策日志、预算余额 | 决策是否合法；取舍是否合理；是否偏题 |
| Research Verifier | Brief、最终 Plan、Evidence Store | Plan 履约、重大缺口、偏题与冲突判断 |
| Claim Verifier | Claim、Excerpt、Premise | 支持/矛盾/不足判断与 derived 校准 |
| no-new-facts | 已验证 Claim 集与报告句子 | 是否检出集合外事实 |

组件回归用于定位 E2E 退化发生在哪个判断点，不替代 E2E 门禁。

### 3.4 D：过程诊断

从 trace、events 和 usage 聚合：

- Planner 决策轮分布、`dispatch/reflect/finish` 比例和格式错误率；
- Replan 次数与 Plan 版本数；
- Worker 工具调用数、连续无新证据停止率、工具失败率；
- Claim 驳回率、derived 深度、冲突处置分布；
- no-new-facts 补录比例；
- token、工具调用和成本。

这些指标不设发布门禁，只用于解释质量和成本变化。

---

## 4. 录制回放

### 4.1 磁带合同

评测适配层记录工具请求和响应，键为 `(tool, 规范化参数哈希)`：

- M1：`web_search`、`web_fetch`；`save_findings` 是本地落库动作，不录外部响应。
- M2：增加 `kb_list`、`kb_structure`、`kb_read` 与沙箱外部输入。

录制模式真实调用外部服务并写磁带；回放模式只读磁带。磁带按 `(eval_set_version, cassette_version)` 绑定，版本不同的运行不得直接比较。

### 4.2 评测入口

评测通过 brief-direct 提交题库中已经人工审定的 Research Brief 输入快照，经 schema 校验后进入与 interactive 相同的研究主图。生产 API 不暴露录制或回放参数；评测能力只存在于独立评测部署。

---

## 5. 裁判校准

每个模型裁判必须绑定人工校准集和版本：

1. Claim 忠实度、Plan 承诺履约和 Brief 对齐裁判先在人工标注集上测一致率。
2. 裁判模型或提示词变化必须重新校准。
3. 未达一致率要求的裁判不得参与发布门禁。
4. 报告整体质量始终只做成对比较，不转成绝对分门禁。

---

## 6. 指标与门禁

| 档位 | 指标 | 真值来源 | 用途 |
|------|------|----------|------|
| 门禁 | `claim_faithfulness ≥ 0.95` | 人工真值集校准过的裁判 | 阻断事实失真 |
| 门禁 | `citation_chain_valid = 1.0` | 确定性代码 | 阻断血缘错误 |
| 门禁 | `plan_commitment_coverage` 不回归，`major_gap_recall` 达标 | 最终 Plan + 人工校准裁判 | 阻断执行合同漏履约 |
| 门禁 | 每分质量 token 成本不超预算 | usage + 质量指标 | 阻断成本回归 |
| 观察 | `brief_alignment` | 人工审定 Brief 核心意图 + 校准裁判 | 发现偏题与机械全覆盖 |
| 观察 | `win_rate_vs_baseline` | 成对比较裁判 | 比较整体报告质量 |
| 观察 | 组件与过程指标 | Golden Set / trace / events | 定位原因 |

具体阈值随 M3 首次校准集一起锁定；在人工真值建立前不提前声明数字。

---

## 7. 题库

每道题包含：

```text
原始问题
+ 人工审定并冻结的 Research Brief 输入快照
+ 评测侧核心意图标注
+ 工具磁带
+ 可选的 Claim/Excerpt、冲突或私库位置真值
```

题库按事实核查、对比、综述和深度研究分层，并覆盖：

- 冲突题：检查并陈或可审计裁决；
- 无解题：检查是否诚实报告重大缺口；
- 过度推理题：检查 derived Claim 的深度与措辞；
- 时效题：控制公开 benchmark 的搜索污染；
- M2 私库题：标注 Document 与页码/行号位置。

M3 首版每类至少 10 题；持续轨道扩到每类至少 30 题。

---

## 8. `eval_run`

每次运行 append-only 保存：

```json
{
  "eval_run_id": "er_042",
  "eval_set_version": "es_v3",
  "cassette_version": "cas_v3.1",
  "system_version": "git:abc1234",
  "k_runs": 3,
  "metrics": {
    "claim_faithfulness": 0.962,
    "citation_chain_valid": 1.0,
    "plan_commitment_coverage": 0.93,
    "major_gap_recall": 0.95,
    "brief_alignment": 0.94,
    "win_rate_vs_baseline": 0.58,
    "cost_per_quality": 1.12
  },
  "gate_result": "pass",
  "created_at": "2026-07-14T00:00:00Z"
}
```

单题流程：

```text
建题：问题 → Scope 产出 Brief → 人工审定输入快照与核心意图
录制：真实跑完整主图 → 保存工具磁带 → 抽取 Claim/Excerpt 供人工标注
回归：同一磁带运行候选版本 → 机器检查 → 语义裁判 → 成对比较
判决：聚合门禁与观察指标 → 写 eval_run
维护：裁判重新校准；题库或磁带升级时提升版本
```

---

## 9. 里程碑衔接

| 阶段 | 评测侧交付 |
|------|------------|
| M1 | 权威链机器检查、工具录制钩子、brief-direct；完成后从真实 Claim 对启动小规模人工标注 |
| M2 | 增加 Computation、FigureSpec 与私库位置检查 |
| M3 | 题库、磁带、人工真值集、裁判校准、`eval_run`、发布门禁与看板 |
| 持续轨道 | 扩题、重录时效磁带、校准裁判、滚动更新基线 |

M4 只能在 M3 的正确性门禁可稳定执行后开始。
