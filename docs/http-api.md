# thsmacosauto HTTP 接口文档（v1）

本文面向股票系统**服务端**开发者，描述功能拆分后的独立服务，**不代表旧版源码或线上实例已包含这些新路由**。同花顺桌面数据来自 macOS 辅助功能（AX），不是券商官方查询接口。接口绑定本机单一预配置账户，不接受通过请求切换券商或账户；不提供买入、卖出、撤单或量化执行。JSON 示例均为合成数据或占位值。

- [连接与报文约定](#transport)
- [接口速查](#routes)
- [健康与能力](#health)
- [登录与登录状态](#login)
- [持仓列表](#holdings)
- [刷新持仓、控件识别与回执](#refresh)
- [扩展查询与旧版兼容](#compatibility)
- [HTTP 错误与超时](#errors)
- [股票系统接入示例](#integration)

<a id="transport"></a>
## 1. 连接与报文约定

| 项目 | 约定 |
|---|---|
| 基础地址 | `http://127.0.0.1:18880`，以实际 [服务配置](configuration.md) 为准；仅监听回环地址 |
| 鉴权 | 每个请求都必须带一个 `Authorization: Bearer <令牌>`，包括健康检查和回执。令牌从服务端私有文件读取；不是同花顺密码，也不下发给浏览器 |
| 请求方法 | 只支持文档列出的 `GET` / `POST`。精确匹配路径，不加尾部 `/`，不带查询参数或片段。GET 不发送请求体 |
| POST 报文 | UTF-8 JSON 对象；`Content-Type: application/json`（也接受 `application/json; charset=utf-8`）；由 HTTP 客户端设置 `Content-Length`。不支持 `Transfer-Encoding: chunked`、`Expect: 100-continue`。普通上限 32 KiB，旧操作入口上限 64 字节 |
| JSON 校验 | 不接受重复键、额外字段、非对象顶层或尾随 JSON；`{}`、空文本、`null` 是不同的输入 |
| 响应 | `Content-Type: application/json; charset=utf-8`，`Cache-Control: no-store`。HTTP `200` 仍需检查业务 `status`；非 `200` 检查 `code`，不要解析英文 `message` 做业务决策 |
| 浏览器 | 股票系统应通过自己的后端调用。POST 不接受浏览器 `Origin` 头；不支持 CORS 预检。跨机器只通过受控的服务端代理或隧道接入 |
| 并发 | 原生操作与刷新回执走同一个 Broker 工作队列。忙时可能直接返回 `409 BUSY`，不要把它当成“已排队”。不同请求的结果不是原子快照 |

服务进程在线、配置了凭据、当前 GUI 登录已验证、已读取客户端持仓、券商已更新数据，是五件不同的事。不能互相推导。字段缺失和 `null` 也不能默认成 `false`、`0` 或空仓。

<a id="routes"></a>
## 2. 接口速查

| 方法 | 路径 | 用途与副作用 |
|---|---|---|
| GET | `/v1/health` | 服务进程健康；不检查 GUI |
| GET | `/v1/capabilities` | 查询当前部署开放的路由和配置标志；不检查 GUI |
| GET | `/v1/login/status` | 被动观察当前交易登录状态；不切页、不输入、不提交 |
| POST | `/v1/login` | 显式发起登录；必要时切到交易页、打开登录框并执行受控登录 |
| GET | `/v1/holdings` | 只读当前持仓面板；不刷新、不切页、不登录 |
| GET | `/v1/holdings/refresh/controls` | 识别符合条件的刷新控件，返回短期令牌；不点击 |
| POST | `/v1/holdings/refresh` | 显式尝试点击一次刷新；不切页、不登录，返回回执而非持仓列表 |
| GET | `/v1/holdings/refresh/status` | 读进程内回执，不访问 AX，不初始化原生适配器 |

账户、委托、成交、范围诊断和旧路径见[第 7 节](#compatibility)。接入前以目标实例的 `/v1/capabilities` 为准；候选源码包含路由，不代表线上实例已升级。

<a id="health"></a>
## 3. 健康与能力

两者均无请求体，不构造原生适配器。

### `GET /v1/health`

```json
{
  "status": "ok",
  "engine": "python-pymacauto",
  "execution_enabled": false,
  "operation_mode": "READ_ONLY",
  "transaction_state": "UNADMITTED",
  "transaction_domain": null,
  "account_unknown": null,
  "history_verified": false,
  "control": null,
  "reservations": null
}
```

`status` 可为 `ok` 或 `degraded`。其余字段描述未启用交易执行的服务状态；例如 `account_unknown: null` 不能用来判定账号是否在线。即使是 `200` + `ok`，也不能证明有辅助功能权限、同花顺已打开或已登录。

### `GET /v1/capabilities`

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | string | `READ_ONLY` |
| `execution_enabled` | boolean | `false`；登录和刷新控制不等于交易执行权限 |
| `get` / `post` | string[] | 当前实例按方法公布的精确路径，含兼容路径 |
| `navigation_enabled` | boolean | `allow_navigation` 配置值；`true` 不是某次 AX 操作的许可证明 |
| `login_secret_configured` | boolean | 是否配置了私有密码文件路径；不表示文件存在、可读、内容正确或登录成功 |
| `native_readiness` | string | `NOT_CHECKED`；此调用不探测原生就绪性 |

<a id="login"></a>
## 4. 登录与登录状态

### `GET /v1/login/status` — 观察状态

无需任何参数或请求体。只观察当前同花顺交易会话，不读取密码文件，不填表，不点击，不因观察失败触发登录。

响应示例（节选）：

```json
{
  "status": "ALREADY_LOGGED_IN",
  "already_logged_in": true,
  "authenticated": true,
  "authentication_state": "VERIFIED",
  "observed_at": "2026-11-27T13:28:42.627+00:00",
  "provenance": "GUI_CURRENT",
  "press_attempted": false,
  "execution_enabled": false
}
```

| 字段/状态 | 含义与调用方处理 |
|---|---|
| `ALREADY_LOGGED_IN` | 配合 `already_logged_in: true`、`authenticated: true`、`authentication_state: "VERIFIED"`，表示本次 GUI 核验成功。不代表其他会话，也不保证持续在线 |
| `LOGIN_ENTRY_INSPECTED` | 观察已执行，`already_logged_in: false`。可能是登录入口、登录框或没有足够证据；不是确定的掉线判定。此分支可能不返回 `authenticated`、`authentication_state`、`observed_at` 或 `provenance` |
| `LOGIN_COMMUNICATION_ERROR` | GUI 中观察到已识别的通讯异常；不是确认密码错误，也不授权重试提交 |
| `LOGIN_RESULT_UNRECOGNIZED` | 当前结果、窗口或弹窗未能可靠认定。显示“登录状态未验证”，不自动点击或提交 |
| `observed_at` / `provenance` | 可选，分别为 ISO 8601 的 UTC 观察时刻与 `GUI_CURRENT`；不是券商会话开始时间 |
| `login_control_count` / `login_controls` | 识别出的登录入口数量与诊断摘要；非零不证明已登录 |
| `auth_forms` / `known_labels` | 客户端表单结构摘要、固定标签出现次数；仅用于诊断，不包含密码或验证码原值，不用于业务判定 |
| `authentication_unsupported` | 状态/检查路径不做的操作列表，不代表整个服务没有显式登录入口 |

登录状态 GET 不是登录尝试账本查询。POST 后独立观察到在线可以更新显示，不能反推某个请求的提交结果，不能清除历史 `UNKNOWN`。

### `POST /v1/login` — 显式发起登录

请求体必须**正好是空对象**：

```json
{}
```

不接受 `action`、账户、密码、验证码或 `request_id`。授权只允许显式发起这一次普通登录流程。已登录则直接返回，不读密码，也不操作界面；需要输入时由服务使用本机配置的 `login_secret_file`，并要求界面、所有权、账本等前置条件通过。实际桌面操作需要 `allow_navigation=true`。调用失败不保证没有发生过切页、打开对话框或输入。

响应示例（已登录）：

```json
{
  "status": "ALREADY_LOGGED_IN",
  "steps": ["ALREADY_LOGGED_IN"],
  "press_attempted": false,
  "execution_enabled": false,
  "transaction_qualified": false,
  "already_logged_in": true,
  "authenticated": true,
  "authentication_state": "VERIFIED",
  "observed_at": "2026-11-27T13:29:31.925+00:00",
  "provenance": "GUI_CURRENT"
}
```

| 字段 | 含义 |
|---|---|
| `status` | 这一次命令的终态，按下表判断。不能套用 GET 状态响应的字段必填规则 |
| `steps` | 有界的阶段状态数组（最多 8 项）；不是逐个 AX 动作的完整审计。中间出现 `LOGIN_DIALOG_OPENED` 不代表已登录 |
| `press_attempted` | 最终分支报告的点击尝试标志；成功提交路径会上报尝试。`false` 不能证明整个编排从未切页、打开登录框或输入 |
| `authenticated`、`already_logged_in`、`authentication_state`、`observed_at`、`provenance` | 可选的观察字段；`LOGIN_VERIFIED` 不要求这些字段一定出现 |
| `ax_challenge` | 可选的验证码控件核验摘要；不含验证码原值 |
| `execution_enabled` / `transaction_qualified` | 都是 `false`；不授予下单资格 |

| 常见业务 `status`（HTTP 200） | 含义 | 调用方动作 |
|---|---|---|
| `ALREADY_LOGGED_IN` | 本次命令发现已登录，未发起输入 | 可以更新界面；需要最新状态时调用状态 GET |
| `LOGIN_VERIFIED` | 本次流程提交后通过 GUI 登录验证 | 显示登录已验证；持仓仍需单独读取 |
| `CREDENTIAL_REQUIRED` | 本机密码配置未通过读取或校验 | 检查私有配置和权限；不要把密码放进请求 |
| `NAVIGATION_DISABLED` | 当前配置不允许所需的桌面操作 | 检查服务端配置，不用其他接口绕过 |
| `LOGIN_FORM_UNQUALIFIED`、`CHALLENGE_UNAVAILABLE`、`LOGIN_ENTRY_BLOCKED`、`LOGIN_TARGET_CHANGED` | 表单、验证码、入口、弹窗或目标连续性未通过 | 保持未验证；只做被动诊断，不自动提交 |
| `LOGIN_UNKNOWN`、`LOGIN_COMMUNICATION_ERROR`、`NATIVE_SESSION_UNCERTAIN` | 本次未取得可信成功结论，可能已进行 GUI 操作 | 禁止自动重发；有限次数查询 `/v1/login/status`，保留原命令结果 |
| `LOGIN_ALREADY_CLAIMED`、`LOGIN_RETRY_USED`、`LOGIN_RETRY_COOLDOWN`、`LOGIN_FRESH_AVAILABLE`、`LOGIN_FRESH_USED`、`LOGIN_FRESH_BLOCKED`、`LOGIN_RECOVERY_COOLDOWN` | 历史记录、冷却或恢复准入阻止当前流程 | 发布诊断状态，交由操作者核对。`AVAILABLE` 不是同意自动恢复；不得删除账本或用重启规避 |

其他状态、HTTP 错误、超时、断线或无法解析的响应，都不应被自动判为成功或触发再次 POST。服务保留持久化登录尝试记录；旧的 `UNKNOWN` 不会因 GET、HTTP 客户端重启或服务重启而自动解除。

<a id="holdings"></a>
## 5. 获取持仓列表：`GET /v1/holdings`

无请求参数。要求当前同花顺已经显示交易**持仓**面板，券商、账户、表格及观察过程通过核验。不要求先调用刷新，也不会自动切到持仓、提交登录或改动客户端查询过滤器。若当前显示别的受识别面板，返回 `503 POSITIONS_PANEL_REQUIRED`；页面无法识别时也可能返回其他原生错误。

成功示例：可见沪市股东范围符合预配置，但缺少另一个配置市场的可见证据；有 200 股，当前可卖为 0 股。

```json
{
  "status": "POSITIONS_PANEL_READ_ONLY",
  "mode": "real",
  "panel": "持仓",
  "execution_enabled": false,
  "captured_start_ms": 1795786171636,
  "captured_end_ms": 1795786171699,
  "captured_at_ms": 1795786171636,
  "freshness_basis": "CLIENT_TABLE_ONLY",
  "upstream_freshness": "UNVERIFIED",
  "broker_as_of_ms": null,
  "scope_status": "FIXED_SCOPE_INCOMPLETE_OBSERVATION",
  "observed_market_count": 1,
  "missing_market_count": 1,
  "positions_complete": false,
  "completeness_scope": "AX_EXPOSED_ROWS_ONLY",
  "account_or_history_complete": false,
  "query_filters": "CURRENT_UI_FILTERS_NOT_CHANGED",
  "cross_table_atomic": false,
  "table": {
    "row_count": 1,
    "column_count": 6,
    "stable_double_read": true,
    "row_column_crosscheck": true,
    "completeness_scope": "AX_EXPOSED_ROWS_ONLY"
  },
  "positions": [
    {
      "symbol": "600000",
      "name": null,
      "total": 200,
      "available": 0,
      "cost": "10.50",
      "market_value": null,
      "current_price": null,
      "today_qty": null,
      "yesterday_qty": null
    }
  ]
}
```

| 顶层字段 | 类型 | 含义 |
|---|---|---|
| `status`、`mode`、`panel` | string | 成功状态固定为 `POSITIONS_PANEL_READ_ONLY`，真实适配器 `mode: "real"`，合成环境可为 `synthetic`；面板为 `持仓` |
| `captured_start_ms`、`captured_end_ms`、`captured_at_ms` | integer | 本地 Unix 毫秒时间；`captured_at_ms` 等于采集开始时间，不是券商更新时间 |
| `freshness_basis`、`upstream_freshness`、`broker_as_of_ms` | string、string、null | 仅反映客户端表，券商侧新鲜度未验证，无券商截至时间 |
| `scope_status` | string | `FIXED_SCOPE_COMPLETE_OBSERVATION` 表示所有配置市场均获得匹配的可见证据；`FIXED_SCOPE_INCOMPLETE_OBSERVATION` 表示缺市场证据但已观察部分未发生冲突。二者均不保证全账户持仓完整 |
| `observed_market_count` / `missing_market_count` | integer | 有可见股东标识证据的市场数 / 缺少可见证据的配置市场数；不是证券数或持仓总数 |
| `positions_complete` / `account_or_history_complete` / `cross_table_atomic` | boolean | 均为 `false`；不提供完整仓位、历史或跨表原子性保证 |
| `table` | object | AX 表行数、列数、稳定双读和行列交叉校验；普通持仓接口不返回原表哈希。双读稳定仅代表观察窗口内表格一致 |
| `query_filters` / `completeness_scope` | string | 只使用客户端当前过滤条件，不改动；覆盖限于 `AX_EXPOSED_ROWS_ONLY` |

| `positions[]` 字段 | 类型 | 含义 |
|---|---|---|
| `symbol` | string | 六位证券代码，须保留前导零 |
| `name` | string / null | 证券名称，未暴露时为 `null` |
| `total` | integer | 客户端显示的总持仓股数，对应“股票余额” |
| `available` | integer | 客户端显示的可卖股数，对应“可用余额”；`0 <= available <= total` |
| `cost` / `market_value` / `current_price` | 十进制字符串 / null | 分别取自客户端成本价、市值、市价列；可空，不应以盘口实时价或浮点零值补写 |
| `today_qty` / `yesterday_qty` | null | 当前未提供日内 / 昨仓拆分 |

`total > 0` 且 `available == 0` 是“仍持有但当前不可卖”，不是空仓；如仅展示当前持有行，可在展示层过滤 `total == 0`，不要篡改原始观察。`positions: []`、缺市场、接口报错或不在持仓页均不能推出全账户空仓。`FIXED_SCOPE_CHANGED` 代表范围冲突，应停止使用旧展示数据，不能靠切账户或再登录来掩盖。不要把任何该接口结果接入自动下单权限。

<a id="refresh"></a>
## 6. 刷新持仓：识别 → 提交 → 查回执

刷新只用于用户明确操作。前置条件包括：`allow_navigation=true`、当前已在持仓面板（不会自动切页）、当前配置市场的严格股东范围核验通过、控件唯一且安全、窗口及进程连续。**能读 `/v1/holdings` 不代表能刷新**：部分市场范围可用于持仓镜像，但刷新要求严格范围证明。

### `GET /v1/holdings/refresh/controls` — 识别控件

无请求参数。只观察并确认控件与表格关联，生成或更新当前令牌，不点击、不提交券商查询。不要在页面初始化或定时持仓读取时调用此接口。

```json
{
  "status": "REFRESH_CONTROL_QUALIFIED",
  "control_fingerprint": "0000000000000000000000000000000000000000000000000000000000000000",
  "control": {
    "role": "AXButton",
    "AXTitle": null,
    "AXDescription": "刷新持仓",
    "AXHelp": null,
    "enabled": true,
    "supported_action": "AXPress",
    "association": "DIRECT_SIBLING_OF_HOLDINGS_TABLE"
  },
  "table": {"row_count": 1},
  "token_ttl_seconds": 30,
  "one_shot": true,
  "query_performed": false,
  "upstream_freshness": "UNVERIFIED",
  "broker_as_of": null,
  "execution_enabled": false
}
```

`control_fingerprint` 是服务发的 64 位小写十六进制**不透明令牌**，不是控件名或客户端计算的哈希。上文全零只用于展示格式，不能提交。`AXTitle`、`AXDescription`、`AXHelp` 可能为 `null`；不要用它们自行找控件。令牌有效期从最近一次成功识别计约 30 秒，提交后实际点击前仍要再次检查。重复识别时同一控件可能沿用令牌，环境变化时可能换令牌；不要并行发起多组刷新。

`200 REFRESH_ALREADY_CLAIMED` 表示本进程已消耗刷新机会：返回 `one_shot: true`、`receipt` 对象、`execution_enabled: false`，不再返回新令牌。

### `POST /v1/holdings/refresh` — 提交一次刷新

```json
{
  "request_id": "holdings-refresh-001",
  "control_fingerprint": "0000000000000000000000000000000000000000000000000000000000000000"
}
```

| 请求字段 | 必填 | 类型与约束 |
|---|---|---|
| `request_id` | 是 | 字符串，1–128 字符，匹配 `[A-Za-z0-9_.:-]{1,128}`。调用方为一次用户操作生成唯一 ID，用来与回执匹配 |
| `control_fingerprint` | 是 | 字符串，匹配 `[0-9a-f]{64}`，原样使用本实例识别返回的值 |

无其他字段。服务在重新核验控件之前先消耗通过初步检查的机会，**每进程生命周期最多一个领用**，即使后续在点击前阻断，也不会发放第二次机会。相同 `request_id` **及** `control_fingerprint` 的重复请求只会返回原回执，不再点击；其他请求得到 `503 REFRESH_ALREADY_CLAIMED`。这不是跨进程幂等协议，不应使用重复 POST 查询结果。

### `GET /v1/holdings/refresh/status` — 查询回执

无请求参数，不接受 `?request_id=...`。读本进程唯一一份刷新回执，无 AX 查询，不要求当前 GUI 可用；仍受工作队列并发限制。尚无领用记录时返回：

```json
{"status":"REFRESH_NOT_CLAIMED","execution_enabled":false}
```

已领用时，返回与 POST 相同结构的回执。以 `request_id` 核对是否为本次操作；收到不匹配、未领用或无法读取，都不构成重发许可，尤其不能跨进程假设连续性。

```json
{
  "status": "REFRESH_DISPATCH_ACKNOWLEDGED",
  "request_id": "holdings-refresh-001",
  "dispatch_attempted": true,
  "dispatch_acknowledged": true,
  "retry_allowed": false,
  "query_completion": "UNVERIFIED",
  "upstream_freshness": "UNVERIFIED",
  "broker_as_of": null,
  "execution_enabled": false,
  "monetary_authority": "NONE",
  "before_table": {"row_count": 1},
  "after_table": {"row_count": 1},
  "table_observation": "CLIENT_TABLE_UNCHANGED"
}
```

| 回执字段/状态 | 含义 |
|---|---|
| `REFRESH_DISPATCH_ACKNOWLEDGED` | 原生点击调用返回且后续客户端表格读取通过核验；只证明“点击与读回”，不证明券商刷新完成 |
| `REFRESH_BLOCKED` | 已领用但在点击尝试前阻断，`dispatch_attempted: false`；不能再次领用 |
| `REFRESH_UNKNOWN` | 已进入点击尝试边界，但整体结果不确定。即使 `dispatch_acknowledged: true`，点击后的核验仍可能失败；不能补发 |
| `dispatch_attempted` / `dispatch_acknowledged` | boolean，分别表示进入点击尝试边界 / 调用已返回。不能把“尝试”当成已执行，也不能把“已返回”当成券商完成 |
| `retry_allowed` | 已领用回执固定为 `false` |
| `before_table` / `after_table` | 可选对象，仅含 `row_count`；未完成相关阶段时可缺失 |
| `table_observation` | 可选，`CLIENT_TABLE_CHANGED` 或 `CLIENT_TABLE_UNCHANGED`；表格相同或变化均不证明券商数据更新 |
| `query_completion` / `upstream_freshness` / `broker_as_of` | `UNVERIFIED` / `UNVERIFIED` / `null`。刷新接口字段叫 `broker_as_of`，与持仓的 `broker_as_of_ms` 不同 |
| `execution_enabled` / `monetary_authority` | `false` / `NONE`，无交易或资金权限 |

若 POST 返回 503、超时、断线或响应不完整，**改查回执 GET，不重发 POST、不自动重新识别、不重启来重试**。回执是内存状态，不跨重启。即使看见 `REFRESH_DISPATCH_ACKNOWLEDGED`，也要独立请求 `/v1/holdings` 读取列表，并保留“券商更新时间未验证”标识。

<a id="compatibility"></a>
## 7. 扩展查询与旧版兼容

### 扩展查询

均为 GET，无参数、无请求体、不执行交易或自动登录。除当前面板观察外，以下部分查询可能切到目标面板并尝试复位；`allow_navigation=false` 且需要切页时拒绝。不要把可能切页的接口作为高频持仓轮询入口。每次响应只反映本次 AX 观察，不保证完整行情、账户、历史或跨接口原子性。

| 路径 | 成功 `status`（真实运行） | 主要响应字段与限制 |
|---|---|---|
| `/v1/real/native-current-panel` | `READ_OK` | `panel`、`captured_start_ms`、`captured_end_ms`、`table`（行列数量、`sha256`、双读与交叉校验）及通用覆盖字段；不返回持仓列表，不切页 |
| `/v1/real/positions` | `REAL_POSITIONS_READ_ONLY` | 桌面持仓表的**严格范围**版本。`account_scope`、`positions`（字段同普通持仓）、`mode`、`captured_at_ms`、`table`、`positions_complete: false`。缺市场证据时拒绝，可按配置切到持仓 |
| `/v1/real/positions-scope-diagnostic` | `POSITIONS_SCOPE_DIAGNOSTIC_READ_ONLY` | `scope_status`、`observed_market_count`、`missing_market_count`、`observed_row_count`，不返回持仓行或原始股东号。可能报告 `FIXED_SCOPE_CHANGED`（有些冲突直接以 503 拒绝），可按配置切到持仓 |
| `/v1/real/account-observation` | `READ_OK` | `cash_available` 为十进制字符串；`funding_account: null`，`account_scope` 为绑定标识，`binding` 含 `domain`、`broker`、`account_scope`。依赖严格持仓核验和资金字段关联；不可作为下单额度或资金账户识别 |
| `/v1/real/orders` | `REAL_ORDERS_READ_ONLY` | `orders[]` 含 `contract_id`、`symbol`、`name`（可空）、`side`（界面原值）、`quantity`（股）、`price`（十进制字符串）、`status`（委托状态原值）、`order_date`、`order_time`；先证明持仓账户范围，再读当前过滤条件下的委托 |
| `/v1/real/trades` | `REAL_TRADES_READ_ONLY` | `trades[]` 含 `trade_id`、`contract_id`、`symbol`、`name`（可空）、`side`（`买入` / `卖出`）、`quantity`（股）、`price`、`amount`（十进制字符串）、`trade_date`、`trade_time`。`fill_evidence` 为 `AX_TRADE_REFERENCE_V1` 诊断，带可见表格和范围证据；`native_fill_qualified` 与 `independent_fill_verified` 都是 `false` |

扩展查询可能返回账户、委托、成交相关敏感证据（包括成交诊断的范围与表格数据）；不要将原始响应透传到浏览器、公共日志或对外接口。需显示时由业务服务选取最少字段。委托/成交日期与时间是客户端表字段，不是服务器采集时间；为当前可见行，不能当作完整历史或独立券商成交证明。

### 兼容映射

| 旧调用 | 优先使用 | 说明 |
|---|---|---|
| `GET /v1/real/positions-panel` | `GET /v1/holdings` | 同一个被动读取实现，不是严格 `/v1/real/positions` |
| `GET /v1/real/holdings-refresh-controls` | `GET /v1/holdings/refresh/controls` | 共用令牌 |
| `POST /v1/real/holdings-refresh` | `POST /v1/holdings/refresh` | 原请求体不变，共用单次领用限制 |
| `GET /v1/real/holdings-refresh-status` | `GET /v1/holdings/refresh/status` | 共用回执 |
| `POST /v1/operator/trade-page`，`{"action":"ensure-login"}` | `POST /v1/login`，`{}` | 同一登录流程与持久化账本 |


### 旧运维入口：`POST /v1/operator/trade-page`

请求体只接受 `{"action":"<动作>"}`，动作名区分大小写。普通业务不需要通过该入口操作。下表的状态为主要结果，不是完整枚举；统一检查 `status`，不能用 200 或 `press_attempted` 代替确认。

`login-status` 不是这个旧 HTTP 入口支持的动作；提交会返回 `400 BAD_REQUEST`。查状态请用 `GET /v1/login/status`，或使用下表中的 `login-inspect` 做旧版诊断。

| `action` | 行为和结果提示 |
|---|---|
| `inspect` | 被动检查交易页；`TRADE_PAGE_INSPECTED`，配合 `trade_page_verified`、`panel`、`trade_control_count` 与诊断字段判断，不点击 |
| `switch` | 显式切到交易页，不提交凭据；`TRADE_PAGE_VERIFIED`、`ALREADY_TRADE_PAGE` 为面板观察结果，`TRADE_LOGIN_ENTRY_VERIFIED` 只表示看到登录入口/表单。`SWITCH_UNCONFIRMED` 不应重发 |
| `login-inspect` | 被动登录诊断，保留较详细的观察结果，例如 `LOGIN_RESULT_UNAVAILABLE`；不点击 |
| `login-open` | 显式打开已通过核验的登录界面，必要时处理唯一获准的断线提示；不读取密码或提交；`LOGIN_DIALOG_OPENED` / `LOGIN_FORM_ALREADY_PRESENT` 都不表示已登录 |
| `ensure-login` | 等价于独立登录入口；普通业务迁往 `/v1/login` |
| `retry-login` | 独立的历史不确定尝试恢复入口，仅限运维明确操作，受历史记录、冷却、当前合格登录框和一次性持久化授权约束 |
| `fresh-login` | 独立的新一代登录恢复入口，须原有历史、输入前核验及独立账本通过；不是重置按钮。不可作为失败后的自动降级 |

<a id="errors"></a>
## 8. HTTP 错误与超时

错误响应遵循固定 `code` / `message` 形状；部分错误可以有额外诊断元数据，调用方应容忍未知字段。例：

```json
{"code":"POSITIONS_PANEL_REQUIRED","message":"Native observation is unavailable."}
```

| HTTP | 常见 `code` | 排查和处置 |
|---|---|---|
| 400 | `BAD_REQUEST`、`INVALID_JSON`、`INVALID_REQUEST` | 检查路径参数、请求头、JSON 对象和字段；额外字段常为 `BAD_REQUEST`，缺字段、格式不符或未知 `action` 可为 `INVALID_REQUEST` |
| 401 | `UNAUTHORIZED` | 服务 Bearer 鉴权失败；不是同花顺掉线 |
| 403 | `FORBIDDEN_ORIGIN` | POST 带不允许的 Origin；通过自己的后端发请求 |
| 404 | `NOT_FOUND` | 路由未部署、路径错误或不开放；先核对 `/v1/capabilities`，不要自动降级成其他 POST |
| 405 | `METHOD_NOT_ALLOWED` | 核对 GET/POST；不能使用 HEAD 或 OPTIONS 探活 |
| 408 | `REQUEST_TIMEOUT` | HTTP 请求读取超时；客户端本身或代理超时也可能根本没有响应 |
| 409 | `BUSY` | Broker 已被占用或正在收尾；只对被动查询做有限退避，不盲重发命令 |
| 411 / 413 / 415 | `LENGTH_REQUIRED` / `BODY_TOO_LARGE` / `UNSUPPORTED_MEDIA_TYPE` | 检查 `Content-Length`、大小、媒体类型；不要用流式 POST |
| 503 | `HTTP_BUSY`、`ENGINE_CLOSED`、`BACKEND_UNAVAILABLE`、`READ_BUDGET_EXCEEDED` | HTTP 容量、服务、后端或原生预算不足；查询结果未知，不当成空数据 |
| 503 | `AX_NOT_TRUSTED`、`APP_IDENTITY_MISMATCH`、`APP_EXECUTABLE_MISMATCH`、`GUI_OWNER_*` | 检查辅助功能权限、运行版本、桌面身份和单一所有权；不应另起桌面执行器绕过 |
| 503 | `POSITIONS_PANEL_REQUIRED`、`FIXED_POLICY_UNAVAILABLE`、`FIXED_SCOPE_INCOMPLETE_OBSERVATION`、`FIXED_SCOPE_CHANGED`、`STALE_READ_CAPTURE` | 分别核对当前页面、绑定配置、证据覆盖、身份冲突或采集时效。缺市场证据不等于账号错误或空仓 |
| 503 | `REFRESH_DISABLED`、`REFRESH_HOLDINGS_REQUIRED`、`REFRESH_STRUCTURE_UNPROVEN`、`REFRESH_CONTROL_UNQUALIFIED`、`REFRESH_CONTROL_CHANGED`、`REFRESH_TOKEN_INVALID`、`REFRESH_ALREADY_CLAIMED` | 检查配置、面板、控件、令牌与回执。特别是 `REFRESH_TOKEN_INVALID` 可能发生在领用前，也可能发生在领用后、点击前；查回执区分，不自动重试 |
| 500 | `INTERNAL_ERROR` | 保留业务“未验证”，检查服务端诊断；无需把底层异常暴露给前端 |

当前独立服务读取 HTTP 请求的默认期限为 5 秒；原生任务另有协作预算（工作阶段约 12 秒，含恢复/收尾的绝对预算约 15 秒）。这不是保证响应在 15 秒内到达。调用方可用约 20 秒的响应等待作为起点，并设置应用整体期限。网络断开不会撤销已启动的桌面操作。登录和刷新应使用单发请求，关闭代理对这些 POST 的自动重试。

<a id="integration"></a>
## 9. 股票系统接入示例

### curl 快速通信检查

在服务所在 Mac 的可信终端运行；先填写私有令牌文件的路径，不把实际令牌写入示例、日志或前端。这里的请求只读：

```bash
BASE='http://127.0.0.1:18880'
TOKEN_FILE='/absolute/private/bearer'
ths_get() {
  printf 'Authorization: Bearer %s\n' "$(<"$TOKEN_FILE")" |
    curl --noproxy '*' --max-time 20 -sS -i -H @- "$BASE$1"
}
ths_get /v1/capabilities
ths_get /v1/login/status
ths_get /v1/holdings
```

不要启用 shell `set -x` 或 curl 详细跟踪。命令输出含业务数据，不应复制到公共日志。下方 Python 示例把 POST 封装成只能由业务代码显式调用的函数。

### Python 服务端示例

无第三方依赖，给出最小调用约定；实际服务应接上自己的鉴权、操作确认与异常处理。模块加载时只定义函数，不登录、不刷新。

```python
import http.client
import json
from contextlib import closing
from pathlib import Path
from uuid import uuid4

HOST = "127.0.0.1"
PORT = 18880
TOKEN_FILE = Path("/absolute/private/bearer")


def call(method, path, payload=None):
    token = TOKEN_FILE.read_text(encoding="ascii").removesuffix("\n")
    headers = {"Authorization": "Bearer " + token}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with closing(http.client.HTTPConnection(HOST, PORT, timeout=20)) as connection:
        connection.request(method, path, body=body, headers=headers)
        with connection.getresponse() as response:
            data = json.load(response)
            if not isinstance(data, dict):
                raise ValueError("Expected an HTTP API JSON object")
            return response.status, data


def read_login_status():
    return call("GET", "/v1/login/status")


def request_login_once():  # 只由明确的用户操作调用；异常后只查状态
    return call("POST", "/v1/login", {})


def read_holdings():
    return call("GET", "/v1/holdings")


def request_refresh_once(request_id):  # 调用前保留 request_id = uuid4().hex
    code, control = call("GET", "/v1/holdings/refresh/controls")
    if code != 200 or control.get("status") != "REFRESH_CONTROL_QUALIFIED":
        return code, control
    return call("POST", "/v1/holdings/refresh", {
        "request_id": request_id,
        "control_fingerprint": control["control_fingerprint"],
    })


def read_refresh_receipt():
    return call("GET", "/v1/holdings/refresh/status")
```

### 推荐接入顺序

1. **启动检查**：读 `capabilities` 确认目标版本。健康检查失败时不发 GUI 命令；健康检查成功也不假定登录可用。
2. **打开持仓页**：按需读取登录状态与持仓，不自动登录或刷新。仅 `200 + POSITIONS_PANEL_READ_ONLY` 且字段合法时更新展示；保留采集时间和覆盖说明。不可读时明确显示旧结果/暂不可用，不能用 `[]` 覆盖为“空仓”。发现账户范围冲突时不保留旧数据用于展示。
3. **用户点登录**：单发 POST。`ALREADY_LOGGED_IN` / `LOGIN_VERIFIED` 之外均不得直接渲染成功。未知或超时时，只允许有限次数的独立状态 GET。
4. **用户点刷新持仓**：先分配本次 `request_id`，识别控件后单发 POST；对重复点击采用单飞保护。遇到错误或不确定，按已知 ID 核对回执；`BUSY` 只对 GET 做有限退避。确认界面读回后单独读持仓列表，不把读回标作“券商已更新”。
5. **最小观测**：日志记录端点、HTTP 状态、业务 `status`/错误 `code`、耗时和业务操作 ID 即可。不要记录 Authorization、验证码、刷新令牌或完整账户数据。

### 接入验收清单

- 无令牌 `401`；错方法 `405`；带密码字段的登录请求被拒绝。
- 未登录时查状态不打开登录框；已登录时显式登录不输入、不点击。
- 重复读持仓不切页、不登录、不刷新；`total > 0, available = 0` 正常显示“持有、不可卖”。
- 刷新新旧路径共用一次领用；超时后只查回执；回执与上游数据新鲜度分别展示。
- 先在隔离环境用合成 AX 跑 `python3 -B -m unittest discover -s tests -v`；真实版本、券商窗口、账户和实际刷新控件的验收与生产切换另行安排。
