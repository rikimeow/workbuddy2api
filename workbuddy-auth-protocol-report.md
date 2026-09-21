# WorkBuddy Desktop — Login / Token-Refresh / Keepalive / Turing 协议逆向报告

所有结论均标注来源文件与行号。**已验证事实**与**推断**严格分开标注。

## 0. 环境与基础常量（已验证）

`D:\WorkBuddy\resources\app.asar.unpacked\cli\product.json`（Desktop 主进程实际读的就是这个 `cli/product.json`）：

```json
"productName": "WorkBuddy",
"platform": "CLI",
"endpoint": "https://copilot.tencent.com",
"stagingEndpoint": "https://staging-copilot.tencent.com",
"officialEndpoints": ["https://copilot.tencent.com","https://staging-copilot.tencent.com","https://www.codebuddy.ai","https://staging-codebuddy.tencent.com"],
"authentication": {
  "id": "workbuddy-desktop",
  "type": "cli-external-link",
  "label": "TencentCloud",
  "attributes": {
    "usernameHeader": "X-User-Id",
    "usernameEncode": "URLEncode",
    "tokenHeader": "Authorization",
    "tokenType": "bearerToken",
    "prefixPath": "/plugin",
    "platform": "workbuddy"
  }
},
"config": { "turingSdk": { "channelId": 109144 },
            "qimei36AppKey": { "darwin": "0MAC070JLSCWV1CM", "win32": "0WIN070JLSH64IEE" } }
```

关键点：
- `prefixPath = "/plugin"` → 所有 auth 路径实际是 **`/v2/plugin/auth/...`**（Desktop `common.js:72944` `get prefixPath(){return this.configuration.authentication?.attributes?.prefixPath || ""}`）。
- `platform = "workbuddy"` → `/auth/state?platform=workbuddy`。
- Desktop 主进程读取路径：`E:\反代理\_wb_rev\main\workbuddy-product-config.js:524-533`（`readProductConfigurationFromDisk` → `resolveBundledAsset("cli","product.json")`）。
- 认证态持久化文件：`<sharedDataPath>/auth/workbuddy-desktop.info`（`file-authentication-storage.js:400-403`），旁路登出标记 `<file>.logged-out`（`getLogoutMarkerPath`）。开发模式 id 追加 `-dev`（`workbuddy-product-config.js:619-625`）。
- 加密落盘字段白名单：`auth.accessToken`、`auth.refreshToken`、`account.phoneNumber`、`account.departmentFullName`、`account.nickname` 等（`credential-protection.js:1507-1533`）。

---

## 1. Login / Auth 端点（已验证）

两条链路代码几乎逐字相同（Desktop daemon `main/common.js` 与 CLI `codebuddy.js`），端点一致。

