# Report Verifier 小型评测

这套评测只回答一个问题：Report Verifier 能否对人工已经确认答案的短案例作出正确判断。

当前案例包含四句话：

- 原文直接支持的等待时间事实，应当通过；
- 写错的满意度增幅，不得通过；
- 从三个站的短期试点推断全市拥堵改善，应判为推断过头；
- 原文明确披露的研究限制，应当通过。

运行：

```bash
uv run --env-file .env python eval/run_report_verifier.py
```

脚本退出码为 `0` 表示全部判断符合人工预期，`1` 表示至少一项判断错误，`2` 表示案例、模型配置或模型输出本身无效。

案例完全保存在 `eval/cases/report_verifier_basic.json`，不依赖本机数据库中的历史 Job。评测只比较结论类别，不要求模型生成固定措辞。
