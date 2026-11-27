# 私有配置

示例配置默认禁止导航，且要求操作者核实单一 GUI owner 后才能启动。`known_writer_markers` 列出已知其他客户端写入者的 marker 绝对路径；不能用空清单掩盖现存服务，也不要删除 marker 或登录账本来通过检查。`allow_navigation=true` 才允许界面切换及持仓按钮的单次刷新；仍没有交易许可。

需要显式桌面登录时，设置 `login_secret_file` 指向本机私有 JSON 文件：`{"schema":"THS_LOGIN_SECRET_V1","password":"本机填写，不提交"}`。不通过 HTTP 传入密码、验证码或账户编号。密码由实际点击登录时读取；没有该文件的配置也能查询已登录的账户。凭据、配置和账户绑定均在本机仓库外保管。

已有冻结单账户绑定可就地使用。如果没有，可在确认券商和同一证券账户的市场股东标识后，在可信 Mac 上运行 `python -m thsmacosauto.provision_policy --output /absolute/private/account-policy.json`，按无回显提示输入深圳股东标识及可选上海标识。工具只创建一个本地 `0600` 文件，不覆盖已有文件，不连接同花顺，也不会验证输入标识的券商关联。不能用配置中的预期值代替实际界面上的账户核验。

读取示例（在股票系统**服务端**执行；默认端口）：

```python
import json
from pathlib import Path
from urllib.request import Request, urlopen

token = Path('/absolute/private/bearer').read_text().removesuffix('\n')
req = Request('http://127.0.0.1:18880/v1/real/positions-panel',
              headers={'Authorization': f'Bearer {token}'})
with urlopen(req, timeout=20) as response:
    result = json.load(response)
```

HTTP 401 代表服务令牌不符，不代表同花顺掉线；409 `BUSY` 表示 GUI 队列当前有其他操作；503 表示本次原生观察不可用。业务拒绝也可用 HTTP 200 加固定 `status`，调用方必须检查状态、覆盖和时间，不能把错误或缺行当作空仓。