| # | Method | Path（含 prefixPath） | 用途 | 关键请求头 | 关键响应字段 |
|---|--------|----------------------|------|-----------|-------------|
| 1 | POST | `/v2/plugin/auth/state?platform=workbuddy` | 取登录态/授权 URL | `X-No-Authorization:true`、`X-No-User-Id:true`、`X-No-Enterprise-Id:true`、`X-No-Department-Info:true`、`X-Domain`、trace 头 | `data.state`、`data.authUrl` |
| 2 | GET | `/v2/plugin/auth/token?state=<state>` | 轮询换取 token | 同上 4 个 `X-No-*` + trace 头 | `data` = AuthToken（见 §5） |
| 3 | GET | `/v2/plugin/login/account?state=<state>` | 轮询取账号 | `Authorization: Bearer <accessToken>`、`X-Domain`、`X-No-User-Id/Enterprise-Id/Department-Info:true` | `data` = Account |
| 4 | GET | `/v2/plugin/accounts` | 账号快照（刷新/切号用） | `Authorization: Bearer <accessToken>`、`X-Domain` | `data.accounts[]` |
| 5 | POST | `/v2/plugin/login/enterprise` 或 `/v2/plugin/login/enterprise/<enterpriseId>` | 切企业账号 | `Authorization: Bearer`、`X-Refresh-Token`、`X-Enterprise-Id`、`X-Tenant-Id` | `data` = 新 AuthToken |
| 6 | POST | `/v2/plugin/account/switch` | 切账号（account-scoped 变体） | `Authorization: Bearer`、`X-Refresh-Token`、`X-Domain`；body `{target_enterprise_id}`（个人号为空 `{}`） | `data` = 新 AuthToken |
| 7 | GET | `/v2/plugin/account` | 取当前账号（account-scoped 变体） | Bearer + `X-Domain` | `data` = Account |
| 8 | POST | `/v2/plugin/auth/token/refresh` | 刷新 token | `X-Refresh-Token: <refreshToken>`、`X-Auth-Refresh-Source: plugin`、`X-Domain` | `data` = 新 AuthToken |
| 9 | POST | `/v2/plugin/auth/token/wxide?tmpCode=<code>` | 微信 IDE 换 token | 仅 trace 头 | `data` = AuthToken |
| 10 | POST | `/v2/plugin/device/auth/code` | 微信小程序设备码 | `Content-Type: application/json` + auth headers | `data.deviceCode`、`data.expiresIn`（或 `expires_in`） |
| 11 | POST | `/v2/as/yuanbao/scan-bind-code` | 元宝扫码绑定 | 同上 | `data.scan_code`、`data.scan_url`、`data.expires_in` |
| 12 | GET | `/v2/activity/workbuddy/banner` | 活动 banner（需客户端版本白名单） | 见 §4 | — |
| 13 | GET | `/v2/billing/meter/checkin-activity-status`, POST `/v2/billing/meter/daily-checkin` | 签到（**带 Turing 设备 token**） | 见 §3 | — |
| 14 | POST | `/v2/activity/growth/buddy/info`, GET `/v2/activity/ambassador/status`, POST `/v2/feedback`, POST `/v2/chat/prediction`, POST `/v2/chat/queue/status`, POST `/v2/chat/queue/cancel` | 其他业务接口 | — | — |
| 15 | POST | `/v3/config`（`DEFAULT_REMOTE_CONFIG_PATH`） | 远端产品配置（登录后拉取，含 `productFeatures.TuringDeviceToken`） | `Authorization: Bearer`、`X-Product`、`X-Requested-With: XMLHttpRequest`、`Connection: close` | `data` = 产品配置 |

来源：
- Desktop: `common.js:72697-72725`（fetchAuthState）、`72764-72798`（loopGetToken）、`72799-72839`（loopGetAccount）、`72857-72878`（getAccountSnapshot）、`72650-72696`（switchAccount）、`73050-73103`（account-scoped refresh/switch/account）、`73004-73122`（account-scoped createSession/openAuthUrl）、`78036-78073`（wx tmpcode/accounts）
- CLI: 同结构位于 `codebuddy.js` @15981143（loopGetToken）、@16044694（wx refresh）、@16045884（wx fetchTokenByTmpCode/fetchAccounts）
- 远程配置：`workbuddy-auth-product-coordinator.js:488`（`DEFAULT_REMOTE_CONFIG_PATH="/v3/config"`）、`:887-906`（带 `Connection: close` 的鉴权拉取）
- 设备码/扫码：`server.js:54728-54806`

### 登录轮询参数（已验证）
`common.js:72472-72477`：
```js
SIGN_IN_PENDING_TIMEOUT$1 = 300 * 1e3;   // 300s
SIGN_IN_FETCH_INTERVAL$1 = 1e3;          // 1000ms
SWITCH_ACCOUNT_RETRY_TIMES = 1;
MAX_ACCOUNT_AUTH_RETRIES = 5;
RESPONSE_CODE_IP_LIMIT = 10081;
```
服务端错误码：`RetryFetchToken = 11217`（继续轮询 token）、`RetryFetchAccount = 12151`（继续轮询 account）、`LicenseSeatLimit = 12005`、`LicenseExpired = 11212`、`TrialExpired = 11216`（`common.js:72478-72489`）。
`/auth/state` 网络连通性探测超时 `5e3` ms（`common.js:72504`）。

### 授权 URL 附加参数（已验证）
`common.js:72726-72755`（Desktop）/ `73104-73122`（account-scoped）：
- `version=<pluginVersion>`（clientInfo.pluginVersion）
- `launch_schema=<process.env.launch_schema>`（仅当 `attributes.launchSchemaWhitelist` 为空或包含它）
- `loginSessionId=<globalLoginSessionId>`（仅 `platform.toLowerCase()==="workbuddy"`，用 `set` 而非 `append`）
- 另外 `server.js:7284892` 分支：申请用户组时会对 authUrl 追加 `applyGroup=true`

### 响应信封
统一 `{ code, msg, data, requestId? }`；成功 `code === 0`（大量 `response.data?.code === 0` 判定，如 `stdio-mcp-inspector.js:14223`）。

