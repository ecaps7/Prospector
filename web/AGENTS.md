# web/AGENTS.md

本文件补充[根目录约定](../AGENTS.md)。前端使用 React + TypeScript + Vite；
展示边界见 [design §6](../docs/design.md#6-对外交付与展示边界)，启动方式见 [usage](../docs/usage.md)。

## 代码边界

以下路径相对于 `web/src/`：

- `api/` 集中处理 REST/SSE 与类型，和后端 `src/prospector/api/` 对齐；不要另定义 Job 状态或把报告核验失败当成任务执行失败。
- `state/` 保存事件处理和展示计算的纯函数；页面负责取数、订阅与编排，组件按职责和复用范围拆分。
- 正文使用后端 Markdown，不重算引用编号；审计 JSON 只为引用抽屉补充证据，不作为另一份正文或直接展示审计明细。
- 报告按不可信内容处理：禁止渲染原始 HTML，链接仅允许 http/https/mailto，不得放宽这些限制。
- 状态颜色集中在 `lib/status.ts`，样式集中在 `styles/app.css`；沿用现有组件和样式，不额外引入状态管理库、UI 库或样式体系。

## 检查

修改前端后，在 `web/` 运行：

```bash
npx playwright install chromium  # 首次或 Playwright 升级后
npm run lint
npm test
npm run build
```

`npm test` 包含纯函数/SSE 测试和 Chromium 交互测试；API 响应受测试控制，不需要真实后端。
浏览器测试使用 4173 端口，不复用已有开发服务。

涉及交互或样式时还需检查实际页面；纯重构应保持行为和外观不变。
`dist/` 是构建产物，由 `prospector serve` 托管，不要手动修改。
