# Cross-Broker Portfolio Dashboard

本项目是一个本地优先的跨券商投资看板原型，用来统一处理：

- 国泰海通 PDF 对账单导入
- IBKR Flex Query / Flex Web Service 同步
- 多币种现金、持仓、入金出金、收益重建
- 历史净值 / 历史价格 / 历史汇率驱动的收益曲线
- 最简再平衡建议

完整项目文档见：

- [/Users/bytedance/Downloads/portfolio/PROJECT_DOCUMENTATION.md](/Users/bytedance/Downloads/portfolio/PROJECT_DOCUMENTATION.md)

## 快速启动

```bash
cd /Users/bytedance/Downloads/portfolio
python3 run.py
```

浏览器打开：

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

## 当前主要能力

- 解析国泰海通 `资金股份流水` PDF
- 同步 IBKR Flex XML 报表
- 自动重建统一账本
- 计算总资产、本金、收益额、TWR、最大成本收益率
- 生成产品分析、收益率曲线、盈利日历、再平衡建议
- 页面打开时自动定向刷新当前持仓价格

## 测试

```bash
python3 -m unittest discover -s /Users/bytedance/Downloads/portfolio/tests -v
```

