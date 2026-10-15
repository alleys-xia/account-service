# Account Service

包含oms 持仓以及风控的模块

**OMS、Risk、Portfolio 同仓库、同进程**。`account-runtime` 统一启动和停止三个内部模块；模块各自拥有数据库、事务和消息身份。当前版本 **0.5.0**，订单协议／客户端 **0.6.0**，Portfolio 客户端 **0.5.0**。

适用于单账户、个人自用、秒级／分钟级策略。本机先运行；Linux Docker 后续实施。行情网关、Execution 和每个策略实例仍是独立进程。整个系统的业务进程数是 **3 + 策略实例数**，另有 NATS、PostgreSQL、Futu OpenD 和 IB Gateway。

```text
account-service/
├── src/account_runtime/        统一生命周期与配置一致性校验
├── src/oms_service/            订单状态机、会话、持久命令与回报
├── src/risk_service/           账户／策略规则、额度预占、审批
├── src/portfolio_service/      策略持仓、成本、损益和券商数量对账
├── packages/oms-protocol/      订单与账户事件协议
├── packages/oms-client/        策略订单 SDK
├── packages/portfolio-client/  只读持仓 SDK 和券商快照协议
├── config/                    SIMULATION／IB_PAPER 配置与 NATS ACL
├── scripts/                   安装与私有凭据初始化
└── docs/                      设计、运行与验收
```

这些 `packages/` 都属于本仓库，用固定版本 wheel 提供给策略和 Execution，**不另建共享包仓库**。策略无需安装账户服务、IB 或 PostgreSQL 驱动。

## Portfolio 首期能力

- 从 OMS 持久账户事件按序回放，每个策略、每个合约独立记账；重复事件幂等，冲突或序列缺口冻结。
- 逐腿成交、撤销／更正成交、版本化手续费；移动加权平均成本、已实现损益、交易现金流。现金流不等于账户余额；未实现盈亏暂不估算。
- Execution 只读查询新鲜 IB 持仓和资金摘要，Portfolio 检查采集窗口和账本水位。已有或无法解释的仓位保留为**外部／未分配**，不自动分配策略。
- 策略通过 `ctx.portfolio.snapshot()` 查询，通过 `on_recover` 和 `on_position_update` 获取完整视图；NATS ACL 限制其只能读取自身持仓。

`POSITIONS_MATCHED` 表示 OMS/Portfolio 与 Execution 自有成交数量匹配，不代表资金、保证金和税务对账完成。手工及外部持仓单独列示，支持同合约共存；不会自动归属策略。IB_PAPER 示例启用 `require_portfolio=true`：内部数量不一致、快照过期或来源不可用时拒绝新增风险。已支持按原开仓批次卖出股票／单腿多头期权，以及整组平仓 bear call / bull put；Risk 原子预占可平数量，确认成交后释放相应持仓预算。所有平仓强制新鲜 Portfolio 门控。股票开空及买回平仓仅对显式授权标的开放，并要求独立风险预算及执行端借券检查；期权新开裸卖、拆保护腿、外部持仓归属、自动差异修复、行权／指派和实盘仍未开放。

同进程提供模块与数据所有权边界，共享内存和故障范围；任一核心模块不可恢复故障会使整个账户进程退出。保留 `oms`、`risk`、`portfolio` 单模块入口用于排障，同一数据库不要与统一入口同时启动。

[当前架构](docs/portfolio-design.md) · [模块边界与状态](docs/operations.md) · [本轮验收](docs/portfolio-acceptance.md) · [订单接口](docs/interfaces.md) · [Risk 规则设计](docs/risk/design.md)

本版不实现统一监控、期权到期／指派处理、行情回放。平仓与风险额度闭环已实现，不新增独立服务；见 [设计及接口](docs/close-risk-design.md) 与 [本轮验收](docs/close-risk-acceptance.md)。本轮不实现具体策略。

本次复核（0.2.1）：OMS 支持 Execution 经逐腿成交核对后的 BROKER_CANCELLED 终态；仅释放未成交预算，保留已成交持仓预算。已通过临时 PostgreSQL 回归与独立复核。需与 Execution 0.3.1 协调升级。

本轮（0.3.0）增加[手工持仓共存](docs/manual-positions.md)，保留真实风控与持仓核验；修复拒单／超时原因未写入快照，以及费用超额时自有减仓被一并阻断。完整 IB 模拟联调见[验收记录](docs/ib-full-chain-20261014.md)。

港股恒指／小型恒指期货的币种限额与记账约定见 [设计](docs/hk-index-futures.md)，真实模拟账户结果见 [2026-10-15 验收](docs/hk-futures-validation-20261015.md)。

股票空头的授权、预算与平仓边界见 [stock-shorts.md](docs/stock-shorts.md)。跨市场日内策略的真实模拟成交记录见 [策略验收](../strategies/docs/implementation-and-validation.md)。
