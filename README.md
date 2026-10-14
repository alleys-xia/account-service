# Account Service

**OMS、Risk、Portfolio 同仓库、同进程**。`account-runtime` 统一启动和停止三个内部模块；模块各自拥有数据库、事务和消息身份。当前版本 **0.1.0**，订单协议／客户端沿用 **0.3.0**，Portfolio 客户端 **0.1.0**。

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

## 安装与启动

需要 Python 3.11、uv、PostgreSQL 和 NATS JetStream。与 `market-data-gateway`、`execution`、`strategies` 平级放置：

```bash
cd account-service
python3 scripts/bootstrap.py
python3 scripts/init_local.py
# 已有 .env 不会被覆盖；按 .env.example 补齐缺少的密码。
set -a
source .env
set +a
```

安装后以 `python3 scripts/bootstrap.py --locked` 验证锁文件。先准备各模块数据库和 NATS，再启动账户进程。IB 模拟环境示例：

```bash
# 仅首次执行；有同名数据库时不要重建。
createdb oms_ib_paper
createdb risk_ib_paper
createdb portfolio_paper

# 独立终端启动；从 account-service/ 执行并加载相同 .env。
../market-data-gateway/data/tools/nats-server-v2.15.1-darwin-arm64/nats-server -c config/nats.paper.conf

# 一个终端即启动三个模块。
.venv/bin/account-runtime --oms config/oms.paper.toml --risk config/risk.paper.toml --portfolio config/portfolio.paper.toml
```

再启动独立 [Execution](../execution/README.md)。两侧 NATS 密码必须一致。策略与 OMS、Portfolio 的账户别名／执行模式也必须一致。SIMULATION 使用默认三份 `*.example.toml`，对应 `oms_dev`、`risk_dev`、`portfolio_dev`；没有券商快照提供者时 Portfolio 如实显示来源不可用。

## Portfolio 首期能力

- 从 OMS 持久账户事件按序回放，每个策略、每个合约独立记账；重复事件幂等，冲突或序列缺口冻结。
- 逐腿成交、撤销／更正成交、版本化手续费；移动加权平均成本、已实现损益、交易现金流。现金流不等于账户余额；未实现盈亏暂不估算。
- Execution 只读查询新鲜 IB 持仓和资金摘要，Portfolio 检查采集窗口和账本水位。已有或无法解释的仓位保留为**外部／未分配**，不自动分配策略。
- 策略通过 `ctx.portfolio.snapshot()` 查询，通过 `on_recover` 和 `on_position_update` 获取完整视图；NATS ACL 限制其只能读取自身持仓。

`POSITIONS_MATCHED` 只表示持仓数量匹配，不代表资金、保证金和税务对账完成。IB_PAPER 示例启用 `require_portfolio=true`：存在外部差异、快照过期或来源不可用时拒绝新增风险。仍不自动释放已有风险预占，也未开放平仓、单腿卖出、自动差异修复、行权／指派处理或实盘。

同进程提供模块与数据所有权边界，共享内存和故障范围；任一核心模块不可恢复故障会使整个账户进程退出。保留 `oms`、`risk`、`portfolio` 单模块入口用于排障，同一数据库不要与统一入口同时启动。

[当前架构](docs/portfolio-design.md) · [运行与迁移](docs/operations.md) · [本轮验收](docs/portfolio-acceptance.md) · [订单接口](docs/interfaces.md) · [Risk 规则设计](docs/risk/design.md)
