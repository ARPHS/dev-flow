---
name: df-verify
description: 在声明开发任务完成前选择并运行适用的测试、静态检查、构建和真实入口验证，输出可复核证据（verify, run tests, check before done）。
---

# Verify

根据改动的行为和风险选择最小但充分的验证集合。先运行最接近改动的检查，再运行项目要求的整体检查（命令来源优先使用 `.flow/project.md`，其次是 CI 配置）。证据类型按 [quality.md](../df-flow/references/quality.md) 选择。

## 步骤

1. 列出验证集合：每条验收条件对应至少一项检查，加上项目整体的 test、lint/typecheck、build。
2. 逐项运行，记录命令、工作目录、退出码和关键输出。失败时读取完整输出。
3. 对照基线区分：本次引入的失败、已知基线失败、环境阻碍。本次引入的失败必须修复或返回 `df-flow` / `df-debug`，修复后重新运行受影响检查。
4. 存在 `.flow/progress.md` 时，运行指纹脚本并更新 `证据版本` 和验证证据表：

   ```bash
   python <本技能目录>/scripts/fingerprint.py
   ```

   没有 Python 时改为记录 `git rev-parse HEAD` 和 `git status --short` 的输出。
5. 输出证据。

## 输出格式

```text
## 验证证据（版本：<指纹或 HEAD>）
| # | 检查 | 命令 | 目录 | 退出码 | 关键结果 |
|---|---|---|---|---|---|
| 1 | AC1 | ... | . | 0 | 12 passed |
## 验收条件对照
- AC1 ✅ 见 #1
## 未运行的检查
- <检查>：<原因>
## 基线失败（非本次引入）
- <测试名>
```

## 禁止

- 用静态阅读、"代码已存在"、代理报告或未执行的命令替代运行结果。
- 引用修改代码之前的运行结果作为当前证据。
- 只跑单元测试就声称 UI、性能或真实入口已验证。

## 可选的完成门禁

`scripts/check_evidence.py` 可以作为 Claude Code 的 `Stop` hook，用来阻止"`progress.md` 标记 done 但证据版本与当前代码不一致"的结束。插件默认不启用，启用方法见插件 README。
