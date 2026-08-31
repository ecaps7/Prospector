# Prospector 使用说明

本文说明当前可以怎样运行和使用 Prospector。系统内部规则见 [设计文档](design.md)，
最短启动流程见 [README](../README.md)。除特别说明外，命令都在仓库根目录执行。
命令中的 `<job-id>` 请替换为任务的完整 UUID，不要保留尖括号。

## 1. 选择入口

| 入口 | 适用场景 | 是否需要 API 服务 |
|---|---|---|
| Web 界面 | 提问、确认方案、查看进度、阅读报告和引用 | 需要 `prospector serve`，以及已构建的前端 |
| `prospector` | 在终端交互提问，查看任务、取消或导出报告 | 需要 `prospector serve`；不需要构建前端 |
| `prospector-local` | 初始化存储、直接运行研究、查看本地事件和显式恢复 | 不需要 API 服务，但直接连接数据库、对象存储和模型服务 |

Web 与 `prospector` 使用同一服务。服务一次执行一个 Job，其余提交排队；
一个 Job 内部的多个研究任务仍可并行。`prospector-local` 不经过服务端队列，
不要用它同时恢复一个仍在其他进程执行的任务。

## 2. 首次准备

### 环境与配置

需要 Python 3.13+、uv、Docker Compose。构建 Web 还需要 Node.js 22.12+ 与 npm。
只用命令行时可以省去前端安装和构建。

首次创建 `.env`；已有配置时直接编辑，不要覆盖：

```bash
cp .env.example .env
```

按 [配置示例](../.env.example) 填写：

| 配置 | 用途 |
|---|---|
| `DATABASE_URL` | PostgreSQL 连接；示例值对应附带的 Compose |
| `S3_ENDPOINT`、`S3_ACCESS_KEY`、`S3_SECRET_KEY`、`S3_BUCKET` | MinIO 或 S3 兼容对象存储；本地示例值对应 Compose |
| `PROSPECTOR_LLM_BASE_URL` | 模型服务的兼容 API 地址 |
| `PROSPECTOR_LLM_API_KEY` | 模型服务密钥 |
| `PROSPECTOR_LLM_MODEL_STRONG`、`PROSPECTOR_LLM_MODEL_MID` | 两档模型名称；必须是服务端实际提供的模型 |
| `EXA_API_KEY` | Exa 搜索及网页内容服务密钥 |
| `PROSPECTOR_SERVER` | CLI 连接的 Prospector 地址，默认 `http://127.0.0.1:7620`；需要改端口时自行添加 |

当前代码根据 DeepSeek 或 Qwen 模型名设置对应的思考参数。仅有兼容 API 地址，
不代表其他模型系列可以直接替换。服务可以在未配置模型时启动，但提问和研究需要上述密钥。
研究材料片段会发送到配置的模型服务，检索请求会发送到 Exa。

`.env` 不应提交到版本库。示例中的 `PROSPECTOR_API_TOKEN` 当前未用于 HTTP 身份校验，
填写它不会开启认证。当前部署只面向可信本机环境。

### 安装与初始化

```bash
uv sync --frozen
docker compose up -d --wait
uv run --env-file .env alembic upgrade head
uv run --env-file .env prospector-local setup
```

这些步骤分别安装依赖、启动 PostgreSQL 与 MinIO、升级业务表、创建 checkpoint 表并确保
对象存储桶存在。服务默认端口为：PostgreSQL `5432`、MinIO API `9000`、MinIO 控制台 `9001`。

已有环境后，日常使用不必重做初始化。更新代码后若有新迁移，再执行 `alembic upgrade head`。
`prospector serve --init` 可以代替最后一步的 checkpoint 和存储桶初始化，不能代替数据库迁移。

### 启动 Web 服务

先构建前端，再启动服务：

```bash
npm --prefix web ci
npm --prefix web run build
uv run --env-file .env prospector serve
```

