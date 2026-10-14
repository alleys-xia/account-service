# OMS 订单管理

第三个独立业务仓库。OMS 负责订单接收、状态、成交记录、幂等、回报路由与恢复；策略通过本仓库的协议和 SDK 调用，不直接接触执行通道。

当前版本 **0.2.0**，适用于本机 Python 3.11、PostgreSQL 16 和 NATS JetStream。服务只允许 `paper / SIMULATION`，`trade_ready` 始终为 `false`。独立 Risk 已接入模拟域；Execution、IB Gateway 和 Docker 留在后续模块实施。

## 已实现

- 股票、单腿期权、开仓 bear call / bull put 两腿信用价差的整股／整张限价委托。两腿属于一个逻辑订单，成交按腿单独记账。
- 新单、撤单、仅修改限价的改单；改单须带当前订单视图版本并重新通过风控。
- 命令去重、执行回报去重、部分成交、撤成竞争、成交更正、手续费更新、异常隔离。
- PostgreSQL 事务性 inbox / 订单状态 / journal / outbox；单账户单写入进程。
- JetStream 持久化收发；策略会话独占、租约续期、身份隔离、按策略回放与恢复。
- 策略框架的 `execution_mode = "oms"` 接入。行情 SDK 的归属保持在行情网关仓库。

## 仓库边界

```text
tradingSystem/
├── market-data-gateway/       # 独立 Git；含行情协议和 SDK
├── strategies/                # 独立 Git；模板、运行器、实际策略
└── oms/                       # 本仓库，独立 Git
    ├── src/oms_service/       # 服务、状态机、数据库和消息传输
    ├── packages/oms-protocol/ # 订单及 Risk / Execution 消息模型
    ├── packages/oms-client/   # 策略使用的 OMS 客户端
    ├── config/               # 无密钥示例配置与 NATS 权限
    ├── scripts/              # 安装和本机配置初始化
    └── docs/                 # 设计、接口、运行与验证记录
```

协议和 SDK 是这个仓库发布的两个 Python 包，不是两个额外 Git 仓库。策略仓库的 `strategy_api.orders` 保留兼容导入，内部直接引用 `oms_protocol.orders`，没有两套订单模型。

## 本机安装与启动

以下命令在 `oms` 目录执行，需要 Python **3.11** 和 `uv`。系统默认 `python3` 如果是旧版本，请明确使用 `python3.11`。

```bash
python3.11 scripts/bootstrap.py
python3.11 scripts/init_local.py
createdb oms_dev
```

`createdb` 仅用于首次建立专用空库，已经存在时跳过。OMS 自动安装自己的 `oms` schema，拒绝复用身份或版本不匹配的库。`.env` 以 0600 权限创建，重复初始化不会覆盖。

行情与订单可以共用一个 NATS 服务。`config/nats.dev.conf` 提供 gateway、oms、risk、alpha、beta 的组合权限。该配置替代单独行情版 NATS 配置；不要在同一个端口同时启动两个服务。启动前加载三个仓库的私有配置：

```bash
source ../market-data-gateway/.env
source .env
source ../risk/.env
../market-data-gateway/data/tools/nats-server-v2.15.1-darwin-arm64/nats-server -c config/nats.dev.conf
```

如使用系统安装的 NATS，可替换二进制路径。持久化目录相对于 NATS 启动目录；迁移已有实例时应保留原存储目录。另开终端启动 OMS：

```bash
source .env
.venv/bin/oms --config config/oms.example.toml
```

启动顺序：PostgreSQL → NATS → 行情网关与 OMS → Risk → 后续 Execution → 各策略。没有 Risk 时，新单等待风控，决策期限到达后过期；OMS 不自行批准风险。

## 策略接入

先在 `strategies` 中执行 Python 3.11 的 `scripts/bootstrap.py`，安装 OMS 协议、SDK 以及 API / Runtime **0.3.1**。现有 `observe`、`mock` 模式仍保留。

实例配置：

```toml
strategy_id = "my-strategy"
strategy_instance_id = "alpha"
environment = "paper"
execution_mode = "oms"
account_alias = "default"
state_root = ".state-oms"

[market]
nats_url = "nats://127.0.0.1:4222"
password_env = "STRATEGY_NATS_PASSWORD"

[oms]
nats_url = "nats://127.0.0.1:4222"
password_env = "STRATEGY_OMS_PASSWORD"
```

组合 NATS 配置使用同一策略身份：alpha 的两个密码环境变量均取 `MD_ALPHA_PASSWORD`，beta 同理。不要把密码写进实例 TOML 或 Git。

使用原有 `ctx.orders.submit/cancel/replace/snapshot`，通过 `on_order_update` 和 `on_execution` 接收回报。`CommandHandle` 仅代表本地待提交命令，SDK 的 PubAck 仅代表消息已持久化；订单是否受理或成交，以后续 OMS 回报为准。

添加策略时同步配置 NATS 的专属 subject / durable 权限和 OMS 的 `strategies` 列表，再启动独立实例。切换 observe/mock/oms 时采用新的状态目录，不能直接混用旧 SQLite 状态。

[模块设计与架构图](docs/design.md) · [接口契约](docs/interfaces.md) · [运行与恢复](docs/operations.md) · [验证记录及边界](docs/acceptance.md)

0.2 更新新增带 epoch 的 RiskRequest、专属账户回放 RPC 和 Risk durable。旧 Mock 环境切换请先阅读 [Risk 迁移说明](../risk/docs/operations.md)，不直接混用旧待审消息。
