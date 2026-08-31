# AGENTS.md

Prospector 是查证型深度调研系统，不是通用 Agent 框架。
系统规则与已知实现差异见 [design](docs/design.md)，环境与操作见 [usage](docs/usage.md)，
前端补充约定见 [web/AGENTS.md](web/AGENTS.md)。

## 修改原则

- 遵循 design 中的范围与术语；改变系统行为时同步设计和测试，不把已知实现差异当成正确规则。
- 采用满足需求的最简单方案。不加兼容旧流程的分支、未经要求的兜底逻辑或未来功能。
- 沿输入、执行、持久化到交付检查完整链路；测试验证实际行为，不依赖模型的固定措辞，不为通过测试削弱断言。

## 代码边界

- Python 3.13，`src/` 布局，行宽 100。数据结构在 `schemas/`，模型调用在 `agents/`，流程编排在 `flow/`。
- `deterministic/` 与 `reporting/` 不调用 LLM；`flow/` 不导入 `runtime/`，图状态必须可序列化。
- 抓取保存 Document 和 DocumentView；Worker 选择 `source_ref` 后，由 `save_findings` 原子保存 Excerpt 与 Assertion。全文不进模型上下文，模型摘要不能充当原文。
- Writer 写正文，Attribution 核验，代码生成引用；不得绕过研究核验和报告审查，也不得为未验证陈述生成已验证引用。
- 事实与行文修订共享两轮预算。报告核验结果与 Job 状态分开；核心问题耗尽修订后为 `failed`，仅有非核心问题为 `partial`，均可交付。
- 不提交 `.env`；日志不得记录密钥、连接串或研究正文。观测服务故障不得阻断 checkpoint。

## 检查

在仓库根目录运行：

```bash
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run pytest tests/unit -q
uv run pytest tests/integration -q
```

集成测试自行创建独立 Compose 项目，每个用例使用临时数据库与存储桶并清理；只需 Docker 可用，
不要加载开发 `.env`。普通测试禁止外网请求。`tests/live` 需要显式 `--live` 才会执行，会产生费用。
