# 港股股指期货验收 · 2026-10-15

本机真实 Futu OpenD + IB Gateway **模拟账户**，约北京时间 11:42。使用固定版本 SDK、正常 NATS ACL、OMS、Risk、Execution、Portfolio 全链路，没有授权测试桩。沿用原有 QQQ −300 股作为外部持仓，不归属策略；本次仅交易 MHI。

## 合约与行情

| 品种 | Futu 实际代码 | IB conId / localSymbol | 最后交易日 | 乘数 / 档位 |
|---|---|---|---|---|
| 小型恒指 | HK.MHI2610 | 919041121 / MHIV6 | 2026-10-29 | HKD 10 / 1 点 |
| 恒指 | HK.HSI2610 | 897062803 / HSIV6 | 2026-10-29 | HKD 50 / 1 点 |

OpenD `get_future_info` 与 IB ContractDetails 交叉核对一致。真实 SDK 收到 MHI QUOTE 和十档 ORDER_BOOK 推送；MHI/HSI 一次性快照返回 HKD 报价。分钟历史查询 `session="ALL"` 包含夜盘，香港时间正确转 UTC。数据量字段仍标明 PROVIDER_UNVERIFIED，未把 10/50 合约乘数当作行情数量倍率。

## 下单与回报

全部成交单为 1 手 LIMIT；开平使用同一策略的 lot 引用，BUY/SELL 价格单位 INDEX_POINTS。

| 测试 | 结果 | 成交价 | 手续费 HKD |
|---|---|---|---|
| 被动买单 → 撤单 | SUBMITTED → CANCELLED | 未成交 | 0 |
| 多头开仓 BUY | FILLED | 24036 | 17 |
| 多头平仓 SELL | FILLED | 24035 | 17 |
| 空头开仓 SELL | FILLED | 24033 | 17 |
| 空头平仓 BUY | FILLED | 24037 | 17 |

共 5 个 IB 订单，4 笔成交及 4 笔费用回报。另有 3 个 OMS 风控拒单：超 1 手限制两次（首次测试断言遗漏 `RISK_REJECTED:` 前缀后重跑）与非整数价格一次，均 NOT_SENT，未抵达 IB。

审查发现卖出限价不能作为空头名义金额上界，已改成显式每手固定预算。MHI 本轮配置 500,000 HKD／手，加 100 HKD 手续费缓冲；多头与空头开仓时都占用 500,100 HKD，USD 风险额度保持 0。平仓后 HKD／USD 未成交占用和持仓占用全部归零。

## 终态与恢复

- MHI 最终数量 0；无未平 lot；成交毛损益 −50 HKD，费用 68 HKD，净损益 −118 HKD。
- `trade_cash_flow=null`、`FUTURES_SETTLEMENT_UNTRACKED`，没有把期货本金当现金收付；逐日结算不在本版范围。
- 直接只读 IB 查询确认账户全部非零持仓与测试前一致：QQQ −300 股；全账户无挂单。
- Portfolio 为 POSITIONS_MATCHED，Risk／Execution 未冻结；账户事件水位 38。
- 关闭并重启 account-service、Execution 后，保持同一 feed epoch、水位 38、费用 68 与相同持仓／损益；未重复发送订单。
- HSI 完成实际行情、合约资格校验；按用户要求没有提交 HSI 订单。

## 代码检查与交付边界

21 项临时回归全部通过，四仓库 Ruff／格式检查与已安装依赖兼容检查通过。临时回归覆盖协议多空反转／旧 USD 行为、合约币种／月份／乘数／档位、费用币种、缺失预算、低价卖单预算、跨币种金额与全局计数、期限、持仓归属、损益、行情时区／会话和额度。独立代码审查的风险预算问题已修复并复查通过。临时脚本、测试源码、凭据及联调数据库在终态确认后清理。

版本：market-data-gateway 0.4.0，行情 protocol/client 0.2.0；account-service 0.4.0，OMS protocol/client 0.5.0，portfolio-client 0.4.0；execution-service 0.5.0；strategy-api/runtime 0.7.0。

这是本机模拟验收；Ubuntu x86_64 服务器运行仍待部署。没有加入正式策略、Docker、自动换月、保证金引擎、盯市结算或实盘权限。
