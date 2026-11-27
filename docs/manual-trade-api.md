# 手动下单与单笔撤单 HTTP 接口（候选）

默认 `thsmacosauto` 服务仍是只读服务。下单、单笔撤单、回执查询和账本状态只由独立的 `thsmacosauto.trade_api.create()` 服务组合注册。两种组合不得同时占用一个真实 GUI，也不得通过改变 GUI owner 目录来并行运行。当前代码支持隔离测试；内置同花顺实盘配置尚未通过交易表单、账户/委托完整性、交易日、新合同号与单笔撤单控件的核验，启动实盘组合会拒绝。自动交易和批量撤单不在此接口范围内。

## 通用约定

仅回环地址。每次请求带 `Authorization: Bearer <服务令牌>`；POST 发送 UTF-8 `application/json` 对象，不接收额外字段或浏览器 Origin。参考[只读接口的报文约定](http-api.md#transport)。业务结果看 `status`，HTTP 错误看 `code`，不能以 HTTP 200 或已点击为成功。`execution_domain` 区分隔离测试 `SYNTHETIC` 与实盘状态域 `REAL`；任何 `SYNTHETIC` 回执都不代表券商动作。能力接口的 `native_readiness: NOT_CHECKED` 不证明实盘可用。

| 方法与路径 | 用途 |
|---|---|
| `POST /v1/real/orders/submit` | 显式提交一笔手动限价买入或卖出 |
| `POST /v1/real/orders/cancel` | 对本服务已核实的合同编号发起一次单笔撤单 |
| `GET /v1/real/orders/status?client_order_id=...` | 读取本地持久化请求回执；不访问 GUI、不会执行或重试 |
| `GET /v1/real/orders/journal-status` | 读取本地账本的总请求、未决和处理中计数；不返回明细，不访问 GUI |
| `GET /v1/health` / `GET /v1/capabilities` | 服务与路由检查；不证明客户端或券商的交易就绪性 |

## 1. 下单

`POST /v1/real/orders/submit`

```json
{
  "client_order_id": "fixture-order",
  "mode": "real",
  "order_type": "limit",
  "symbol": "600000",
  "side": "buy",
  "price": "10.00",
  "quantity": 100,
  "shareholder_scope": "FIXTURE-HOLDER"
}
```

上面是测试夹具，不是实盘交易建议或委托指令。请求体的 `mode` 是沿用的协议字面值；它不能把隔离服务切到实盘，部署域由服务构造时确定。

| 字段 | 约束 |
|---|---|
| `client_order_id` | 调用方生成的唯一请求编号，1–128 字符，匹配 `[A-Za-z0-9_.:-]+`，提交、撤单共用编号空间 |
| `mode` / `order_type` | 固定 `real` / `limit`，不接受市价或默认成交价 |
| `symbol` | 六位数字字符串，保留前导零 |
| `side` | `buy` 或 `sell` |
| `price` | 正的十进制字符串，应使用两位小数；必须精确到分，不接受 JSON 浮点数 |
| `quantity` | 正整数股数，买入须为 100 的整数倍；卖出校验可卖数量而非总仓量 |
| `shareholder_scope` | 目标市场已绑定的股东标识，服务会比对当前账户证据，不使用请求值替代账户核验。只在可信服务端使用，不暴露给浏览器或公开日志 |

提交前记录原委托列表和不可变意图；重新核对输入及确认弹窗后才允许确认。`BROKER_ORDER_VISIBLE_UNFILLED` 表示已关联到一笔新可见委托，**不是独立成交凭证**；应同时读取 `broker_status`，不能从历史命名中的 `UNFILLED` 推断当前成交股数。回执包含 `client_order_id`、`status`、`reason`、`submitted`、`retryable`、`broker_contract_id`、`broker_status`；核验成功的委托另外返回代码、方向、限价、股数和委托日。`submitted: true` 可表示可能提交，不证明已受理。`independent_fill_verified: false` 和空成交字段不能用于计算成交量。

## 2. 单笔撤单

`POST /v1/real/orders/cancel`

```json
{
  "client_order_id": "fixture-cancel",
  "order_request_id": "fixture-order",
  "broker_contract_id": "FIXTURE-CONTRACT"
}
```

三个字段均为必填标识字符串，1–128 字符，匹配 `[A-Za-z0-9_.:-]+`。`client_order_id` 是本次撤单的新编号；`order_request_id` 是原下单编号；`broker_contract_id` 必须原样来自原始委托回执。不支持批量、按股票/方向撤单，也不支持撤销本服务无已核验提交回执的历史委托。

仅在新鲜、完整的当前委托表中找到一笔严格匹配的委托（账户、交易日、合同号、代码、方向、价格、数量）才允许尝试。撤单入口须绑定到确切的委托行；确认窗口也须匹配同一委托。不能以「撤买」「撤卖」「全撤」或坐标点击兜底。最终以同一合同的明确已撤状态和自洽撤销量验证；行消失、只看到弹窗、或只收到点击成功都不算已撤。

| 回执字段 | 解释 |
|---|---|
| `operation` | `CANCEL`；与原下单回执分开 |
| `order_request_id` / `broker_contract_id` | 原始下单编号 / 目标合同编号 |
| `cancel_attempted` | 已进入可能发生撤单界面操作的阶段，不证明点击或撤销已完成 |
| `cancel_verified` | 仅在目标合同的撤销状态及数量证据通过时为 `true` |
| `BROKER_ORDER_CANCELLED` | 已验证目标合同撤销。若 `reason: ALREADY_CANCELLED`，本请求未再点击 |
| `BLOCKED` | 操作在进入尝试阶段前停止，或委托已不可撤 |
| `UNKNOWN` | 操作未能完成验证；可能已发生一部分操作。保留原请求，只查回执，不重复提交 |

## 3. 回执与错误

状态 GET 的查询参数 `client_order_id` 必填，且只能有此参数。查下单用下单编号，查撤单用撤单编号。请求内容一致且编号已在账本中时，POST 返回原回执而不重复执行；日常查询只用 GET。编号复用但内容不同返回 `409 REQUEST_ID_CONFLICT`。重启前未核实的操作保留为 `UNKNOWN`，此时回执可能只有通用字段和恢复原因。`UNKNOWN` 阻止新交易写入，不因重启或换请求编号解除。

常见 HTTP 错误：`400 INVALID_REQUEST` / `INVALID_REQUEST_ID`（字段或编号不合法）、`401 UNAUTHORIZED`、`403 FORBIDDEN_ORIGIN`、`404 ORDER_NOT_FOUND`（账本无此编号）、`409 CANCEL_TARGET_MISMATCH`（原始回执不对应）、`409 ACCOUNT_UNKNOWN`（未决交易阻止新写入）、`409 BUSY`（同一执行队列忙）。断线、非预期响应以及本地回执 `UNKNOWN` 都不能自动重发。即使在盘后，券商仍可能接收夜市委托，且撤单并非必然可用。

### 账本状态

`GET /v1/real/orders/journal-status` 不接受参数，不访问 AX，不进入桌面执行队列，不写入或清除账本。字段是同一次 SQLite 查询的快照：

```json
{
  "status": "TRADE_JOURNAL_READ_ONLY",
  "request_count": 2,
  "unresolved_count": 1,
  "in_flight_count": 0,
  "new_requests_blocked_by_unknown": true,
  "execution_domain": "SYNTHETIC",
  "execution_enabled": false
}
```

`request_count` 统计本地所有请求（含下单、撤单和被阻断的已记录请求），不是券商委托笔数；`unresolved_count` 统计 `UNKNOWN`；`in_flight_count` 统计 `CLAIMED`、`PREPARED`、`SUBMITTING`、`CONFIRMING`。`new_requests_blocked_by_unknown: false` 只表示未发现 `UNKNOWN`，不表示队列空闲、资金充足、账户已登录或允许交易。账本不可读取时返回 `503 TRANSACTION_STATE_UNAVAILABLE`。

## 4. 验收范围

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s tests -v
```

`test_trade_api.py` 使用真实回环 HTTP、Broker 和持久化账本，配合隔离交易适配器验证请求、精确合同撤单、异常及重启去重。`test_trade_native.py` 使用同一个原生 AX 适配层和合成界面核对表单输入、下单确认、单行撤单、独立已撤读回及禁止回退行为。两者均不连接账户、不点击真实桌面。实盘还需核验当前客户端的单笔撤单控件及合同确认弹窗、同时满足账户/委托证据和私有实盘账本准入；不能把测试配置改标成实盘，也不能用旧批量控件凑验收。
