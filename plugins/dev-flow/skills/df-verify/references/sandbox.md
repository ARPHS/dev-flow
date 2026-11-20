# 环境与沙箱排障

仅在实际遇到对应环境报错或跨平台用法问题时阅读本文件，正常流程不必加载。

## Codex Windows 沙箱：`tempfile` 权限错误

Codex 的 Windows `unelevated` 沙箱中，Python 3.12.4 及以上使用 `tempfile`（`mkdtemp`、`TemporaryDirectory`）创建的目录随后无法访问，表现为测试报 `PermissionError` / `WinError 5`，路径是 `tmp*` 目录。

- **第一次出现就判定为环境阻碍，立即停止排查**：修改 `TEMP`/`TMP`、预建目录、写包装脚本重跑都无效，**不要改写项目测试**。
- 能申请提权时提权重跑；不能时把受影响的测试列入“未运行的检查”，验收条件改用真实入口（CLI、API 调用）验证。
- 沙箱里删不掉的残留目录在最终报告中列出，请用户手动删除。

## Codex Windows `elevated` 沙箱：Git `dubious ownership`

`elevated` 沙箱以独立低权限用户运行命令，与仓库所有者不同，Git 会报 `detected dubious ownership` 导致命令和 `evidence.py` 快照失败。

- 这是环境阻碍，给该命令设置环境变量后重跑（或写入 `~/.codex/config.toml` 的 `shell_environment_policy.set`）：
  `GIT_CONFIG_COUNT=1`、`GIT_CONFIG_KEY_0=safe.directory`、`GIT_CONFIG_VALUE_0=*`。
- 不要修改项目或全局的 `.gitconfig`，也不必阅读 `evidence.py` 源码排查。

## Linux / WSL 环境

- 使用 Bash 与 Linux 路径；没有 `python` 命令时统一改用 `python3`：
  ```bash
  EV="python3 <df-verify目录>/scripts/evidence.py"
  $EV doctor
  $EV run -l AC1 -- python3 -m unittest discover -s tests
  $EV report
  ```
- 首次使用时可先运行 `$EV doctor` 做只读环境自检（检查 Python、Git 版本、仓库状态与 `.dev-flow/`）。