打开 [Web 界面](http://127.0.0.1:7620/)。前端由同一个服务托管，不需要另开前端进程。
没有构建 `web/dist` 时，API 和 CLI 仍可使用，但浏览器首页不可用。
若先启动了服务、后补建前端，需要重启服务才能挂载界面；有运行中任务时先等待其结束。

服务只监听 `127.0.0.1`。如需换端口：

```bash
uv run --env-file .env prospector serve --port 7621
```

此时浏览器使用新端口，CLI 也要指定相同地址，例如在另一个终端运行：

```bash
PROSPECTOR_SERVER=http://127.0.0.1:7621 uv run --env-file .env prospector
```

## 3. 在 Web 中研究

1. 在首页输入问题，选择投入档位和报告语言。时间、地区、比较对象或来源限制应直接写进问题。
2. 系统必要时提出一个澄清问题；回答后生成待确认的研究方案，也就是 Brief。
3. 阅读“研究范围”和“你的限定条件”。研究范围文字可以直接修改；点击“编辑”，输入指令后
   点击“重写”，可以让模型修改方案。重写不会自动开始研究，仍需复看后点击“开始”。
4. 点击“开始”后创建 Job，进入“研究监控”。可以查看计划、各任务进度、事件时间线和用量。
   “全部”显示完整业务时间线，“关键”减少高频工具与轮次记录的展示。
5. 报告生成后切换到“报告”。点击正文角标或来源条目，可以查看相应来源及保存的证据片段。

`quick`、`standard`、`deep` 调整研究可用轮次和并发限制，不是固定字数、费用或耗时。
具体限额统一见 [设计文档 §1.3](design.md#13-投入档位effort)。

确认前点击“取消”只是放弃方案，不会创建 Job；但此前的方案生成已经可能产生模型费用。
确认后不能修改本次研究输入，需求变化需要重新创建任务。

关闭页面不会取消已经创建的 Job，只要服务仍在运行，研究就会继续。
从 [任务列表](http://127.0.0.1:7620/jobs) 可以重新打开监控和报告。
停止研究应使用“取消任务”；当前调用可能需要完成后才能停止，取消后不能恢复。

任务列表中的删除只把已停止的任务移出列表，不清除证据、共享快照或报告文件。
它不是释放全部存储空间的操作。仍在运行或排队的任务应先取消。

## 4. 在命令行中研究

保持服务运行，另开终端：

```bash
uv run --env-file .env prospector --effort standard --language zh
```

不带子命令会进入持续交互界面，输入一个研究问题即可开始。一次研究结束或离开进度展示后，
返回问题输入处，可以继续提问。当前没有 `prospector ask` 子命令。

确认 Brief 时：

| 输入 | 行为 |
|---|---|
| `c` | 确认当前版本并开始研究 |
| `e` | 在编辑器中修改 YAML，保存退出后回到确认界面；可多次编辑 |
| `i` | 输入一条指令，让模型修订一次，再回到确认界面；本次确认流程最多使用一次 |
| `q` | 放弃当前方案，不创建 Job |

编辑器依次采用 `$EDITOR`、`$VISUAL`，未设置时使用 `vi`。
交互提问需要输入、输出都连接到终端；`--plain` 只改变进度样式，不会绕过人工确认。

创建后会打印 `JOB_CREATED: <job-id>`。常用命令如下：

```bash
uv run --env-file .env prospector job list
uv run --env-file .env prospector job status <job-id>
uv run --env-file .env prospector job attach <job-id>
uv run --env-file .env prospector job attach <job-id> --plain
uv run --env-file .env prospector job cancel <job-id>
```

`status` 查看当前状态、任务和用量；`attach` 回放并继续跟踪进度。
非交互输出或 `--plain` 使用逐行时间线，否则使用终端面板。
查看期间按 Ctrl-C 只离开展示，任务继续；终端面板中的 `x` 才会请求取消。
逐行模式需要从另一个终端运行 `job cancel` 来取消。

`attach` 不是恢复命令。如果执行进程已经中断，重新连接只会继续等待事件，
不会让研究重新运行。恢复方法见下一节。

## 5. 本地运行与恢复

### 不启动 API，直接研究

完成数据库和对象存储初始化后，在交互终端执行：

```bash
uv run --env-file .env prospector-local ask "一个需要多方查证的问题" --effort standard --language zh
```

该入口使用相同的研究图和 Brief 确认方式，直接在当前进程执行，完成后向终端输出 Markdown
及对象存储引用。它不会自动将报告下载到 CLI 的缓存目录。
与服务端 `attach` 不同，终止这个执行进程会中断研究。

不启动 API 也可以查看数据库中已保存的时间线：

```bash
uv run --env-file .env prospector-local job events <job-id>
uv run --env-file .env prospector-local job events <job-id> --follow
```

### 恢复中断的研究

服务启动时不会自动恢复上次中断的研究。恢复前应当：

1. 确认原执行进程已经停止，不能让同一个 Job 被两个进程同时执行。
2. 处理此前的配置、服务或模型输出错误；使用原来的数据库和对象存储。
3. 确认任务没有完成或取消，再执行：

```bash
uv run --env-file .env prospector-local job resume <job-id>
```

恢复会尝试从原 checkpoint 继续，而不是创建新 Job；外部模型或工具调用仍可能重做并产生费用。
已经写入终止事件 `job.stopped` 的任务会被拒绝，包括完成、取消和预算耗尽等终态失败。
不能只凭页面显示“失败”就断定能否恢复；保存的终止事件、checkpoint 和原错误都要检查。
恢复也不增加原任务的预算，不保证任何中断都能成功继续。

不要为了恢复而先取消任务：取消会让它正式结束，之后不能 `resume`。

## 6. 查看、导出与理解报告

终端查看或导出需要 API 服务：

```bash
uv run --env-file .env prospector report show <job-id>
uv run --env-file .env prospector report export <job-id> --format md -o report.md
uv run --env-file .env prospector report export <job-id> --format json -o report.json
```

`export` 不覆盖已有文件，目标存在时会报错；不指定 `-o` 时使用当前目录的 `report.md`
或 `report.json`。`attach` 跟踪到任务完成时，还会自动下载 Markdown 到
`~/.prospector/reports/<job-id>/report.md`；这是缓存，后续 attach 可能重新写入，不要在此编辑唯一副本。

| 产物或入口 | 内容 |
|---|---|
| Web 报告页 | 正文、脚注、来源清单及引用证据抽屉，不展示内部核验摘要和问题清单 |
| Markdown 文件 / `report show` | 核对情况摘要、正文与来源 |
| JSON 文件 | 归因结果、证据关联、问题记录及报告核验结果，不是另一份正文格式 |

Web 当前没有报告导出按钮，可使用上述命令保存文件。
脚注能帮助回查材料，但仍应结合来源和上下文判断，不把引用存在当作结论绝对正确。

`job status` 中的报告 `verification` 与 Job 的执行状态分别表示：

| 报告核验结果 | 含义 |
|---|---|
| `verified` | 通过现有归因与整体审查，不保证绝对正确 |
| `partial` | 仍有未解决的非核心问题 |
| `failed` | 核心内容未通过核验，不等于全文全部错误 |

三种报告都可交付。Job 的 `completed` 只表示执行和交付已完成；Job 本身 `failed` 则是执行失败，
不能与报告的同名核验结果混淆。核验规则见 [设计文档 §2.8](design.md#28-review-与-readthrough)。

## 7. 常见问题

| 现象 | 检查方式 |
|---|---|
| 浏览器打不开，CLI 提示服务未运行 | 确认 `prospector serve` 仍在运行；浏览器端口与 `PROSPECTOR_SERVER` 应一致 |
| API 可访问，但首页 404 | 检查是否已构建 `web/dist`；首次补建后需重启服务 |
| `serve preflight failed` | 用 `docker compose ps` 查看 PostgreSQL、MinIO 状态，再检查连接配置、迁移和存储桶初始化 |
| 提问提示 `llm_not_configured` | 检查模型地址与密钥，修改服务配置后重启服务；健康检查不验证外部模型或 Exa 是否可用 |
| 不支持的模型、搜索鉴权失败 | 检查实际模型名、对应系列请求参数及 Exa 密钥；不要只检查服务能否启动 |
| 报告提示 `report_not_ready` | 报告尚未保存；查看任务进度与错误，失败或取消的任务不保证有报告 |
| 页面显示运行中，但事件长期不动 | 先检查原执行进程及服务日志；慢调用和进程中断都可能出现这种现象，不能直接重复启动任务 |
| Ctrl-C 后研究仍在跑 | 服务端 attach 的正常行为；若要停止，使用 Web 取消按钮或 `job cancel` |
| 导出提示目标文件存在 | 换一个输出路径；命令不会覆盖原文件 |

日常停用前，先等待任务结束或明确取消，再停止服务和基础设施。
当前 [Compose 配置](../docker-compose.yml) 没有显式声明持久化卷，不能把删除、重建容器当成安全保留
研究历史的方式。仅需暂停基础设施时使用 `docker compose stop`；数据库和 MinIO 数据都需要保留，
只保留代码、`.env` 或下载的报告不能恢复完整研究记录。

## 8. 前端开发

保持后端在 `7620` 运行，另开终端启动 Vite：

```bash
npm --prefix web ci
npm --prefix web run dev
```

使用 Vite 输出的地址。它把 `/api` 请求代理到 `http://127.0.0.1:7620`；
`PROSPECTOR_SERVER` 只影响 CLI，不会修改这个代理。

前端检查与构建：

```bash
npm --prefix web exec -- playwright install chromium
npm --prefix web run lint
npm --prefix web test
npm --prefix web run build
```

浏览器安装只需首次或 Playwright 升级后执行。`npm test` 同时运行纯函数/API 流测试和 Chromium
交互测试；浏览器测试自行启动 `127.0.0.1:4173` 的 Vite，并拦截 API 响应，不调用研究服务。

构建结果在 `web/dist/`，供 `prospector serve` 托管。开发规则见 [web/AGENTS.md](../web/AGENTS.md)。

## 9. API 与命令参考

服务运行时，可查看 [交互式 API 文档](http://127.0.0.1:7620/docs) 和
[OpenAPI schema](http://127.0.0.1:7620/openapi.json)。业务接口统一位于 `/api` 下；
这里不再复制字段表，以免与实际接口分叉。

主要入口包括 Scope、Brief 修订、Job 创建与查询、取消、软删除、事件流、报告和证据片段回查。
`POST /api/jobs` 接收完整且已确认的 Brief，不直接接收一条自然语言问题。
事件流支持 `Last-Event-ID` 续传；详细语义见 [设计文档 §6.3](design.md#63-业务事件与-sse给监控与诊断)。

查询当前命令参数：

```bash
uv run prospector --help
uv run prospector job --help
uv run prospector report export --help
uv run prospector-local --help
uv run prospector-local job resume --help
```