---

## 2. Token 刷新与"保活"（已验证）

### 2.1 刷新端点与请求头
```
POST /v2/plugin/auth/token/refresh
X-Refresh-Token: <session.auth.refreshToken>
X-Auth-Refresh-Source: plugin
X-Domain: <session.auth.domain>
<trace headers: X-Trace-ID / X-B3-* / b3>
body: {}
```
来源：`common.js:72612-72620`、`73054-73059`、`78004-78009`（Desktop）；`codebuddy.js` @16044694（CLI）。
微信提供者用 `/v2/auth/token/refresh`（**无 `/plugin` 前缀**，`common.js:72612` 所在的 Wx provider 版本为 `78004`；`common.js:73054` 与 CLI 均为 `/v2${prefixPath}/auth/token/refresh`）。⚠️ 这是两个不同 provider 的写法差异，实现时按 `/v2/plugin/auth/token/refresh` 为准。

### 2.2 刷新时机 / 提前量（核心，已验证）
`common.js:72054+`（Desktop）；CLI 同源 @16005061：
```js
MIN_REFRESH_DELAY_MS = 15e3;                       // 全局最小延迟
const last = session.auth.lastRefreshTime || 0;
if (Date.now() - last < 3e4 && !shouldRecoverWorkbuddyAccountMetadata(session)) return;  // 30s 内不重复刷新
if (!refreshTokenTimer) {
  let delay = 864e5 + Math.floor(10 * Math.random() * 6e4) - 3e5;   // 24h + rand(0..600000) - 300000
  if (expiresAt - 864e5 < Date.now()) delay = 3e5 + Math.floor(60 * Math.random() * 1e3); // 5min + rand(0..60000)
  let wait = (last + delay < Date.now()) ? 0 : (last + delay - Date.now());
  if (wait === 0) wait = MIN_REFRESH_DELAY_MS;
  wait = Math.max(wait, MIN_REFRESH_DELAY_MS);
  this.refreshTokenTimer = timeout(wait);
  await this.refreshTokenTimer;
}
```
即：
- **正常情况：每 ~24h 刷新一次**（86400000ms + 0~600000ms 抖动 − 300000ms），以 `lastRefreshTime` 为基准。
- **若 `expiresAt − 24h < now`（临时/短生命周期 token）：提前 5min + 0~60000ms 抖动**。
- 硬下限 15s；30s 内不重复。
- `lastRefreshTime = Date.now()` 在每次成功刷新/换 token/切号后写入（`common.js:72279`、`72676`、`72913`、`73173`、`78011`）。

### 2.3 失败重试（已验证）
`common.js:72289-72312`：
```js
retryDelay = Math.min(5e3 * Math.pow(2, Math.max(0, retryAttempt - 1)), 6e4); // 5s,10s,20s,40s,60s
// retryTimes 初值 5；UnauthorizedError 不重试
```

### 2.4 过期时间换算（已验证）
`common.js:72911-72914` / `73229-73232`：
```js
if (!authToken.expiresAt && authToken.expiresIn) authToken.expiresAt = Date.now() + authToken.expiresIn * 1e3;
if (!authToken.refreshExpiresAt && authToken.refreshExpiresIn) authToken.refreshExpiresAt = Date.now() + authToken.refreshExpiresIn * 1e3;
```

### 2.5 401 自动刷新
`common.js:73685-73692`：业务 API 收到 401 时，`AuthInterceptor.tryRefreshOnBusinessApi401` 调 `authenticationManager.refreshSession()`（调用方自己的请求不自动重放）。
`common.js:73598`：带 `X-Skip-Auth-Interceptor` 的请求直接跳过（该头会被删除）。
`common.js:73680-73683`：refresh 由 `scheduleRefreshSession` 最多重试 5 次指数退避，全失败由上层 `auth:statusChanged` 决定登出。

### 2.6 心跳 / keepalive
**没有找到**"token keepalive"接口（无 `/ping`、`/v2/heartbeat`、`/v2/user/keepalive` 之类用于续期登录态的心跳）。找到的心跳都是别的子系统，**不要混淆**：

