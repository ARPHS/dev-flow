# 行为评测

`tests/` 只检查结构和脚本；这里检查**插件实际改变了 agent 的什么行为**。每个用例把夹具复制到全新的 Git 仓库，用 `claude -p` 或 `codex exec` 执行，再用程序化检查评分。两种 agent 的记录在 `lib.Context` 中统一成同一套工具调用，评分器不区分 agent。

## 用例

| 用例 | 夹具 | 关注点 |
|---|---|---|
| `quick` | ledger | 小改动不进入完整流程、不建台账、不改无关文件 |
| `explain` | ledger | 只读问答不触发 `df-flow`、不改文件 |
| `feature` | ledger | 基线、测试先行、真实 CLI 调用、证据、不提交 |
| `debug` | orders | 修复根因（隐藏用例覆盖多种数量和页大小），不放宽断言 |
| `project` | empty | 第一轮出设计并停顿，第二轮确认后再实现 |

检查分为**必需**（决定通过与否）和**参考**（例如是否调用了某个技能、是否创建 `.dev-flow/`、是否用 `evidence.py` 记录验证、是否派发独立审查），参考项用于观察趋势。

## 运行

```powershell
python -X utf8 evals/run.py                      # 全部用例，加载插件
python -X utf8 evals/run.py --case feature debug # 指定用例
python -X utf8 evals/run.py --no-plugin          # 不加载插件，作为对照组
python -X utf8 evals/run.py --grade-only <结果目录>  # 修改评分器后重新评分
python -X utf8 evals/run.py --model sonnet          # 指定 Claude 模型
python -X utf8 evals/run.py --agent codex           # 用 Codex CLI 运行
```

### Codex

- 把 `CODEX_HOME` 指向一个专用于评测的目录，其中 `config.toml` 只保留模型、认证和 `[windows] sandbox` 等设置，不要带 MCP 服务器、notify 或 `sandbox_mode = "danger-full-access"`。
- 运行器会先从当前工作树重新安装插件（Codex 从缓存加载插件，不重装就会测到旧版本）；`--no-plugin` 会卸载插件（`-c` 覆盖 `enabled=false` 不生效），之后不带该参数运行时会自动重装。
- 每轮在出现结束事件（Codex `turn.completed`、Claude `result`）后最多再等 30 秒，进程仍未退出（例如后台子代理未结束）就结束整棵进程树；超时同样结束整棵进程树，原因写入 `turnN.stderr.txt`。
- agent 在默认的 `workspace-write` 沙箱中运行，后续轮次通过线程 id 续接。Codex 没有 Skill 工具，"调用技能"按读取 `df-*/SKILL.md` 的命令判断；成本列为 0，用 tokens 列代替。tokens 取自 `turn.completed` 的 usage，经第三方代理转发的模型可能只上报部分请求，这时以耗时和命令数比较。

结果默认写入系统临时目录下的 `dev-flow-evals/<时间>/`，其中包含每个用例的工作区、每轮的 `turnN.jsonl` 原始记录、`result.json` 和汇总 `summary.md`。

> [!WARNING]
> Claude 以 `--dangerously-skip-permissions` 在临时工作区中运行；两种 agent 都会产生模型调用费用。

## 如何使用结果

- 修改技能前后各跑一次，对比通过率和参考项。模型输出有随机性，关键结论至少重复 3 次。
- 和 `--no-plugin` 对照组比较，确认插件确实带来了提升，而不只是增加了上下文。
- `case.json` 中的 `rubric` 供人工或 LLM 复核那些难以程序化判断的部分（例如设计质量）。

## 新增用例

1. 在 `fixtures/` 下添加最小夹具（尽量只依赖标准库）。
2. 在 `cases/<名称>/` 下添加 `case.json`（`description`、`fixture`、`turns`、`rubric`）和 `grade.py`（`grade(ctx) -> list[Check]`，可用的辅助函数见 `lib.py`）。
3. 在 `tests/test_evals.py` 中加入"未修改的夹具应失败、参考答案应通过"的测试，确保评分器能区分好坏结果。
