# web/AGENTS.md

React 19 + Vite SPA：提问、任务监控、报告、任务列表。API 类型与错误码跟 `src/prospector/api` 对齐，不要在前端发明第二套任务状态机。

## Commands

```bash
npm ci
npm run dev      # 开发；/api 代理到 http://127.0.0.1:7620
npm run build    # tsc -b && vite build → dist/，供 prospector serve 托管
npm run lint     # oxlint src
```

改 UI 后跑 `npm run build`。`dist/` 是服务端静态资源，不要手改打包文件。

## Layout

```
api/         REST / SSE 客户端与响应类型
state/       事件折叠、时间线投影、预算换算（纯函数，无 DOM）
lib/         无状态工具：status.ts（状态→色彩）、format.ts（数字/时钟/日期）
components/
  ui/        跨页原语：StatusDot、Tag、Chip、Meter、Spinner、AutoGrowTextarea…
  ask|monitor|report|jobs/   单页私有组件
  *.tsx      框架级组件：TopBar、JobBar、Toast、Segmented
pages/       只做编排：取数、订阅、状态机、把数据喂给组件
styles/app.css   全站唯一样式表
```

- 组件放哪由复用范围决定：两个以上页面用到才升到 `ui/`，否则留在页面自己的目录。
- 页面文件里出现大段 JSX 就该往下拆。`pages/` 剩下的行数应当是编排逻辑，不是标记。

## Boundaries

- 认证头与 REST/SSE 契约以现有 `src/api/client.ts`、`src/api/sse.ts` 为准。
- 报告角标、引用列表、`verified` / `partial` 是后端确定性渲染结果；前端只展示，不重算编号。
- 任务状态到颜色的映射只有 `lib/status.ts` 一处。不要在页面里手写 `status === "completed" ? …` 的 if 链——这类映射散开过一次，三份实现对 `running` 和 `completed` 已经给出不同结果。
- 样式是 `app.css` 里的全局语义类，组件只负责挂 className。抽组件时保持渲染出的 DOM 与 className 不变，改动才可以用「页面长得完全一样」来验证。
- 不要引入状态管理库、UI 组件库或第二套样式方案（CSS Modules / Tailwind / CSS-in-JS），除非现有页面明显撑不住。