| 心跳 | 间隔 | 位置 | 用途 |
|---|---|---|---|
| ide_lifecycle | `3600*1e3` = 1h（仅前台 2h 内活跃才发） | `index.js:4086-4128` | 遥测 DAU |
| 钉钉 WS | `1e4` = 10s ping / 20s 超时 | `server.js:35231-35233` | Claw 钉钉连接 |
| 企业微信 AI Bot WS | `heartbeatInterval: 3e4` | `server.js:41684` | Claw 企微连接 |
| Yuanbao Bot | `heartbeatIntervalS = 5`（可由服务端 `heartInterval` 改） | `server.js:50482/50949/50989` | Claw 元宝连接 |
| envd（E2B） | `KEEPALIVE_PING_INTERVAL_SEC = 50`，头 `Keepalive-Ping-Interval` | `e2b-filesystem.js:15466-15467` | 沙箱连接 |
| Turing 设备 token | 5min 软过期 | `index.js:18472` | 见 §3 |

**推断**：客户端不靠定时心跳维持登录态，登录态有效性完全由 `POST /v2/plugin/auth/token/refresh` 的周期性调用体现（~24h/次）。

---

## 3. Turing / 反爬签名（最高价值发现）

### 3.1 结论：**没有 `X-Turing-*` 头，也没有客户端计算签名的算法**

- 全仓 grep `X-Turing` / `x-turing` / `Turing-Token` / `x-wb-` / `X-WB-` → **0 命中**。唯一的 `X-Wb-` 是 `X-Wb-Cover-Cache`（`index.js:39071`，封面缓存，与反爬无关）。
- 全仓 grep `q16` / `q36` 字符串 → 0 命中。
- 命名误导：**类名叫 `turing_sdk` / `TuringShieldSDK.dll`，但 HTTP 头名是 `X-Device-Token`**。

### 3.2 精确头名（已验证）
`stdio-mcp-inspector.js:13785-13786`：
```js
var TURING_SHIELD_ID_HEADER = "X-Device-Token";
var TURING_SHIELD_ERROR_HEADER = "X-Device-Token-Error";
```
互斥：成功带 `X-Device-Token`，失败带 `X-Device-Token-Error`（`tar.js:716-720`、`server.js:109401-109405`）。
值清洗（`stdio-mcp-inspector.js:13787-13790`）：
```js
value.replace(/[^\x20-\x7E]/g, "").slice(0, 64).trim() || undefined   // 仅可打印 ASCII，截断 64 字符
```

### 3.3 签名在哪：原生静态库，**不在 JS 里**
`native/turing-sdk/index.cjs`（N-API bridge）暴露两个方法：
```js
configure(channelId, productName, productVersion, keychainStorageEnabled)  // mac 传第 4 参，win 只传 3 个
fetchDeviceToken(options)                                                   // options = { usingCachedMessage: true, includesOutdatedMessage: true, includesDeviceInfo: true, timeoutMs }
```
`TuringShieldSDK.dll` 内可见字符串（`strings` 提取，已验证）：
- `https://tdid.m.qq.com/tmf`
- `https://tdid.m.qq.com/event/report`
- `device_tL`、`device_ticket`、`channel`
- `compress device token V3 jce failed, msg[%s]`、`fetch device token V3 failed, msg[%s]`、`request token error, response code[%d]`
- 设备指纹 API（Windows）：`EnumDisplayDevicesW`、`SetupDiGetDeviceInterfaceDetailW`、`SetupDiGetDevicePropertyW` 等
- 变体标记：`build/Release/turing-sdk-variant.json` = `{"variant":"domestic"}`（海外版为 `"overseas"`）

**推断**：设备 ID / 签名由腾讯 T-Sec TuringShield SDK 在 native 层采集设备信息后向 `tdid.m.qq.com` 换取 device token，JS 侧只是透传一个不透明字符串。**任何纯 JS 再实现都无法复刻该 token**——第三方网关只能选择（a）转发真实客户端采集到的值，或（b）整体省略该头（客户端自身有"未配置"降级路径）。

### 3.4 参数与开关（已验证）
`index.js:18660-18678` `resolveConfiguration()`：
- `channelId`：`WORKBUDDY_TURING_CHANNEL_ID` 环境变量 优先，否则 `productConfig.config.turingSdk.channelId` = **109144**（必须为 1..2147483647 的正整数）
- `sdkVariant`：`productConfig.isOversea === true ? "overseas" : "domestic"`（必须与 `turing-sdk-variant.json` 一致，否则 `Turing SDK variant mismatch`）
- `productName` / `productVersion`：产品配置或 `electron.app.getName()/getVersion()`
- `keychainStorageEnabled`：`electron.app.isPackaged`
- `requestTimeoutMs`：`WORKBUDDY_TURING_TIMEOUT_MS` 或配置，默认 **15000**，上限 300000

