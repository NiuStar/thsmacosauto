# thsmacosauto

独立的同花顺 macOS AX 自动化 HTTP 服务。股票系统在服务端通过 HTTP 调用，本项目不依赖股票网页、策略、行情采集、数据库或其登录会话。首版不提供买入、卖出、撤单或量化执行接口。

## 安装与启动

需要 Python 3.11+、macOS 图形登录会话和实际运行 Python 的辅助功能权限。当前原生定位限定为 `/Applications/同花顺.app`，同花顺 5.3.5（7.0.62）、天府证券。不同版本或窗口不匹配时拒绝，不做坐标点击。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
```

复制 `examples/config.example.json` 到仓库外的私有目录，改成实际绝对路径。配置、令牌、账户绑定和可选登录密码文件应由运行用户持有，文件 `0600`、各文件直接父目录 `0700`。用可信随机源生成独立 Bearer 令牌，例如在私有目录中运行 `(umask 077; openssl rand -hex 32 > bearer)`。已有账户绑定可在同一主机就地使用，也可按 [配置说明](docs/configuration.md) 离线生成。不要提交这些文件。

```sh
.venv/bin/thsmacosauto --config /absolute/private/config.json --check-config
.venv/bin/thsmacosauto --config /absolute/private/config.json
```

`--check-config` 不启动 HTTP、不访问 AX、不占用 GUI 锁。正式启动前，必须核实且明确填写 GUI 写入者清单，使用已有服务的同一个 canonical owner 目录并确保旧服务已退出，保留原登录尝试账本。**不能换一个 owner 目录来与现有服务并跑。** 新装环境才创建新的私有 owner 目录；空的 `known_writer_markers` 只表示操作者已核实没有其他 GUI 写入者。此版没有继承某台机器专用的服务停机/交接脚本。

## HTTP API

仅绑定回环；所有路由都需要 `Authorization: Bearer …`，返回 JSON、`Cache-Control: no-store`。供本机服务端调用，不应把令牌下发给浏览器，也不要将端口直接暴露到公网。跨设备请在部署时独立设计受控的服务器侧代理或隧道。

| 方法和路由 | 用途 |
|---|---|
| `GET /v1/health`、`GET /v1/capabilities` | 进程状态、可调用路由；不证明桌面已登录 |
| `GET /v1/login/status` | 被动观察当前交易登录状态，不输入、不提交 |
| `POST /v1/operator/trade-page` | `{"action":"ensure-login"}` 显式一次登录请求；也支持 `inspect`、`switch`、`login-inspect`、`login-open`、`retry-login`、`fresh-login`，后两项保留既有历史授权和账本前置条件 |
| `GET /v1/real/native-current-panel`、`GET /v1/real/positions-panel` | 当前面板、被动读取已显示的持仓；`total` 与 `available` 分开 |
| `GET /v1/real/positions`、`GET /v1/real/positions-scope-diagnostic` | 严格持仓与账户覆盖诊断；可能切换面板，默认不允许导航 |
| `GET /v1/real/account-observation`、`GET /v1/real/orders`、`GET /v1/real/trades` | 资金/账户观察、委托、成交；账户范围不足时拒绝，可能按配置切页后复位 |
| `GET /v1/real/holdings-refresh-controls` | 仅识别当前持仓页上的唯一合格刷新按钮，不点击 |
| `POST /v1/real/holdings-refresh` | 发送 `{"request_id":"唯一操作标识","control_fingerprint":"识别接口返回的原值"}`，同一启动周期至多一次点击 |
| `GET /v1/real/holdings-refresh-status` | 读取本进程的刷新回执，不调用 AX |

持仓面板是 `CLIENT_TABLE_ONLY`；其读取时间不是券商更新时间，不保证完整账户覆盖。刷新 token 有效期 30 秒；按钮、窗口、账户或覆盖无法核验时拒绝。`REFRESH_DISPATCH_ACKNOWLEDGED` 仅表示 AX 点击和随后的界面读回，不证明券商已完成查询。请求超时或结果不明，只查询回执，不重发 POST；刷新回执不跨进程保留，不用重启来重试。登录历史另有持久化记录，不覆盖旧 `UNKNOWN`。

股票系统接入：在后端配置 `http://127.0.0.1:18880` 和 Bearer 文件路径，保留自己的用户鉴权、估值与策略逻辑。按需要读取接口；登录和刷新 POST 只由用户明确操作触发。服务内部的 `_broker` / `_ax` 不是供股票系统 import 的公共 API。

## 验证范围

```sh
python3 -B -m unittest discover -s tests -v
```

测试使用回环 HTTP、真实 Broker 队列及合成 AX 夹具，不登录、不点击真实客户端。独立包未接管生产，刷新按钮是否符合真实界面的定位条件仍需单独验收。未包含真实账户数据、运行状态、凭据、股票系统或部署脚本。移植来源见 `SOURCE-PROVENANCE.json`；内置的 pymacauto 代码保留其 [MIT 许可](thsmacosauto/_ax/LICENSE.txt)。
