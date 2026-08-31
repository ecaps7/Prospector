# Prospector

Prospector 是查证型深度调研系统：从一个问题出发，规划研究任务、检索公开网页、
核对材料并形成分析，最后生成带引用的 Markdown 报告和审计 JSON。
可以通过 Web 界面或命令行使用。

它不仅收集资料，也尝试解释材料之间的关系。事实核验与写作分开进行，
引用由代码生成，并关联到保存的原文片段及来源版本。
这些机制方便回查，不保证每个判断都正确。

## 工作方式

1. **确认问题。** 系统展开研究简报（Brief），必要时澄清；你确认后才创建研究任务。
2. **搜集与核验。** Planner 安排任务，Worker 检索、抓取并保存选用的证据；
   Research Verifier 判断材料能否回答问题，必要时要求补充研究。
3. **分析与写作。** Research Synthesis 整理材料之间的关系，Writer 写出报告正文。
4. **检查与交付。** Attribution 核对事实出处，Review 检查整体回答，Readthrough 检查行文。
   最多修订两轮后，由代码生成引用和报告文件。

任务执行完成与报告核验通过是两回事。报告可能是 `verified`、`partial` 或 `failed`；
三种结果都可以交付，未验证陈述不会获得已验证引用。
当前实现的具体限制与已知差异见 [设计文档](docs/design.md)。

## 快速开始

需要 Python 3.13+、uv、Docker Compose，以及可用的模型服务和 Exa 密钥。
使用 Web 界面还需要 Node.js 22.12+ 与 npm。以下命令均从仓库根目录执行。

首次创建配置；已有 `.env` 时不要覆盖：

```bash
cp .env.example .env
```

填写 `.env` 中的 `PROSPECTOR_LLM_BASE_URL`、`PROSPECTOR_LLM_API_KEY`、`EXA_API_KEY`，
并确认两个 `PROSPECTOR_LLM_MODEL_*` 模型名在你的服务中可用。
当前模型适配支持 DeepSeek、Qwen 系列的请求参数，不承诺任意兼容接口都能直接使用。
详细配置见 [使用说明](docs/usage.md#2-首次准备)。

初始化后端：

```bash
uv sync --frozen
docker compose up -d --wait
uv run --env-file .env alembic upgrade head
uv run --env-file .env prospector-local setup
```

构建 Web 界面：

```bash
npm --prefix web ci
npm --prefix web run build
```

启动服务并保持该终端运行：

```bash
uv run --env-file .env prospector serve
```

打开 [本机 Web 界面](http://127.0.0.1:7620/)，输入问题，阅读并确认研究方案。
后续使用不必重复安装和初始化；前端有改动时需要重新构建。

也可以另开终端，通过同一服务交互提问：

```bash
uv run --env-file .env prospector --effort standard --language zh
```

不启动服务、直接在本地运行，以及查看进度、取消、恢复和导出报告的方法，
见 [使用说明](docs/usage.md)。

## 当前范围

- 面向本机单用户；服务一次执行一个 Job，后续提交排队，Job 内的研究任务可以并行。
- 只采集公开网页，不支持上传附件、私有知识库、代码沙箱、数据计算或图表生成。
- 研究会调用外部模型与检索服务，产生时间和费用；投入档位不是固定耗时或费用承诺。
- 同一问题重复运行可能得到不同结果；服务重启也不会自动恢复中断的研究。
- 当前 HTTP API 没有身份认证，不要将本地服务或附带的数据库、对象存储直接暴露到公网。

## 开发与检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run pytest tests/unit -q
uv run pytest tests/integration -q
```

集成测试只需 Docker 可用：自动启动独立 PostgreSQL/MinIO，每个用例使用临时数据库和存储桶，
退出时清理；不读取开发 `.env`。非 live 测试只允许访问本机测试服务。
`tests/live` 中的测试会使用真实外部服务，默认不执行；手动运行：
`uv run --env-file .env pytest tests/live --live -q`。
这些命令说明如何运行检查，不代表现有测试已经完整或全部通过。
前端开发与检查命令见 [使用说明](docs/usage.md#8-前端开发)。

## 文档

| 文档 | 内容 |
|---|---|
| [使用说明](docs/usage.md) | 环境配置、Web 与 CLI 操作、报告导出、恢复和常见问题 |
| [设计文档](docs/design.md) | 系统行为、证据链、预算、状态、存储边界及已知实现差异 |
| [开发约定](AGENTS.md) | 开发命令、代码组织与修改纪律 |
| [前端开发约定](web/AGENTS.md) | 前端目录、组件和展示边界 |

`docs/usage.md` 是当前操作说明，`docs/design.md` 是当前系统设计依据。
仓库中尚未清理的历史方案不作为另一套现行规则。
