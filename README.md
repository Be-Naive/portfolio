# Cross-Broker Portfolio Dashboard

本项目是一个本地优先的跨券商投资看板原型，用来统一处理：

- 国泰海通 PDF 对账单导入
- IBKR Flex Query / Flex Web Service 同步
- 多币种现金、持仓、入金出金、收益重建
- 历史净值 / 历史价格 / 历史汇率驱动的收益曲线
- 最简再平衡建议

完整项目文档见 [PROJECT_DOCUMENTATION.md](PROJECT_DOCUMENTATION.md)。

## 快速启动

Windows 上可以直接双击根目录的 `start-dashboard.cmd`。它会自动选择
`8000-8010` 之间的空闲端口，在后台守护看板，并打开正确的浏览器地址；
进程意外退出后会自动重启。需要停止时双击 `stop-dashboard.cmd`。

首次使用仍需安装环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

也可以手动启动；端口冲突时通过 `PORT` 指定其他端口：

```powershell
$env:PORT = 8001
.\.venv\Scripts\python.exe run.py
```

浏览器打开：

- 默认地址是 [http://127.0.0.1:8000](http://127.0.0.1:8000)，一键脚本会在端口被占用时自动改用下一个空闲端口。

## 当前主要能力

- 解析国泰海通 `资金股份流水` PDF
- 同步 IBKR Flex XML 报表
- 自动重建统一账本
- 计算总资产、本金、收益额、TWR、最大成本收益率
- 生成产品分析、收益率曲线、盈利日历、再平衡建议
- 在盈利日历的全区间、年度、月度和单日详情中查看产品收益拆分
- 页面打开时自动定向刷新当前持仓价格
- 从看板导出或恢复完整 SQLite 账本

## 账本迁移

1. 在旧设备的看板中点击“导出账本备份”，保存 `.db` 文件。
2. 在新设备安装依赖并启动看板。
3. 在“恢复账本”中选择 `.db` 文件，确认替换当前账本后恢复。

备份包含统一账本、账户、产品、交易流水、行情、汇率、持仓快照和同步记录，不包含 IBKR Flex Token。数据库包含私人财务数据，请按敏感文件保管。

## 测试

```bash
python -m unittest discover -s tests -v
```