`index.js:18655-18658` `isTuringSdkEnabled()`：
```js
const envOverride = readBoolean(process.env.WORKBUDDY_TURING_ENABLED);  // "1"/"true"/"0"/"false"
if (envOverride !== undefined) return envOverride;
return productConfig?.productFeatures?.[ProductFeature.TuringDeviceToken] !== false;  // 缺省即开启
```
关停开关：`WORKBUDDY_TURING_ENABLED=0` 或远端 `/v3/config` 下发 `productFeatures.TuringDeviceToken:false`（`common.js:2686-2709` 有详细注释：只有显式 `false` 才关闭）。

### 3.5 缓存与刷新节奏（已验证）
`index.js:18471-18475`：
```js
DEFAULT_REQUEST_TIMEOUT_MS$1 = 15e3;
DEFAULT_REFRESH_INTERVAL_MS  = 5 * 6e4;      // 5 分钟软过期
DEFAULT_RETRY_BASE_DELAY_MS  = 3e4;          // 30s
DEFAULT_RETRY_MAX_DELAY_MS   = 5 * 6e4;      // 60s
MAX_DURATION_MS              = 1440 * 6e4;   // 24h 上限
```
- `getDeviceTokenForRequest()`（`:18508-18525`）：缓存未过期（<5min）直接返回 token；软过期则**后台异步刷新**，本次请求仍返回旧 token，**绝不阻塞**（`refreshReason` = `"cache-miss"` | `"soft-expired"`）。
- 失败退避：`retryDelay = min(30000 * 2^(failures-1), 60000)`，`nextRetryAt` 之前不再尝试（`:18561-18567`）。
- 启动后 PostReady 阶段异步 warm-up 一次（`index.js:18773-18800`）。

### 3.6 设备 ID 派生的其它标识（已验证，非 Turing）
- `machineId`：`CLIENT_INFO_MACHINE_ID` 环境变量 或 `machineIdSync()`（读硬件 UUID：`osxUuid`/`winUuid`/`linuxUuid`，失败回退 `crypto.randomUUID()`），见 `client-info-env.js:94-103`；暴露为 RPC `app:getMachineId` / `workbuddy:machineId`，以及反馈接口头 `X-Machine-Id`（`host-power-events.js:605`）。
- `qimei36`：**仅遥测公参**，从环境变量 `CODEBUDDY_QIMEI36` 读取（`daemon-bootstrap.js:2013-2072`、`common.js:50228-50250`）；appKey 在 `product.json` 的 `config.qimei36AppKey`（darwin `0MAC070JLSCWV1CM`，win32 `0WIN070JLSH64IEE`）。**没有发现它被放进 HTTP 请求头**。
- 没有找到任何 `deviceId`/`guid` 自造标识参与 HTTP 签名。

### 3.7 唯一的"自算签名"（不涉及 Turing，已验证）
只有意图识别 `rewrite-embed` 接口有一个 SHA256 salt 签名（`server.js:117597-117700`）：
```
POST /agenttool/v1/intent/rewrite-embed
sign = sha256( concat(items[].input) + "kkkzajzffwarlinahjgu78lol" ).hex.toLowerCase()
header: sign: <hex>    // 注意头名就叫 "sign"，无前缀
```
以及 Yuanbao Bot 的 HMAC-SHA256 签名（`server.js:50713-50727`，`signPayload = nonce + timestamp + appKey + appSecret`，头 `X-Instance-Id: 16`）——这两者都**不是**主 API 的反爬签名。

---

## 4. API 调用的必需请求头（完整清单）

### 4.1 全局公共头（桌面 CellJS/axios 拦截器自动注入）
| 头 | 值 | 来源 |
|---|---|---|
| `Accept` | `application/json` | `stdio-mcp-inspector.js:14266`、`server.js:72322` |
| `Content-Type` | `application/json` | 同上 |
| `Authorization` | `Bearer <accessToken>` | 拦截器 `common.js:73605`、`73680` |
| `X-User-Id` | `account.uid` | `common.js:73606`；常量 `common.js:49333` |
| `X-Enterprise-Id` | `account.enterpriseId` | `common.js:73607`；`:49334` |
| `X-Tenant-Id` | `account.enterpriseId`（与上一行**同值**） | `common.js:49336`、`73273` |
| `X-Department-Info` | `account.departmentFullName` | `common.js:73608`；`:49335` |
| `X-Domain` | `auth.domain`（否则取 endpoint 的 authority） | `common.js:72900-72905`、`:49337` |
| `X-Product` | `deploymentType ?? "SaaS"`（如 `SaaS`/`Internal`/`IOA`/`Selfhosted`/`Cloudhosted`） | `common.js:26476`、`19048` |
| `X-Request-ID` | `crypto.randomUUID().replace(/-/g,"")`（32 位无横线十六进制） | `index.js:1460`；常量 `common.js:19047` |
| `User-Agent` | 见 §4.3 | `module-base.js:658-673` |

