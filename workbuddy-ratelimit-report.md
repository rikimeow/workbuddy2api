# WorkBuddy Desktop Client — Rate Limiting / Throttling / Retry Reverse-Engineering Report

**Scope (read-only; nothing modified):**
- `E:\反代理\_wb_rev\main\` — Electron main-process JS, 169 files / ~25 MB, readable non-minified (`server.js` = 129,000 lines)
- `D:\WorkBuddy\resources\app.asar.unpacked\cli\dist\codebuddy.js` — 23,030,852 bytes / 3,324 lines (minified)
- `D:\WorkBuddy\resources\app.asar.unpacked\cli\dist\codebuddy-headless.js` — 19,796,851 bytes / 3,296 lines (minified)
- `D:\WorkBuddy\resources\app.asar.unpacked\cli\product.json` — 382,019 bytes

**Convention:** ✅ VERIFIED = quoted from the file at the cited line/offset. 🔵 INFERENCE = my reasoning, not literal code.
**Offsets** are character offsets into the minified bundles (`@NNNNNNN`); **line numbers** are given for the readable main-process tree.

**Structural note:** the two trees are disjoint. The **model-request path** (what a gateway fronts) lives in the **CLI bundles**; `main/` holds the Electron host, automations, connectors (QQ/Discord/WeChat), and resource upload.

---

## 1. RATE LIMIT / THROTTLE CONSTANTS AND LOGIC

### 1.1 Gateway inbound limiter — the headline numbers ✅

`codebuddy.js` module `51065`, literal at **@11233570**:

```js
let ev="1.0",eS=8321,eQ=60,eD=3}
```

```js
DEFAULT_GATEWAY_PORT:()=>eS, DEFAULT_RATE_LIMIT_PER_MINUTE:()=>eQ, DEFAULT_MAX_CONCURRENT:()=>eD
```

| Constant | Value |
|---|---|
| `GATEWAY_PROTOCOL_VERSION` | `"1.0"` |
| `DEFAULT_GATEWAY_PORT` | **8321** |
| `DEFAULT_RATE_LIMIT_PER_MINUTE` | **60** |
| `DEFAULT_MAX_CONCURRENT` | **3** |

**Algorithm** — `GatewayRateLimiter`, `@9413039` ✅ (token bucket keyed by `sender.id`, *not* IP, *not* global):

```js
check(eA){let el=this.getOrCreateBucket(eA);return(this.refillTokens(el),
 el.concurrent>=this.maxConcurrent)?(...'hit concurrent limit'...,!1):
 el.tokens<=0?(...'hit per-minute limit'...,!1):
 (el.tokens--,el.concurrent++,!0)}
refillTokens(eA){let el=Date.now(),ec=Math.floor((el-eA.lastRefill)/6e4)*this.maxPerMinute;
 ec>0&&(eA.tokens=Math.min(this.maxPerMinute,eA.tokens+ec),eA.lastRefill=el)}
```

Exact semantics ✅:
- **Concurrency is checked BEFORE tokens** — a concurrency-rejected request does **not** consume a token.
- Refill is **lumpy, on whole-minute boundaries**: `floor(elapsedMs/60000) * maxPerMinute`, capped at 60. Not a smooth token bucket.
- `release(key)` runs in a `.finally()` of the async run → `concurrent` decrements when the **whole agent run** completes, not when the HTTP response is sent.
- Bucket GC timer every `3e5` ms (5 min); evicts buckets idle > `6e5` ms (10 min) with `concurrent === 0`.
- **What resets it:** a whole minute elapsing since `lastRefill` (full window refill), or `release()` on the concurrent counter.

**429 response** (`@9431438` webhook path, `@9432629` `createRun` path) ✅:

```js
if(!this.rateLimiter.check(el.source.sender.id))
 return ...ec.response.statusCode=429,(0,Em.error)(Em.ApiErrorCode.RATE_LIMITED,"Rate limit exceeded");
