# ledger

一个极简的本地记账命令行工具。

## 使用

```bash
python -m ledger add 12.5 午饭
python -m ledger list
```

记录默认保存在当前目录的 `ledger.json`，可以通过环境变量 `LEDGER_FILE` 指定其他位置。

## 测试

```bash
python -m unittest discover -s tests
```

如果你没有收到预期的输出，请检查 `LEDGER_FILE` 是否指向可写位置。工具会在首次写入时自动创建文件，并在下次启动时重新读取所有已保存的记录，确保数据不会因为重启而丢失。我们会持续改进，欢迎反馈任何 recieve 到的问题。