### 4.2 追踪头（tracing，naive 实现最容易漏）
`common.js:71076-71087`（`Span.requestHeaders` getter）：
```js
X-Trace-ID:        <traceId>
b3:                `${traceId}-${spanId}-${traceFlags}-${parentSpanId}`
X-B3-TraceId:      <traceId>
X-B3-ParentSpanId: <parentSpanId>
X-B3-SpanId:       <spanId>
X-B3-Sampled:      String(traceFlags)   // 采样时 "1"
```
CLI 侧同一头族（`codebuddy.js` @10320500 附近，`setTraceHeaders`）：
```js
traceparent: `00-${traceId}-${spanId}-${sampled?"01":"00"}`   // W3C
b3:          `${traceId}-${spanId}-${sampled?"1":"0"}${parentSpanId?"-"+parentSpanId:""}`
X-B3-TraceId / X-B3-ParentSpanId / X-B3-SpanId / X-B3-Sampled / X-Trace-ID
```
`codebuddy.js` @9156672 根 span 生成：`X-Trace-ID = traceId`，`X-B3-SpanId = randomBytes(8).toString("hex")`，`X-B3-Sampled = "1"`，`b3 = traceId-spanId-1`。
请求结束时这 8 个头会被**显式删除**再交给下游：`["X-Trace-ID","X-Request-ID","b3","X-B3-TraceId","X-B3-ParentSpanId","X-B3-SpanId","X-B3-Sampled","traceparent"]`（`codebuddy.js` @9160270 / @9164102）。

### 4.3 User-Agent（Desktop 覆盖逻辑）
`module-base.js:633-673` `WorkbuddyUserAgentHttpInterceptor`：
```js
parts.push(`${clientInfo.applicationName}/${clientInfo.productVersion || "unknown"}`);
parts.push(`${clientInfo.platform}/${clientInfo.platformVersion || "unknown"}`);
if (clientInfo.userAgentExtension) parts.push(clientInfo.userAgentExtension);
config.headers["User-Agent"] = parts.join(" ");
config.headers[SkipUserAgentMergeHeader] = "1";   // "X-Skip-User-Agent-Merge"（模块内部标记，公共拦截器会删）
```
- `applicationName` = 产品配置 `applicationName`（ASCII 安全，避免中文 `productName` 触发 `ERR_INVALID_CHAR`）
- `platform` = `product.applicationName || "WorkBuddy"`（`module-base.js:749` `WORKBUDDY_PLATFORM = "WorkBuddy"`）
- `userAgentExtension` = `CLI/<cli/package.json version>`（`module-base.js:839-849`；本机 `publishConfig.customPackage.version = "2.137.1"`）
- **推断**：实际形如 `WorkBuddy/<ver> WorkBuddy/<ver> CLI/2.137.1`
- 其它路径上的 UA 覆盖：`server.js:36484` / `39447` → `WorkBuddy/${version}`（无版本时退化为 `"WorkBuddy"`）

服务端白名单接口会显式重写整组身份头（`stdio-mcp-inspector.js:14215-14222`）：
```js
"User-Agent": `WorkBuddy/${WORKBUDDY_CLIENT_VERSION}`,
"X-IDE-Type": "WorkBuddy",
"X-IDE-Name": "WorkBuddy",
"X-IDE-Version": WORKBUDDY_CLIENT_VERSION,
"X-Product": "WorkBuddy"
```
（`WORKBUDDY_CLIENT_VERSION` = `process.env.WORKBUDDY_APP_VERSION ?? ""`，`stdio-mcp-inspector.js:13791`）

> ⚠️ **别把这段当成通用值。** 这是**单个窄接口**（`/v2/activity/workbuddy/banner`）
> 自己重写的整组身份头，是本仓库里唯一见过 `X-Product: "WorkBuddy"` 的地方。
> 模型/对话请求走的是全局 `ProductEndpointHttpInterceptor`，那里 `X-Product`
> 取的是**部署形态**（见上表 4.2 与 4.4）——`deploymentType ?? "SaaS"`。
> 两者混用会让 `X-Product` 报成产品名，本身就是个可识别差异。