```

✅ **No `Retry-After` header is set on either 429.** Success = `202 {runId, status:"accepted"}`.

**The `security.rateLimit` config object is never read back** ✅ — `.rateLimit` has only **2** matches in the whole bundle: an unrelated `ChatResponseMetadataBuilder.rateLimit()` setter (`@6306`) and an i18n string `"login.error.rateLimit"` (`@9443616`). `rateLimiter.configure(` → **0 matches**. 🔵 So the limiter always runs at the hardcoded 60/3; the config is written but dead.

Also ✅: `rate-limit` / `max-concurrent` CLI flags → **0 matches**; `CODEBUDDY_GATEWAY_RATE*` env → **0 matches**.

### 1.2 Gateway auth failure limiter ✅

`codebuddy.js` `@9408574`:

```js
this.hmacTimeWindow=3e5,this.minuteLimiter=new RateLimiter(2,6e4),this.hourLimiter=new RateLimiter(12,36e5)
```

`RateLimiter` class (`@9408031`): `constructor(maxTokens, refillIntervalMs)`, `canTry()` / `removeToken()` / `refill()` — same whole-interval refill shape.

```js
recordFailedAttempt(){return!(this.minuteLimiter.canTry()||this.hourLimiter.canTry())||
 (this.minuteLimiter.removeToken()||this.hourLimiter.removeToken(),!1)}
```

- **2 failed attempts / 60 s**, **12 failed attempts / 1 h**, HMAC window 300 s.
- On exhaustion (`@8718206`): HTTP **429** + JSON-RPC error code **`-32e3` (= -32000)**, message `"Too many failed authentication attempts"`; otherwise HTTP **401** + `-32001`.

### 1.3 Capacity queue (server-side admission) ✅

`conversations.js:1711`:

```js
var QUEUE_ERROR_CODES = { WAITING: 6020, FULL: 6021, USER_LIMIT: 6022 };
```

Polling cadence (`conversations.js:1731`) and retry-after normalisation (`:1726`):

```js
function getPollingIntervalMs(queuePosition) {
  if (queuePosition === void 0 || queuePosition <= 10) return 2e3;   // 2 s
  if (queuePosition <= 50) return 5e3;                               // 5 s
  return 1e4;                                                        // 10 s
}
function normalizeRetryAfterMs(value) { ... const milliseconds = raw >= 1e3 ? raw : raw * 1e3;
  return Math.max(milliseconds, 1e3); }
```

`queue timeoutMs = options.timeoutMs ?? 12e5` (**20 min**, `:1775`). ✅
🔵 Client behaves politely: it polls the queue at 2/5/10 s depending on position, clamped to a **minimum 1 s** wait, giving up after 20 min.

### 1.4 Embedding recall limiter ✅

`server.js:117162`:

```js
var RateLimiter = class { constructor(opts = {}) { ... this.maxConcurrent = opts.maxConcurrent ?? 1;
    this.minIntervalMs = opts.minIntervalMs ?? 500; } ... }
```

Instantiated at `server.js:117467` with `{ maxConcurrent: 1, minIntervalMs: 500 }`. ✅ Also `minIntervalMs: 2500` at `server.js:7701` for the memory-profile polling coordinator, with `baseBackoffMs: 1500, maxBackoffMs: 2e4, jitterMs: 500`.

### 1.5 Concurrency limits — full table

**CLI bundles — marketplace/plugin** ✅ (`codebuddy.js` @10871333, module `72731`; identical offsets pattern in headless):

```js
eL=3,eF=5,eU=2,eH=4,eG=16,ej=16;
```

| Constant | Value |
|---|---|
| `CONCURRENT_MARKETPLACE_INSTALL_LIMIT` | **3** |
| `CONCURRENT_PLUGIN_INSTALL_LIMIT` | **5** |
| `CONCURRENT_MARKETPLACE_UPDATE_LIMIT` | **2** |
| `CONCURRENT_MARKETPLACE_PLUGIN_LOAD_LIMIT` | **4** |
| `CONCURRENT_PLUGIN_METADATA_LOAD_LIMIT` | **16** |
| `CONCURRENT_PLUGIN_COMPONENT_LOAD_LIMIT` | **16** |
| `INSTALL_STATS_CACHE_TTL_MS` | `36e5` (1 h) |

**CLI — filesystem/skill** ✅ `@14026400`: `let eu=32,ed=32` → `DEFAULT_FS_CONCURRENCY=32`, `DEFAULT_PLUGIN_COMPONENT_CONCURRENCY=32`. Env overrides: `CODEBUDDY_MARKETPLACE_PLUGIN_LOAD_CONCURRENCY` (`@10507444`), `CODEBUDDY_SKILL_LOAD_CONCURRENCY` (`@10581782`).

**CLI — workflow agents** ✅ `@14441805` (`WorkflowConcurrencyConstants`):

```js
MAX_AGENT_CONCURRENCY=Math.floor(Math.min(16,Math.max(2,(()=>{try{return(0,eD.cpus)().length||1}catch{return 1}})()-2)))
MAX_BATCH_SIZE=50, MAX_AGENTS_PER_RUN=1e3, SCRIPT_SYNC_TIMEOUT_MS=3e4,
DEFAULT_STALL_MS=18e4, PROGRESS_DEBOUNCE_MS=16, MAX_INLINE_SCRIPT_BYTES=524288
```

- `MAX_AGENT_CONCURRENCY = floor(min(16, max(2, cpuCount − 2)))` → range **[2, 16]** ✅
- `Semaphore` (`@14442225`) is used **exclusively** by the workflow engine (`new Semaphore(MAX_AGENT_CONCURRENCY)` @14447749; `callParallel` clamps user `concurrency` the same way @14453405). It is **not** on the model-request or gateway path. ✅

**CLI — subagent spawn budget** ✅ module `97010` (`codebuddy.js` @7249355):

```js
let eu=5,ed=200,ep="CODEBUDDY_CODE_MAX_SUBAGENTS_PER_SESSION"
```

`MAX_SUBAGENT_DEPTH = 5`, `DEFAULT_MAX_SUBAGENTS_PER_SESSION = 200`.

**Main process** ✅:

| Mechanism | Limit | Citation |
|---|---|---|
| Automation scheduler concurrency | **3** (`options.concurrency ?? 3`), tick 30 s / active tick 5 s | `server.js:16177`, `:16174-16175` |
| Same-source-key serialisation | max **1** per owner+cwd | `server.js:16169` |
| Automation run supervisor | `maxConcurrentRuns ?? 3`, run timeout `54e5` (90 min) | `server.js:71040-71041` |
| Concurrent CWD runs per aggregate | `MAX_CONCURRENT_CWD_RUNS = 3` | `server.js:20129` |
| Transcript reads | `MAX_CONCURRENT_TRANSCRIPT_READS = 4` | `server.js:104752` |
| COS/tdrive upload workers | `uploadConcurrency: 5` default | `server.js:12317` |
| Prewarm pool (desktop) | `poolSize: 2` | `daemon-app-server-main.js:4124` |
| ACP agent dispatch queue | `maxConcurrent ?? 1` | `server.js:117167` |
| Transcript/ClawService queue concurrency | `options.concurrency ?? 3` | `server.js:16177` |
| OTLP exporter | `concurrencyLimit` default **30** | `common.js:51849`, `:51849` `?? 30` |

🔵 The desktop client caps **in-flight automation work at 3** in several independent places (scheduler, run supervisor, CWD runs) — consistent and clearly deliberate.

### 1.6 Peak-hour deferral (client-side load shedding) ✅

`server.js:17305-17329`:

```js
var PEAK_HOURS = [9, 10];
var PEAK_DISPATCH_WINDOW_MS = 600 * 1e3;     // 10 min
var NON_PEAK_DISPATCH_WINDOW_MS = 30 * 1e3;  // 30 s
function isPeakHour(timestampMs) { return PEAK_HOURS.includes(new Date(timestampMs).getHours()); }
function getDispatchWindowMs(occurrenceMs) {
  return isPeakHour(occurrenceMs) ? PEAK_DISPATCH_WINDOW_MS : NON_PEAK_DISPATCH_WINDOW_MS; }
```

🔵 Recurring automations are spread by a deterministic FNV-1a hash over `automationId + occurrenceMs` across a **10-minute** window during local hours **09:00–10:59**, and **30 s** otherwise. This is an explicit **thundering-herd mitigation** (comment cites issue #88480). A duplicate set exists at `server.js:14785-14823` — `PEAK_HOURS$1 = [9,10]`, same window values.

Deferral reason codes ✅: `"peak_queued"` (`server.js:20403`, `:21237`) and `"concurrency_limit"` (`server.js:21167`, `:21237`). i18n string `"concurrencyLimit": "已超过定时任务最大并发限制（最多 {{max}} 个），请稍后再试。"` (`server.js:20167`, invoked with `{ max: 3 }` at `:20903`).

### 1.7 Other throttle / interval / cooldown constants ✅

| Constant | Value | Citation |
|---|---|---|
| `ATTACH_FAILURE_COOLDOWN_MS` | **5,000 ms** (caches a failed attach, rejects within window) | `conversations.js:10851`, `:11037` |
| Session-list poll `intervalMs` / `maxBackoffMs` | **30,000** / **300,000** | `conversations.js:22045-22046` |
| Conversation-list backoff formula | `intervalMs * 2 ** min(consecutiveFailures, 10)`, capped at `maxBackoffMs` | `conversations.js:22099-22000` |
| Migration circuit breaker | open at `failureCount >= 3`; cooldown **864e5 ms (24 h)** | `index.js:13139-13145` |
| Enterprise-model API backoff | `MIN_FAILURE_BACKOFF_MS = 30,000`, `MAX = 300,000`, factor 2 | `module.app-server.js:14998-15000`, `:15146` |
| `RATE_LIMIT_RECONNECT_DELAY_MS` (QQ 4008) | **60,000 ms** | `server.js:38436` |
| Claw control startup retry | base `1,000` → max `15,000`, factor 2 | `server.js:2853-2854`, `:2997` |
| Prompt (automation) retry delay | base **2,000**, `* 2^(n-1)`, cap **15,000** | `server.js:70193`, `:70925` |
| Auth wait stable window | `PROMPT_AUTH_READY_STABLE_MS = 3,000` | `server.js:70197` |
| Terminal output throttle | `windowMs = 250`, `maxFrameBytes = 65536`, `maxTotalBytes = 2097152`; env `CODEBUDDY_ACP_TERMINAL_OUTPUT_WINDOW_MS` | `codebuddy.js` @8731684 |
| Defers / debounce (UI) | `DEFER_CAP_MS=10,000`, `DEFER_PRESS_DEBOUNCE_MS=1,000`, `FLUSH_TIMEOUT_MS=2,000` | `codebuddy.js` @6826647 |
| Workflow progress debounce | `PROGRESS_DEBOUNCE_MS = 16` | `codebuddy.js` @14441805 |
| Cron scheduling jitter (not request retry) | `{recurringFrac:.1, recurringCapMs:9e5, oneShotMaxMs:9e4, oneShotMinuteMod:30}` | `codebuddy.js` @8061657 |

---

## 2. ERROR CODES

### 2.1 The WorkBuddy business enum — `ServerErrorCode` ✅

`codebuddy.js` @14036500 (identical in headless), module `93202`:

```js
eA[eA.CraftRateLimit=6e3]="CraftRateLimit",
eA[eA.CraftRateTPSLimit=6001]="CraftRateTPSLimit",
eA[eA.CraftRateTPMLimit=6002]="CraftRateTPMLimit",
eA[eA.CraftRateTPHLimit=6003]="CraftRateTPHLimit",
eA[eA.CraftRateTPDLimit=6004]="CraftRateTPDLimit",
eA[eA.CraftRateRPSLimit=6005]="CraftRateRPSLimit",
eA[eA.CraftRateRPMLimit=6006]="CraftRateRPMLimit",
eA[eA.CraftRateRPHLimit=6007]="CraftRateRPHLimit",
eA[eA.CraftRateRPDLimit=6008]="CraftRateRPDLimit",
eA[eA.UsageLimitExceeded=14001]="UsageLimitExceeded",
eA[eA.ConversationChatTooMany=14002]="ConversationChatTooMany",
eA[eA.RateLimitError=14003]="RateLimitError",
eA[eA.UsageLimitExceededEnterprise=14012]="UsageLimitExceededEnterprise",
eA[eA.UsageLimitExceededTencent=14013]="UsageLimitExceededTencent",
eA[eA.UsageLimitEnterpriseExhausted=14014]="UsageLimitEnterpriseExhausted",
eA[eA.UsageLimitLicenseExpired=14015]="UsageLimitLicenseExpired",
eA[eA.UsageLimitEnterpriseNotActivated=14016]="UsageLimitEnterpriseNotActivated",
eA[eA.UsageLimitUserNotActivated=14017]="UsageLimitUserNotActivated",
eA[eA.UsageLimitUserExhausted=14018]="UsageLimitUserExhausted",
eA[eA.ConversationLimitExceeded=10105]="ConversationLimitExceeded",
eA[eA.WebSearchRateLimit=15001]="WebSearchRateLimit",
eA[eA.ContextTooLong=11115]="ContextTooLong"
```

**6004 is a real, load-bearing rate-limit code = `CraftRateTPDLimit` (tokens-per-day).** ✅

### 2.2 Rate-limit code sets and the retry decision ✅

`codebuddy.js` @14038200:

```js
let ey=eu.CraftRateLimit,            // 6000
    ew=eu.CraftRateRPDLimit,         // 6008
    e_=new Set([eu.CraftRateTPDLimit,eu.CraftRateRPDLimit]),   // {6004, 6008}
    eB=new Set([eu.RateLimitError]),                            // {14003}
    eI=new Set([eu.UsageLimitExceeded,eu.UsageLimitExceededEnterprise,eu.UsageLimitExceededTencent,
                eu.UsageLimitEnterpriseExhausted,eu.UsageLimitUserExhausted]),
    ev=new Set([...eB,...eI]);
```

Predicates (`@14046340`, `@14044299`, `@14045668`):

```js
isCraftDailyQuotaBusinessCode = code => e_.has(code)                       // {6004, 6008}
isTransientRateLimitBusinessCode = code => code>=6000 && code<=6008 ? !e_.has(code) : eB.has(code)
isQuotaExhaustedError   → code ∈ {14001,14012,14013,14014,14018}
isModelOverloadedError  → status===429 || code ∈ [6000,6008] || code ∈ ev
```

**Net behaviour (✅ derived mechanically from the above, 🔵 labelled):**
- **`6004` / `6008` are terminal — never retried** (they are the explicit carve-out in `isRequestLevelRetryableError`, §3.1).
- **`6000/6001/6002/6003/6005/6006/6007`, `14003`, `14001/14012/14013/14014/14018`, a bare HTTP 429, and all 5xx ARE retried.**

### 2.3 Per-code category table `classifyErrorDetail` ✅

`codebuddy.js` @8109993:

```js
new Map([[10105,{category:"quota",subcategory:"quota_active_session"}],
[15001,{category:"quota",subcategory:"quota_web_search"}],
[14003,{category:"quota",subcategory:"quota_request_limit"}],
[14001,{category:"quota",subcategory:"quota_balance_exhausted"}],
[14002,...],[14012,...],[14013,...],[14014,...],[14018,...],
[14015,{category:"auth",subcategory:"auth_expired"}],
[14016,{category:"quota",subcategory:"quota_not_activated"}],
[14017,{category:"quota",subcategory:"quota_not_activated"}],
[11140,{category:"auth",subcategory:"auth_forbidden"}],
[11141,{category:"model_service",subcategory:"model_behavior_error"}],
[11142,{category:"auth",subcategory:"auth_forbidden"}]])
// plus RANGES:
eh=[{min:6e3,max:6004,category:"quota",subcategory:"quota_token_limit"},
    {min:6005,max:6008,category:"quota",subcategory:"quota_request_limit"}]
```

🔵 A bare HTTP 429 with no code is classified `quota_balance_exhausted` — note the asymmetry with `14003 → quota_request_limit`.

### 2.4 Gateway API error codes (string enum) ✅

`codebuddy.js` module `7400` @11228472:

```js
let eu={AUTH_REQUIRED:"AUTH_REQUIRED",AUTH_INVALID:"AUTH_INVALID",AUTH_RATE_LIMITED:"AUTH_RATE_LIMITED",
FORBIDDEN:"FORBIDDEN",NOT_FOUND:"NOT_FOUND",BAD_REQUEST:"BAD_REQUEST",RATE_LIMITED:"RATE_LIMITED",
INTERNAL_ERROR:"INTERNAL_ERROR",PLUGIN_MANAGEMENT_DISABLED:"PLUGIN_MANAGEMENT_DISABLED",
PLUGIN_RECONCILE_TIMEOUT:"PLUGIN_RECONCILE_TIMEOUT",PLUGIN_RECONCILE_FAILED:"PLUGIN_RECONCILE_FAILED",
SESSION_NOT_FOUND:"SESSION_NOT_FOUND",SESSION_DELETE_CURRENT:"SESSION_DELETE_CURRENT",
INSTANCE_NOT_FOUND:"INSTANCE_NOT_FOUND",TERMINAL_NOT_FOUND:"TERMINAL_NOT_FOUND",RUN_NOT_FOUND:"RUN_NOT_FOUND",
RUN_ALREADY_COMPLETED:"RUN_ALREADY_COMPLETED",PLATFORM_UNSUPPORTED:"PLATFORM_UNSUPPORTED",
SIGNATURE_INVALID:"SIGNATURE_INVALID",PATH_REQUIRED:"PATH_REQUIRED",PATH_NOT_ABSOLUTE:"PATH_NOT_ABSOLUTE",
PATH_NOT_DIRECTORY:"PATH_NOT_DIRECTORY",FILE_TYPE_FORBIDDEN:"FILE_TYPE_FORBIDDEN",
CONNECTION_LIMIT:"CONNECTION_LIMIT",WORKER_NOT_FOUND:"WORKER_NOT_FOUND",WORKER_NO_LOGS:"WORKER_NO_LOGS",
DAEMON_NOT_RUNNING:"DAEMON_NOT_RUNNING",DAEMON_ALREADY_RUNNING:"DAEMON_ALREADY_RUNNING"};
```

Body shape: `{error:{code,message,details?}}`. Gateway 429 body ⇒ `{"error":{"code":"RATE_LIMITED","message":"Rate limit exceeded"}}` 🔵.

### 2.5 JSON-RPC / ACP / app codes ✅

- `RequestError` factories (`codebuddy.js` @6533684, @6574840): `-32700` ParseError, `-32600` InvalidRequest, `-32601` MethodNotFound, `-32602` InvalidParams, **`-32603` InternalError**, `-32800`, `-32e3` (= -32000) authRequired, `-32002` resourceNotFound, `-32042` UrlElicitationRequired.
- **App-level codes** (`@6420498`): `{NetworkError:-32001, InternalError:-32002, QuotaExceeded:-32003, ModelServiceError:-32004}` and `tS=-32010`.
- `-32603` has **no rate-limit role** — it is plain JSON-RPC internal error (7 matches: 2 Zod unions, 2 `RequestError.internalError`, MCP enum tables). ✅
- `main/auth.js:130-140` mirrors this: `ConnectionClosed=-32e3, RequestTimeout=-32001, ParseError=-32700, InvalidRequest=-32600, MethodNotFound=-32601, InvalidParams=-32602, InternalError=-32603, UrlElicitationRequired=-32042`. `main/protocol.js:182-186` has the daemon subset. ✅

### 2.6 Main-process numeric tables ✅

**`BIZ_CODE_MAP`** (`server.js:128`):
```js
410001:{code:"INVALID_ARGUMENT",reason:"invalid"}, 410003:{code:"NOT_FOUND",reason:"not_found"},
410004:{code:"CONFLICT",reason:"expired"},          410005:{code:"CONFLICT",reason:"disabled"},
410006:{code:"RATE_LIMITED",reason:"rate_limited"}, 410007:{code:"INVALID_ARGUMENT",reason:"too_large"},
410008:{code:"NOT_AUTHENTICATED",reason:"unauthorized"}
```
Companion `HTTP_STATUS_REASON` (`:159`): `400:"invalid", 401:"unauthorized", 404:"not_found", 410:"expired", 413:"too_large", 429:"rate_limited"`. Handler `toInspirationCodeError` `:171-186`.

**`RPC_ERROR_CODES`** (`host-power-events.js:1181`): `NO_HANDLER:4001, HANDLER_ERROR:4002, AUTH_FAILED:4003, PARSE_ERROR:4004`.

**`DISCORD_CLOSE_CODE`** (`server.js:36060`): `4000 UNKNOWN_ERROR, 4001 UNKNOWN_OPCODE, 4002 DECODE_ERROR, 4003 NOT_AUTHENTICATED, 4004 AUTHENTICATION_FAILED, 4005 ALREADY_AUTHENTICATED, 4007 INVALID_SEQ, 4008 RATE_LIMITED, 4009 SESSION_TIMED_OUT, 4010..4014`.

**`QQ_CLOSE_CODE`** (`server.js:38438`): `4004 INVALID_TOKEN, 4006 SESSION_TIMEOUT, 4007 SEQ_INVALID, 4008 RATE_LIMITED, 4009 SESSION_INVALID, 4900-4913 SERVER_ERROR_RANGE, 4914 BOT_OFFLINE, 4915 BOT_BANNED`. Handler `:38756-38780`: `4914|4915` → no reconnect; `4004` → `clearSession()`; **`4008` → 60,000 ms delay**; `shouldClearSessionForCloseCode` = `4006||4007||4009||(4900..4913)`.

**Auth/IP/licence** (`common.js`): `RESPONSE_CODE_IP_LIMIT = 10081` (`:72477`), `ServerErrorCode$1` (`:72478`) = `RetryFetchToken:11217, RetryFetchAccount:12151, LicenseSeatLimit:12005, LicenseExpired:11212, TrialExpired:11216`; `LICENSE_ERROR_CODES = new Set([12005,11212,11216])`; `MAX_ACCOUNT_AUTH_RETRIES = 5` with backoff `min(5e3 * 2^(n-1), 6e4)`. `AuthErrorCode` strings (`:49362`) incl. `IpLimit:"AUTH_IP_LIMIT"`. `AUTH_INVALID_CGICODE = 12100` (`server.js:10005`). `INNER_CODE_SUBJECT_NOT_FOUND = 10040` (`auth-liveness.js:13`).

**Other**: `TODO_ERROR_CODES={notFound:17301}` (`server.js:92600`); `QUOTA_ERROR_CODES` (`:90592`) = `member:17234, memberJoinRequest:17273, project:17260, exclusive:17261`; `CLOUD_SERVICE_BACKEND_CODE` (`:126111`) = `POOL_EXHAUSTED:18371, QUOTA_EXCEEDED:18305, MODULE_UNREADY:18309, TCB_FAILED:18370, APP_EMPTY:18300, APP_NOT_FOUND:18301, APP_NOT_OWNED:18302, BAD_REQUEST:10001`; `ImaErrorCode` string set + `fallbackErrorCodeFromStatus` (`celljs-deps.js:29`, `:114`) mapping `429→RATE_LIMITED`, with `retriable()` = `NETWORK|RATE_LIMITED|UPSTREAM_ERROR||5xx`; `wb-error.js:4255` `HTTP_STATUS_MAP = {401:"NOT_AUTHENTICATED",403:"PERMISSION_DENIED",404:"NOT_FOUND",409:"CONFLICT",429:"RATE_LIMITED"}`.

**Credit-purchase UI table** (`stdio-mcp-inspector.js:6630`), keyed by error code string: `"14018"` (获取 Credits), `"6004"`, `"6005"` (升级专业版) → `https://www.codebuddy.cn/profile/plan`. ✅ Cross-ref `common.js:2654`: "* 单用户限频（6004）后的付费复制线路配置 … `allowPaidSwitch: true`", feature `ModelRateLimitCap`, issue #96599. 🔵 Confirms 6004 = per-user rate limit / paid-tier gate.

### 2.7 Codes explicitly NOT found ✅

| Pattern | Result |
|---|---|
| `\b11102\b` | **0 matches** (only inside a bare numeric enumeration list) |
| `\b11140\b` in `main/` | **0 matches** — but **exists** in the CLI bundle: `tQ=new Set([11140,11141,11142])` @6420733 and category table §2.3 |
| `\b14017\b` in `main/` | **0 matches** — exists in CLI: `UsageLimitUserNotActivated=14017` (§2.1) |
| `\b12153\b` | **NOT an error code** — 3 hits, all Unicode CJK range tables |
| `\b10000\b` | **NOT an error code** — all byte/char-size constants |
| `\b41000\b`, `\b4101\b`, `\b50001\b` | **0 matches** |
| `ApiErrorCode` in `main/` | **0 matches** (exists in CLI, §2.4) |
| `BizCodeMap` / `STATUS_CODE_MAP` / `ERROR_CODE_MAP` | **0 matches** |
| `-32000` literal in `codebuddy.js` | **0 matches** (written as `-32e3`) |

🔵 Note `14018` **does** exist while `14017` does not appear in `main/` — a digit transposition in the original target list is likely.

---

## 3. RETRY POLICY

### 3.1 Model request retry — the primary gateway-facing policy ✅

**Constants** — `codebuddy.js` @10309638, module `46778`:

```js
let A3=(0,tX.promisify)(t0.gunzip),A4="codebuddy_code.model_request",A5=12e5,A8=12e5;
let A6=500,A9=32e3,A7=8,rn=15,ra=3e5,rl=5e3;
```

Export map @10182240:
```js
DEFAULT_REQUEST_MAX_RETRIES:()=>A7, DEFAULT_STREAM_IDLE_TIMEOUT_MS:()=>A8,
REQUEST_RETRY_BASE_DELAY_MS:()=>A6, DRAIN_BODY_TIMEOUT_MS:()=>rl,
DEFAULT_FIRST_TOKEN_TIMEOUT_MS:()=>A5, REQUEST_RETRY_MAX_DELAY_MS:()=>A9,
WATCHDOG_MAX_BACKOFF_MS:()=>ra, MAX_REQUEST_MAX_RETRIES:()=>rn
```

| Constant | Literal | Value |
|---|---|---|
| `REQUEST_RETRY_BASE_DELAY_MS` | `A6` | **500 ms** |
| `REQUEST_RETRY_MAX_DELAY_MS` | `A9` | **32,000 ms** |
| `DEFAULT_REQUEST_MAX_RETRIES` | `A7` | **8** |
| `MAX_REQUEST_MAX_RETRIES` | `rn` | **15** |
| `WATCHDOG_MAX_BACKOFF_MS` | `ra` | **300,000 ms** |
| `DRAIN_BODY_TIMEOUT_MS` | `rl` | **5,000 ms** |
| `DEFAULT_FIRST_TOKEN_TIMEOUT_MS` | `A5` | **1,200,000 ms** |
| `DEFAULT_STREAM_IDLE_TIMEOUT_MS` | `A8` | **1,200,000 ms** |

**Env override** ✅ `@10309638`:
```js
function resolveRequestMaxRetries(eA){if(void 0!==eA&&""!==eA){let el=parseInt(eA,10);
  if(!isNaN(el)&&el>=0)return Math.min(el,rn)}return A7}
function isRetryWatchdogEnabled(eA){...return"1"===el||"true"===el||"yes"===el}
```
Read at the call site as `process.env.CODEBUDDY_MAX_RETRIES` (clamped to `[0,15]`, default 8) and `process.env.CODEBUDDY_RETRY_WATCHDOG` (`"1"|"true"|"yes"`). ✅

**Backoff formula** ✅:

```js
function computeRequestRetryDelayMs(eA,el,ec=A9,eu=Math.random){
  return null!=el&&el>0 ? Math.min(el,ec)
       : Math.min(A6*Math.pow(2,Math.max(0,eA-1)),ec)*(1-.25*eu()) }
```

- If a server `Retry-After` hint is present and > 0 → **`min(retryAfter, cap)` verbatim, no jitter**.
- Otherwise → `min(500 · 2^(attempt-1), cap)` × `(1 − 0.25·random)` — **jitter is downward-only, in [0.75, 1.0]**.
- **Nominal schedule at the 32 s cap:** `500, 1000, 2000, 4000, 8000, 16000, 32000, 32000` ms → **~95.5 s cumulative** across 8 retries. ✅

**The retry loop** ✅ `requestWithConnectionRetry` @10320158:

```js
async requestWithConnectionRetry(eA,el,ec,eu,ed){let ep,eg=1,
  eh=resolveRequestMaxRetries(process.env.CODEBUDDY_MAX_RETRIES),
  em=isRetryWatchdogEnabled(process.env.CODEBUDDY_RETRY_WATCHDOG),ef=0,eE=0;for(;;)try{...
```
```js
if(this.isConnectionLevelRetryableError(ey)){if(ef>=eg)break;ef+=1;   // eg=1 → exactly one retry, no sleep
  ... 'Connection-level failure before response headers, retrying once' ...;continue}
if(await this.drainErrorResponseBody(ey,eC),this.isRequestLevelRetryableError(ey,eu)){
  if(!em&&eE>=eh)break;                    // watchdog ON ⇒ UNBOUNDED attempts
  eE+=1;
  let eg=(0,AZ.parseRetryAfterMs)(ep),
      ef=em?ra:A9,                          // 300000 w/ watchdog else 32000
      ew=em?(0,AZ.parseRateLimitResetMs)(ep):null,
      e_=null!=ew?Math.min(ew,ef):computeRequestRetryDelayMs(eE,eg,ef);
  ...await this.abortableSleep(e_,eC);continue}
```

✅ Key facts:
- **Connection-level failures (no `.response`) are retried exactly ONCE with zero sleep.**
- **Request-level: 8 attempts** by default; with `CODEBUDDY_RETRY_WATCHDOG` truthy the attempt cap is **disabled entirely**, the cap widens to **300 s**, and `parseRateLimitResetMs` is preferred over `Retry-After`.
- **The error body is drained and parsed BEFORE the retryable check** — business codes inside the JSON body gate retry.
- `retry_count` set as `ef + eE` on the OTel span; a `retry_attempt` event records `{attempt, kind, status, delay_ms}`.

**What is retryable** ✅:

```js
function isRequestLevelRetryableStatus(eA){return"number"==typeof eA&&!!Number.isFinite(eA)&&
  (408===eA||409===eA||429===eA||eA>=500&&eA<=599)}     // @14056598
```
→ **408, 409, 429, and all 5xx.** Note **409 Conflict is retryable**, which is unusual. 🔵

```js
isConnectionLevelRetryableError(eA){if(!eA||void 0!==eA.response)return!1; /* guard: pre-response only */
  ... new Set(["ECONNRESET","ECONNREFUSED","ETIMEDOUT","EAI_AGAIN","EPIPE","UND_ERR_SOCKET",
               "ERR_STREAM_PREMATURE_CLOSE","EPROTO"]).has(code)
   || message.includes("socket hang up") || isTlsRecordCorruptionMessage(message) }  // @10318346 region
```

```js
isRequestLevelRetryableError(eA,el){...
  if(matchesOwnModelCode(isQuotaExhaustedError)||!el&&matchesOwnModelCode(isCraftDailyQuotaBusinessCode))
    return !1;                                             // ← HARD ANTI-RETRY CARVE-OUT
  let eu=eA.response?.status;
  return !!(isRequestLevelRetryableStatus(status)||!el&&matchesOwnModelCode(isTransientRateLimitBusinessCode)) }
```
🔵 **`6004`/`6008` (and the quota-exhausted set) suppress retry even on 429/5xx.** A bare 429 **is** retried 8×.

**`Retry-After` / reset-header parsing** ✅ @14056940, @14057143:

```js
function parseRetryAfterMs(eA){let el=readHeaderValue(eA,"retry-after");if(null==el)return null;
  let ec="number"==typeof el?el:parseInt(String(el).trim(),10);
  return!Number.isFinite(ec)||ec<=0?null:1e3*ec}
let eC=["anthropic-ratelimit-unified-reset","x-ratelimit-reset"];
```

- **Only integer delta-seconds is supported.** `parseInt("Wed, 21 Oct 2015 07:28:00 GMT")` → NaN → `null` ⇒ **date-form `Retry-After` is silently discarded on the model path.** 🔵
- `x-ratelimit-remaining` / `ratelimit-limit` → **0 matches**; the only `x-ratelimit*` strings in the bundle are the two reset headers above.
- `parseRateLimitResetMs` accepts epoch-seconds digits or a `Date.parse`-able string, returns ms remaining, accepted only if `> 0`, and is **used only on the watchdog path**. ✅

**Overload / throttle message regexes** ✅ @14052630:

```js
matchesModelOverloadMessage: /overload(?:ed)?/, /rate limit(?:ed| exceeded)?/, /too many requests/,
  /quota (?:exhausted|exceeded)/, /(?:no available|insufficient) credits?/, /credits? exhausted/,
  /capacity exceeded/, /(?:额度已?用尽|额度不足|请求限流)/
matchesQuotaExhaustedMessage: /exceeded your current quota/, /insufficient_quota/,
  /credit balance is too low/, /billing_hard_limit_reached/
```
🔵 The Chinese literals are stored as UTF-8 bytes re-decoded as CP936 (mojibake `棰濆害宸?鐢ㄥ敖`); one byte at ~@11226997 stays ambiguous after round-trip — flagged, not asserted.

**Broad substring classifier** ✅ @14052630 region — plain `.includes()` on `"overloaded"`, `"overload"`, `"rate limit"`, `"too many requests"`, `"quota"`, `"credit"`, `"capacity"`, `"额度"`, `"用尽"`, `"限流"` reclassifies the failure into a quota/rate-limit category. 🔵 **Very easy to trip incidentally** — avoid those substrings in unrelated error text.

### 3.2 `SubagentThrottleRetry` — the one explicit throttle-aware retry ✅

`codebuddy.js` @14485641:

```js
let e6=45e3,e9=/(rate.?limit|throttl|429|too\s+many\s+requests)/i;
run(eA,el){let ec=await this.tryOnce(eA,el);
  if(!this.looksThrottled(ec.result,ec.error)){if(ec.error)throw ec.error;return ec.result}
  if(await this.sleep(e6,el),el.aborted)throw new WorkflowAbortedError("aborted-during-throttle-backoff");
  let eu=await this.tryOnce(eA,el);...return{...eu.result,throttleRetried:!0}}
looksThrottled(eA,el){...if(!(e9.test(text)||e9.test(message)))return!1;
  if(eA){let tokens=eA.tokens??0,durationMs=eA.durationMs??0;
    return null==eA.stopReason&&void 0===eA.structured&&tokens<50&&durationMs>.5*this.stallMs}
  return!0}
```

✅ **Fixed 45,000 ms sleep, exactly ONE retry.** Detection: regex against subagent output text or error message. A *successful* result counts as throttled only when `stopReason == null` **and** `structured === undefined` **and** `tokens < 50` **and** `durationMs > 0.5 × stallMs` (> 90 s at the 180 s default). An *error* matching the regex is unconditionally treated as throttled.

### 3.3 Main-process retry policies ✅

| Mechanism | Count | Delay | Citation |
|---|---|---|---|
| Resource upload (`presign`/`PUT`/`complete`) | `DEFAULT_MAX_RETRIES = 3`; retriable kinds `presign-5xx`, `PUT >=500 or ===429` | `min(8e3, 500 * 2^(n-1)) + floor(random()*200)` **(jitter UP to 200 ms)** | `server.js:72554`, `:72573`, `:72559-72560`, `:72861`; `classifyHttp` `:72917` → `status>=500 || status===429` |
| Automation prompt (transient) | `PROMPT_MAX_ATTEMPTS = 3` | base **2,000** × 2^(n-1), cap **15,000** | `server.js:70192`, `:70925`, `:70866` |
| Automation prompt (auth reset) | `PROMPT_AUTH_RESET_MAX_RECOVERIES = 3` | wait **90,000** per recovery, total budget **180,000**, stable window **3,000** | `server.js:70194-70197` |
| Automation delivery outbox | `maxAttempts: 5` | claim TTL `now + 6e4` | `server.js:23338`, `:24391`, `:24398` |
| WeChat KF bind polling | `ceil(300*1e3 / 1e4)` = 30 attempts | 10,000 ms interval, 5 min budget | `server.js:54092-54095` |
| Account auth | `MAX_ACCOUNT_AUTH_RETRIES = 5` | `min(5e3 * 2^(n-1), 6e4)` | `common.js:72475`, `:72291` |
| Smartsheet data-init | `DATA_INITING_MAX_RETRY = 30` | fixed `DATA_INITING_WAIT_MS = 2,000` | `server.js:93830`, `:93836`, `:94172` |
| Enterprise model API | consecutive failures | `min(30e3 * 2^(n-1), 300e3)` | `module.app-server.js:15146` |
| Daemon crash recovery | give-up at 4 crashes / 60 s window, or 3 consecutive startup failures | `min(500 * 2^(attempt-1), 8,000)` | `index.js:16350-16355`, `:16331` |
| Memory-profile polling | — | `minIntervalMs 2,500`, backoff `1,500 → 20,000`, jitter `500` | `server.js:7700-7705` |
| Conversation-list poll | — | `intervalMs 30,000`, `maxBackoffMs 300,000` | `conversations.js:22045-22046` |
| Binary download | `maxAttempts: 3` | `initialDelay 1,000`, `maxDelay 30,000`, factor 2 | `client-info-env.js:2188-2192` |
| QQ gateway reconnect | `max < 50` attempts | `min(1e3 * 2^n, 6e4) + random()*1e3`; **4008 → flat 60,000** | `server.js:50437`, `:39347`, `:38436` |
| Galileo exporter | `maxRetries = 3` | `retryDelay 1,000`, `backoffMultiplier 2`, `maxRetryDelay 30,000` | `desktop-monitor-service.js:898-901`, `:1075` |
| Session-store upload | `backoffMs = 1e3` | `*2` to `MAX_BACKOFF_MS` | `node.js:248`, `:440` |
| fs cleanup | `maxRetries: 3` | `retryDelay: 500` | `index.js:32654-32655` |
| Automation missed-run circuit | break after `AUTOMATION_RECOVERY_INTERRUPT_CIRCUIT_MAX = 5` | — | `server.js:20138` |

### 3.4 Other retry policies in the CLI bundles ✅

| Mechanism | Value | Citation |
|---|---|---|
| OpenAI SDK client default | `maxRetries ?? 2` | `codebuddy.js` @~10180000 |
| **OpenAI SDK per model request** | **`maxRetries: 0`** — WorkBuddy disables SDK retry and uses its own loop | `codebuddy.js` @~10180000 region |
| OpenAI SDK backoff (unused for model calls) | `min(.5*2^(n),8)*(1-.25*random())*1e3` | same |
| Structured output retry policy | `MAX_RETRIES = 2` | `codebuddy.js` @11727629 region |
| Structured-output context `DEFAULT_MAX_RETRIES` | **4** | `codebuddy.js` @9181318 |
| Textual tool-call recovery | `MAX_RETRIES = 1`, `RETRY_CHECK_WINDOW = 15` | `codebuddy.js` @11747750 |
| Model-error retry | `MODEL_ERROR_MAX_RETRIES = 2`, `MODEL_ERROR_RETRY_CHECK_WINDOW = 4` | `codebuddy.js` @11727629 |
| Quick tunnel | `QUICK_TUNNEL_MAX_RETRIES = 3`, `RETRY_BASE_DELAY_MS = 1e3` | `codebuddy.js` @9418886 |
| Async reply | `ASYNC_REPLY_MAX_RETRIES = 3` | `codebuddy.js` @11149382 |
| At-rest encryption registry | `min(max, base * 2^min(10, n-1))` | `codebuddy.js` @~3160 |
| Vendored undici `RetryHandler` | `maxRetries 5, minTimeout 500, maxTimeout 30000, factor 2, statusCodes [500,502,503,504,429]`; **supports date-form `retry-after` here** | `proxy-agents.js:7281-7375`, `codebuddy.js` @~11884090 |
| OTel exporter retryable | `[429,502,503,504]` | `codebuddy.js` @~632904 |
| Anthropic SDK (if used) | `retry-after` honoured, `maxRetries`/`retryCount` present | `common.js:51797`, `:52013` |

🔵 The vendored undici retry path **does** support HTTP-date `Retry-After` (`calculateRetryAfterHeader` → `new Date(retryAfter).getTime()`), but that is the low-level proxy transport, not the model-request retry decision described in §3.1.

---

## 4. CONCURRENCY LIMITS — CONSOLIDATED TABLE

| Scope | Limit | Source |
|---|---|---|
| Gateway inbound, per `sender.id` | **3 concurrent, 60/min** | CLI @11233570 |
| Gateway auth failures | **2/min, 12/h** | CLI @9408574 |
| Workflow agents | **`floor(min(16,max(2,cpu-2)))`** | CLI @14441805 |
| Workflow batch items | 50 | CLI @14441805 |
| Workflow agents per run | 1,000 | CLI @14441805 |
| Subagent nesting depth | 5 | CLI module 97010 |
| Subagents per session | 200 (env `CODEBUDDY_CODE_MAX_SUBAGENTS_PER_SESSION`) | CLI module 97010 |
| Marketplace install / plugin install / marketplace update | 3 / 5 / 2 | CLI @10871333 |
| Marketplace plugin load / metadata load / component load | 4 / 16 / 16 | CLI @10871333 |
| FS + skill load | 32 | CLI @14026400 |
| Automation scheduler | 3 | `server.js:16177` |
| Automation run supervisor | 3 | `server.js:71040` |
| Concurrent CWD runs | 3 | `server.js:20129` |
| Same-source-key automations | 1 | `server.js:16169` |
| Transcript reads | 4 | `server.js:104752` |
| COS upload workers | 5 | `server.js:12317` |
| Desktop prewarm pool | 2 | `daemon-app-server-main.js:4124` |
| Embedding recall | 1 concurrent, 500 ms min interval | `server.js:117467` |
| OTLP export | 30 | `common.js:51849` |
| ACP agent dispatch | 1 | `server.js:117167` |

---

## 5. IMPLICATIONS FOR TUNING A PRODUCTION GATEWAY 🔵 (inference, not code)

1. **The 60/min + 3-concurrent limiter is INBOUND-ONLY**, keyed per `sender.id`, and governs the local gateway's webhook/run API — it is **not** applied to the client's outbound model traffic. **No client-side outbound request-rate governor exists** in either bundle (search: `minInterval`/`requestsPerMinute`/`requestsPerSecond`/`queueLimit` → 0 matches; `p-limit` → 0 matches). Enforce client-side budgets server-side.
2. **Emit `Retry-After` as INTEGER SECONDS.** Date-form is silently discarded on the model path; the client then falls back to 500 ms → 32 s exponential backoff.
3. **Soft-shed:** HTTP 429 + numeric `Retry-After`. The client will retry up to **8 times (~95.5 s nominal, ×0.75 with jitter)**.
4. **Hard-stop:** HTTP 429 (or 5xx) **with a body `code` of `6004` or `6008`**, or any code in `{14001,14012,14013,14014,14018}`. These are explicitly excluded from retry.
5. **`CODEBUDDY_RETRY_WATCHDOG=1` makes retry UNBOUNDED** with a 300 s cap and makes the client honour `anthropic-ratelimit-unified-reset` / `x-ratelimit-reset`. Do not set this in a constrained deployment.
6. **Do not put `quota`, `credit`, `capacity`, `rate limit`, `overload`, `额度`, `用尽`, `限流` in unrelated error text** — a broad plain-substring classifier reclassifies the failure as a quota/rate-limit error.
7. **409 is retryable.** Avoid returning 409 for genuinely terminal conflicts.
8. Serve **60xx capacity-queue codes (6020/6021/6022)** for admission control if you want the client to poll politely: it will poll at 2 s (position ≤ 10), 5 s (≤ 50), 10 s (> 50), minimum 1 s, giving up after 20 min.
9. **Deploy the gateway on port 8321** to match `DEFAULT_GATEWAY_PORT`, or pass an explicit port.

---

## 6. EXPLICIT NON-FINDINGS ✅

| Pattern | Result |
|---|---|
| `p-limit` (library) | **0 matches** in both trees |
| `pLimit` | 27 hits in `codebuddy.js`, **all false positives** (`heapLimit`, `IpLimit`, `hitLoopLimit`, `Math.max`) — individually inspected |
| `DEFAULT_RATE_LIMIT_PER_MINUTE=`, `DEFAULT_MAX_CONCURRENT=`, `DEFAULT_REQUEST_MAX_RETRIES=`, `REQUEST_RETRY_BASE_DELAY_MS=` | **0 matches** — values are bare-identifier literals (`eQ=60`, `eD=3`, `A7=8`, `A6=500`) |
| `rateLimiter.configure(` | **0 matches** — `GatewayRateLimiter.configure()` is dead code |
| `rate-limit` / `max-concurrent` CLI flags | **0 matches** |
| `CODEBUDDY_GATEWAY_RATE*` env | **0 matches** |
| `x-ratelimit-remaining`, `ratelimit-limit` | **0 matches** |
| `tooManyRequests` | **0 matches** (constant is `RATE_LIMITED`) |
| `RATE_LIMIT_EXCEEDED` | **0 matches** |
| `exponentialBackoff`, `backoffFactor` (in CLI bundle) | **0 matches** (main tree has `backoffFactor: 2` in `client-info-env.js:2192`) |
| `throttleMs` | **0 matches** |
| `windowMs` (initializer form) | **0 matches** (only the terminal-output throttle uses `windowMs`) |
| `product.json` | contains **no** `rateLimit` / `maxPerMinute` / `maxConcurrent` / `429` config; the 12 `retry` matches are all English prompt text |
| Rate limiter on outbound model requests | **none found** |

---

**Verification notes:** every headline number (60, 3, 8321, 500, 32000, 8, 15, 300000, 6000–6008, 14001–14018) was independently re-extracted from **both** `codebuddy.js` and `codebuddy-headless.js` with matching values. Marketplace concurrency literals (3/5/2/4/16/16) and the retryable-status predicate (408/409/429/5xx) were likewise re-verified in both. No file under `D:\WorkBuddy\` or `E:\反代理\_wb_rev\` was modified; the only file written anywhere was a read-only PowerShell helper (`_scan_ctx.ps1`) in the working directory.