### 4.4 CLI 模型请求专属头（`codebuddy.js`，`/chat/completions`）
常量定义 @16098166–@16098770（`X-*` 全表），组装 @10321800–@10324000：
| 头 | 值 | 依据 |
|---|---|---|
| `X-Conversation-ID` | `session.id` | @10321812 |
| `X-Conversation-Request-ID` | `session.conversationRequestId \|\| ""` | 同上 |
| `X-Conversation-Message-ID` | `session.messageId` | 同上 |
| `X-Request-ID` | `session.messageId`（= `generateUUUID().replace(/-/g,"")`，32 位 hex） | 同上 |
| `X-Root-Request-ID` | `meta.rootRequestId`（若为合法 W3C trace id） | `resolveRootRequestId` |
| `X-Parent-Conversation-ID` | 父会话 id（若有） | `resolveParentConversationId` |
| `X-Agent-Type` | `resolveAgentType(session)` | @10324000 |
| `X-Agent-Intent` | `session.meta["codebuddy.ai/mode"] ?? "craft"` | @10321812 |
| `X-Agent-Purpose` | `process.env.PERSONAL_AGENT_ROLE ? "person_agent" : session.agentPurpose` | 同上 |
| `X-IDE-Type` | `telemetryClientInfo.ideType \|\| clientInfo.ideType \|\| PRODUCT_TYPE` | @10322222 |
| `X-IDE-Name` | `... \|\| clientInfo.platform \|\| ""` | 同上 |
| `X-IDE-Version` | `... \|\| clientInfo.platformVersion \|\| "0.0.0"` | 同上 |
| `X-Product` | `deploymentType ?? "SaaS"`（ProductEndpointHttpInterceptor @16633442） | — |
| `X-Private-Data` | `enableModelOptimization.enabled !== false ? "false" : "true"`（`DisableYuanbaoChannel===true` 时强制 `"true"`） | @10307977 |
| `X-API-Key` + `Authorization: Bearer <key>` | 仅当 `CODEBUDDY_API_KEY` / settings env / model.apiKey 存在 | @10309739 |
| `X-Moderation-Type` | `["protected"]`（受保护云端资产 Expert/Skill 时） | `server.js:109136-109160` |
| `X-Expert-Id` / `X-Expert-Team-Task` | 专家 id / `"true"`（team 型） | `server.js:109465-109466` |

**重要细节**：模型请求在最后会 **`delete ey.authorization; delete ey["user-agent"]`**，然后才重新注入（@10323450 附近）——说明 `Authorization` 是被**刻意重新计算**的，重实现不能只靠透传。
另外 `X-Private-Data` 在 custom model 请求中会被删除。

外部注入通道（网关若要做等价物值得注意）：
- `CODEBUDDY_CUSTOM_HEADERS`（`CodeBuddy: Header` 逐行，支持 `\n` 字面量与真实换行）→ `parseCustomHeaders()` @7585703/@10307700
- `X-Skip-User-Agent-Merge` / `X-Skip-Auth-Interceptor` 两个拦截器旁路标记
- CLI 的 `internalModelRequestHeaders`（volatile plugin）→ `POST <localGateway>/api/v1/plugins/switch` 注入，仅对非 custom model 生效（`shouldApplyInternalModelRequestHeaders` = `!!model && !isCustomModelRequest`）@10323743

### 4.5 未授权接口的抑制头
`X-No-Authorization`、`X-No-User-Id`、`X-No-Enterprise-Id`、`X-No-Department-Info`（值均为字符串 `"true"`）——登录态获取/轮询阶段必须带，否则拦截器会塞入过期身份（`common.js:72704-72707`、`72777-72780`、`73130-73133`、`73165-73168`；常量 `common.js:49338-49341`）。

### 4.6 局域网 sidecar 网关的 Bearer
Desktop 主进程与其 sidecar CLI 之间的本地网关用**进程内随机 secret**鉴权（`gateway-secret.js:171-204`）：
- 注入 env：`CODEBUDDY_GATEWAY_AUTH=password`、`CODEBUDDY_GATEWAY_PASSWORD=<randomBytes(32).toString("base64url")>`、`CODEBUDDY_GATEWAY_DISABLE_API_DOCS=1`
- 请求头：`Authorization: Bearer <secret>`（`gatewaySecretHeaders()`）
- 与腾讯云端无关，第三方网关不需要复刻。

### 4.7 没有找到的东西（明确说明）
- ❌ 任何 `X-Turing-*` 请求头
- ❌ 任何 `q16` / `q36` / `qimei` HTTP 头（qimei36 只在遥测事件体里）
- ❌ 主 API 的 HMAC / RSA / nonce+timestamp 签名算法（只在 native DLL 内）
- ❌ `device_code` / `qr` / `scan` 形式的 OAuth Device Flow **主登录**（主登录是 "打开浏览器 + 轮询 `/auth/token?state=`"；`device_code`/`user_code`/`verification_uri` 出现在 **MCP connector OAuth** 与 **Claw 微信小程序**两条旁路，不是登录 WorkBuddy 本身）
- ❌ token keepalive 心跳接口

---

## 5. AuthToken / Account 字段（已验证）

`common.js:77520` 附近为 `jsonwebtoken` 的 sign schema，**不是** AuthToken 定义。AuthToken 的真实字段由使用点拼出：

```
AuthToken {
  accessToken:  string
  refreshToken: string
  expiresIn:    number      // 秒
  expiresAt:    number      // ms epoch（由 expiresIn*1000 推算）
  refreshExpiresIn:  number // 秒（可选）
  refreshExpiresAt:  number // ms epoch（可选）
  tokenType:    string      // "Bearer"；API Key 模式为 "ApiKey"
  scope:        string
  domain:       string      // 决定 X-Domain 与网络环境判定
  lastRefreshTime: number   // 本地写入，不来自服务端
}
Account {
  uid, uin, nickname, name, type ("personal"|...), lastLogin: boolean,
  enterpriseId, enterpriseName, enterpriseUserName,
  oneidAccountId, avatarUrl, departmentFullName, phoneNumber, idp,
  editionType ("experience"|...), pluginEnabled: boolean
}
```
来源：`common.js:72857-72877`、`:72911-72914`、`:72279`、`:77856-77864`（API Key 模式构造 `{accessToken, expiresIn, expiresAt, refreshToken:"", refreshExpiresIn, refreshExpiresAt, tokenType:"ApiKey", scope:""}`）、`tar.js:721-748`、`common.js:78063-78065`（`accounts.filter(item => item.pluginEnabled)`——**只有 `pluginEnabled` 为真的账号会被采纳**）。

`/accounts` 端点存在**个人号过滤**差异：`isWorkbuddyPlatform()` 为真时使用 `allAccounts`，否则用 `accounts`（`common.js:72845-72856`）。

---

## 6. 给第三方网关的可执行要点（推断，基于以上事实）

1. 端点前缀固定 `https://copilot.tencent.com/v2/plugin/...`，`platform=workbuddy`。
2. 登录：`POST /v2/plugin/auth/state?platform=workbuddy` → 拿 `authUrl`/`state` → 打开浏览器 → 每 **1000ms** `GET /v2/plugin/auth/token?state=<state>`，共 **300s**，遇 `code=11217` 继续轮询；成功后 `GET /v2/plugin/login/account?state=<state>`（遇 `12151` 继续），再 `GET /v2/plugin/accounts`。
3. 续期：`POST /v2/plugin/auth/token/refresh`，body `{}`，头 `X-Refresh-Token`、`X-Auth-Refresh-Source: plugin`、`X-Domain`；节奏 ~24h（带抖动），失败按 5s/10s/20s/40s/60s 重试（401/403 不重试）。
4. 每个业务请求必带：`Authorization: Bearer`、`X-User-Id`、`X-Enterprise-Id`、`X-Tenant-Id`（= enterpriseId）、`X-Department-Info`、`X-Domain`、`X-Product`、`X-Request-ID`（32 位 hex 无横线）、`User-Agent`（`WorkBuddy/<ver> ... CLI/<ver>`）、以及 `X-Trace-ID`+`X-B3-*`+`b3` 追踪头族。
5. 风控头只有一个 `X-Device-Token`（不透明字符串，来自 native TuringShield，向 `tdid.m.qq.com/tmf` 换取）。无法在纯 JS 复刻；省略时会走客户端自身认可的"未配置"降级路径（`X-Device-Token-Error` 也是合法形态）。
6. 时间格式：所有 epoch 均为 **毫秒**（`expiresAt`、`refreshExpiresAt`、`lastRefreshTime`），`expiresIn`/`refreshExpiresIn` 为**秒**。
