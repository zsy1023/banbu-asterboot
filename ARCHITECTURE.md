# Bambu A1 × AstrBot 状态插件 —— 架构设计

> 状态：设计稿（评审中，暂不写代码）· 版本 0.1 · 对应 AstrBot ≥ 4.9.2 · 拓竹云协议档案 `a1_v1`

本文描述 `astrbot_plugin_bambu_a1` 的整体架构。评审意见确认后再进入第 15 节的里程碑实现。

**已确认的决策**

| 决策 | 结论 |
|---|---|
| LLM 档位 | 由插件配置项 `llm_mode` 在 WebUI 里自由切换，**改动即时生效、无需重启**；`/banbu llm` 与自然语言也可改；默认 `summary_only` |
| **LLM 分界线** | 不再写死在代码里：`llm_route` 配置项（事件 → `direct`/`llm`/`both`）在设置里逐项改；见 6.3 |
| **推送形态** | 事实与模型解释**合并成一条**消息发出（合并窗口 3s / critical 0.8s），不拆两条；模型超窗就先发事实 |
| 凭据存储 | AES-256-GCM + **机器绑定盐**（换机器需重新绑定，安全优先） |
| 设备范围 | **本期只做单台 A1**；数据模型保留 `device` 字段以便扩展；**二期目标明确是烘干箱接入**（WebSocket 长连接被动推送），因此适配器层与联动钩子要做扎实，不能是摆设 |
| 工程规范 | 全程 git 版本控制，按里程碑分提交，见第 17 节 |

---

## 1. 目标与范围

### 1.1 一句话目标

在 AstrBot 里用 `/banbu` 完成拓竹账号绑定（手机号 + 验证码），插件在后台持续读取打印机 MQTT 状态，
**状态变化时主动推送**；同时支持**斜杠命令**和**自然语言**两种方式随时查状态、设提醒。
关键事件**不经过 LLM 直推**，需要解释/汇总/复盘的内容才**过一遍 LLM** 再推。

### 1.2 三个来源的分工

| 来源 | 角色 | 在本项目中的用途 |
|------|------|------------------|
| `docs.astrbot.app/dev/star/*` | 宿主框架 | 插件骨架、`@filter.*` 注册器、`context.send_message` 主动消息、`llm_generate` / `llm_tool`、`_conf_schema.json` 配置、KV/文件存储、事件钩子 |
| `zsy1023/banbu-a1-Status-acquisition` | 协议实现（事实基线） | 拓竹云端认证端点、MQTT broker/主题/指令、`print` 状态字段语义、事件检测与通知规则的判定逻辑 |
| `zsy1023/3D-dryer-for-esp32-c3` | 二期设备 | **本期不实现**，只预留 `DeviceAdapter` 抽象 + TCP Agent(8080) 命令契约 + 订阅钩子 |

### 1.3 明确不做（Non-goals）

- 不复制原项目的 HTTPS Web 仪表盘 / Windows 桌面通知 / 自签证书逻辑（AstrBot 有 WebUI）。
- 不做打印机摄像头（A1 无内置相机）。
- 不做云端设备自动发现、耗材库、切片文件上传。
- 不实现烘干箱（只留接口，见第 10 节）。

---

## 2. 事实基线：从两个仓库提取的协议

以下内容是**兼容层的输入**，全部要经过第 9 节的映射表访问，代码里不允许直接散落硬编码。

### 2.1 云端认证（`auth.py`）

| 项 | 中国区 | 国际区 |
|----|--------|--------|
| API 基址 | `https://api.bambulab.cn` | `https://api.bambulab.com` |
| 短信域名 | `https://bambulab.cn` | 同（当前实现两区共用） |
| 发码 | `POST {sms}/api/v1/user-service/user/sendsmscode` `{"phone","type":"codeLogin"}` | 同路径 |
| 登录 | `POST {api}/v1/user-service/user/login` `{"account","code"}` → `accessToken` | 同路径 |
| 换 uid | `GET {api}/v1/design-user-service/my/preference` → `uid` → MQTT 用户名 `u_{uid}` | 同路径 |
| CSRF | 先 `GET {sms}/api/csrf` 拿 `bbl_csrf_token` cookie，POST 时回显 `x-bbl-csrf-token` | 同 |

实现要点（都来自原项目踩坑记录，必须保留）：

- **Token 不跨区**：手机号在 `.cn` 收码就必须用 `.cn` 登录，否则报 `code:1`（验证码不存在/过期）。
- **发码接口经常超时/504，但短信其实已下发** → 发码结果按"已请求"处理，不据响应码判失败。
- **Cloudflare**：裸 `requests` 易被拦，`curl_cffi`（`impersonate="chrome"`）作为可选依赖，缺失时自动回退。
- 登录错误码：`1` 验证码过期/不存在，`2` 验证码不正确。
- 手机号**永不打日志全量**（`138****0000`），且发码前校验 `isdigit()`（防止把脱敏号回传）。

### 2.2 MQTT 通道（`mqtt_client.py`）

```
broker : cn.mqtt.bambulab.com:8883   (国际区 us.mqtt.bambulab.com，走端点表)
账号   : username = u_{uid}  password = accessToken
TLS    : 必须 CERT_NONE + tls_insecure_set(True)（拓竹 broker 证书链不被标准库接受）
订阅   : device/{serial}/report
发布   : device/{serial}/request
握手   : 1) {"pushing":{"sequence_id":"1","command":"start"}}
         2) {"pushing":{"sequence_id":"1","command":"pushall"}}   ← 全量快照
```

- `report` 报文形如 `{"print": {...}}`；**pushall 是全量，后续增量报文只带变化字段** → 必须有快照合并逻辑。
- 原项目的判据是 `len(data["print"]) > 10`（粗略的全量判定），本插件改为"收到 `pushall` 响应后建基线 + 增量合并"。
- 控制指令 publish 到 `device/{serial}/request`，不等响应。

### 2.3 状态字段语义（`core.py`）

| 内部字段（canonical） | 云端字段 | 说明 |
|---|---|---|
| `state_raw` | `print.gcode_state` | `IDLE` / `PREPARE` / `RUNNING` / `PAUSE` / `FINISH` / `FAILED` |
| `percent` | `print.mc_percent` | 0–100 |
| `layer` / `total_layers` | `print.layer_num` / `print.total_layer_num` | |
| `remaining_min` | `print.mc_remaining_time` | 分钟 |
| `elapsed_min` | `print.mc_print_time` | 分钟 |
| `total_min` | `print.mc_print_total_time` | 分钟 |
| `nozzle.current/target` | `print.nozzle_temper` / `nozzle_target_temper` | °C |
| `bed.current/target` | `print.bed_temper` / `bed_target_temper` | °C |
| `chamber` | `print.chamber_temper` | °C |
| `task_name` | `print.subtask_name` | 任务名 |
| `speed` | `print.spd_mag` | |
| `fan.*` | `print.cooling_fan_speed` / `heatbreak_fan_speed` / `big_fan1_speed` / `big_fan2_speed` | 十六进制→百分比 |
| `light` | `print.lights_report[0].mode` | `on` / `off` |
| `ams[].trays[]` | `print.ams.ams[].tray[]` | `tray_type` / `tray_color` / `tray_remain` |
| `vtray` | `print.vtray.tray[0]` | 外挂耗材（不经过 AMS） |
| `hms` | `print.hms[]` | 故障码列表 |
| `print_error` | `print.print_error` | 高 8 位 = 故障模块 |
| `stage` | `print.stg_cur` | 阶段码，暂停原因的主要来源 |
| `wifi` | `print.wifi_signal` | |

### 2.4 事件判定与暂停原因（`core.py: EventEngine`）

- **跃迁事件**：`IDLE/PREPARE → RUNNING` 开始打印 · `RUNNING → PAUSE` 暂停 · `→ FINISH` 完成 · `→ FAILED` 失败。
- **层到达**：`layer_num` 变化即算事件（原项目如此），提醒规则再做阈值过滤。
- **暂停原因**优先级：`stg_cur` 阶段码 > `hms` 错误码 > `print_error` 模块，含 `filament_runout`（断料）、`gcode_pause`、`manual_pause`、`lost_step` 等 20+ 种。
- **首轮不误报**：服务启动首次拉取只建立基线，不把"当前已在 FINISH/FAILED/PAUSE"当成刚发生的事件（仅 `RUNNING`/`layer` 保留）。
- **通知规则去重**：同一规则一个任务内只触发一次，`RUNNING` 开始时 `reset_triggered()`。

### 2.5 控制指令（本项目只开放安全子集）

| 动作 | payload | 开放 |
|---|---|---|
| 灯光 | `{"system":{"command":"ledctrl","led_node":"chamber_light","led_mode":"on\|off"}}` | ✅ |
| 暂停 / 继续 / 停止 | `{"print":{"sequence_id":"N","command":"pause\|resume\|stop"}}` | ✅（需二次确认） |
| 控温 / 移动轴 / 发 G-code | — | ❌ 本期不开放 |
| 烘干箱 | 见 10.2 | 二期 |

### 2.6 烘干箱 TCP Agent（二期用，本期仅登记契约）

`TCP <device-ip>:8080`，行协议：`STATUS` / `START` / `STOP` / `SETTEMP <0-100>` / `SETTIME <1-999>` /
`MANUAL ON|OFF` / `HEATER ON|OFF` / `FAN ON|OFF` / `SINGLESENSOR ON|OFF` / `RESTART` / `AUTH <key>`；
`STATUS` 返回
`{"boxTemp","heaterTemp","state","targetTemp","dryTime","remaining","heater","fan","fault","manual","singleSensor"}`。
另有一套 `GET /api/status`、`POST /api/control|manual` 的 HTTP 接口。

> **二期形态补充**：烘干箱后续会**通过 WebSocket 长连接主动推送**（不是我们轮询它）。
> 所以适配器层不能只按"拉"来设计，必须同时容纳"推"（见 10.2 的 `PushSource`）。这件事本期不做，但接口先留。

---

## 3. 总体架构

```
┌─────────────────────────────── AstrBot (宿主) ────────────────────────────────┐
│  WebUI 配置页 (_conf_schema)   llm_generate / llm_tool   平台适配器 (QQ/Telegram) │
└───────────────▲───────────────────────▲──────────────────────────▲────────────┘
                │ AstrBotConfig         │ LLM 调用                  │ context.send_message(umo)
┌───────────────┴───────────────────────┴──────────────────────────┴────────────┐
│                        astrbot_plugin_bambu_a1 (main.py)                      │
│                                                                               │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────────┐  │
│  │ command.py   │   │ llm_tools.py │   │ router       │   │ llm_bridge.py │  │
│  │ /banbu 命令组 │   │ 自然语言入口  │   │ coalescer    │   │ 文案/诊断/复盘 │  │
│  │              │   │              │   │ push 投递     │   │               │  │
│  └──────┬───────┘   └──────┬───────┘   └──────▲───────┘   └───────▲───────┘  │
│         │                  │                  │                   │          │
│  ┌──────▼──────────────────▼──────────────────┴───────────────────┴───────┐  │
│  │                        core (与 LLM 无关的纯逻辑)                       │  │
│  │  BindManager │ MonitorEngine │ EventEngine │ RuleEngine │ StateStore   │  │
│  └──────┬───────────────┬──────────────────────────┬──────────────────────┘  │
│         │               │                          │                          │
│  ┌──────▼──────┐ ┌──────▼───────┐          ┌───────▼────────┐               │
│  │ credentials │ │  DeviceAdapter 注册表 │  │ hooks (事件总线) │               │
│  │ 加密存储     │ │ bambu_a1 ✅ / dryer ⏳ │  │ 8 个扩展点      │               │
│  └─────────────┘ └──────┬───────┘          └────────────────┘               │
└──────────────────────────┼──────────────────────────────────────────────────┘
                           │
              ┌────────────▼─────────────┐        ┌──────────────────────┐
              │ 拓竹云 REST + MQTT 8883   │        │ 烘干箱 TCP 8080（二期）│
              └──────────────────────────┘        └──────────────────────┘
```

**分层原则**

1. `core` 层不认识 AstrBot，也不认识 LLM —— 只产出结构化 `Snapshot` / `DeviceEvent`，可单测。
2. `push` 层负责"事实 → 文本"，纯模板、可预测、零延迟。
3. `llm_bridge` 只在被明确要求时介入（第 6 节），输入是结构化 JSON，输出是措辞。
4. 设备差异全部关在 `DeviceAdapter` 后面。

---

## 4. 目录与模块职责

```
banbu-asterboot/               ← 仓库根 == 插件包根（AstrBot 只认包根下的 main.py / metadata.yaml）
├── metadata.yaml              # 市场元数据：name/display_name/short_desc/desc/version/author/repo/
│                              #   astrbot_version/social_link/tags/support_platforms
├── _conf_schema.json          # WebUI 可视化配置（第 11 节）
├── requirements.txt           # paho-mqtt, requests, cryptography, curl_cffi(可选)
├── README.md                  # 市场首页：居中 banner + 徽章 + 折叠章节
├── CHANGELOG.md               # 更新日志（版本与 metadata.version 对齐）
├── LICENSE                    # GPL-3.0（与作者另两个仓库一致）
├── logo.png                   # 256×256 像素风图标（tools/make_logo.py 生成）
├── banner.png                 # 1280×320 README 顶部图（tools/make_banner.py 生成）
├── main.py                    # Star 子类：装配 + 命令 + LLM 工具 + 生命周期（薄）
├── .astrbot-plugin/i18n/      # 插件国际化（宿主约定的目录名）
│   ├── zh-CN.json
│   └── en-US.json
│
├── bambu_a1/                  # 逻辑全部在这里，可脱离 astrbot 单测
│   ├── models.py              # Snapshot / DeviceEvent / Rule / Account / DeviceAction 数据类
│   ├── store.py               # 原子写 JSON（tmp+replace）、轮转 jsonl、锁
│   ├── logging_compat.py      # 日志门面：只用宿主插件 logger，绝不进 astrbot.* 命名空间
│   ├── compat.py              # 协议档案：端点表 / 字段映射 / 状态映射 / 漂移统计
│   ├── credentials.py         # AES-256-GCM + 机器绑定盐，密钥派生与掩码
│   ├── auth.py                # 发码 / 登录 / 换 uid / 列举设备（端点全来自档案）
│   ├── mqtt.py                # poll 单次取全量 + stream 长连接增量（paho 阻塞调用进线程）
│   ├── monitor.py             # 后台循环：拉取→合并→detect→分发；auto 降级、退避、静默判离线
│   ├── events.py              # 跃迁/故障码/断料/里程碑/离线检测 + 去重（EventState 持久化）
│   ├── rules.py               # 13 种提醒规则类型 + 校验 + 持久化
│   ├── hooks.py               # 8 个扩展点的钩子总线（异常隔离）
│   ├── router.py              # 分界线：rule > llm_route > 默认，受 llm_mode 总闸约束
│   ├── coalescer.py           # 合并窗口：事实 + 模型补写合成一条，超窗先发事实
│   ├── render.py              # 确定性文本渲染（状态卡、各事件文案、规则列表）
│   ├── push.py                # 订阅者清单 + 安静时段 + context.send_message
│   ├── prompts.py             # 系统提示词、防编造约束、事实注入模板
│   ├── bridge.py              # llm_generate 封装：超时/预算/降级/供应商选择
│   ├── tools.py               # 自然语言入口的实现（被 main 的 llm_tool 薄封装调用）
│   ├── adapters/
│   │   ├── base.py            # DeviceAdapter + PollSource/PushSource + 注册表
│   │   ├── bambu_a1.py        # 拓竹 A1（本期实现）
│   │   └── dryer_esp32.py     # 烘干箱骨架（available=False，调用明确报"未实现"）
│   └── protocol_profiles/
│       ├── a1_v1.json         # 可热更新的端点/字段映射/状态码表
│       └── dryer_v1.json      # 二期占位
│
├── tests/                     # 本地验证，不随插件包分发（.gitattributes export-ignore）
│   ├── run_tests.py           # 入口：python tests/run_tests.py
│   ├── test_core.py           # 事件/规则/路由/合并/渲染/加密/存储/监控集成
│   ├── test_plugin_smoke.py   # 用 stub 宿主验装配、绑定链路、推送链路
│   ├── test_packaging.py      # 市场规范合规（元数据/体积/i18n/目录布局/文档链接）
│   ├── test_logging.py        # 日志兼容层（禁止 astrbot.* 自建 logger、宿主 formatter 复现）
│   └── stubs/astrbot/         # 最小 AstrBot stub（Star/Context/filter/MessageChain）
│
├── tools/                     # 开发脚本，不随插件包分发
│   ├── install-hooks.ps1
│   ├── make_logo.py           # 生成 logo.png（--preview 打 ASCII 网格）
│   ├── make_banner.py         # 生成 banner.png（5×7 像素字体内置）
│   └── git-hooks/post-commit
└── ARCHITECTURE.md            # 本文件（不随插件包分发）
```

**为什么逻辑收进一个子包 `bambu_a1/`**：插件若把 `core.py` / `llm/` / `ui/` 直接摊在插件根目录，
就必须把插件目录塞进 `sys.path` 才能导入，会与其它插件产生同名模块冲突（多个插件都叫 `core` 时
后加载的会覆盖前面的，reload 后更难排查）。收进唯一命名的 `bambu_a1/` 后，
`main.py` 用"先相对导入、失败再兜底"的两段式导入即可同时兼容两种宿主加载方式。
协议档案跟着代码走（`bambu_a1/protocol_profiles/`），避免运行期数据目录与可入库资产混在一起。

**日志约束（0.1.2 事故后固化）**：`bambu_a1/*` 里一律 `from .logging_compat import get_logger`，
禁止 `logging.getLogger("astrbot...")`。宿主 `astrbot` logger 上挂着格式串含 `%(plugin_tag)s` 的
`LogQueueHandler`，而 `plugin_tag` 只由 `LogManager` 创建 logger 时注入的 `_RecordEnricherFilter` 提供；
自建同名空间 logger 的记录冒泡上去会直接抛 `ValueError` 把命令打挂。门面另外吞掉日志系统异常，
保证"日志永远不影响业务"，并在绑定宿主 logger 前回退到 `bambu_a1` logger（不在 `astrbot.*` 下）。

运行期数据（不在插件目录，遵循 AstrBot 规范）：

```
data/plugin_data/astrbot_plugin_bambu_a1/
├── account.json        # 加密凭据：region / phone_masked / uid / username / token_cipher / key_id
├── subscribers.json    # 推送目标 umo 列表 + 各自的过滤/安静时段
├── rules.json          # 用户提醒规则
├── event_state.json    # 最近快照 + 去重指纹 + 任务基线（重启不重复报警）
└── events.jsonl        # 事件流水（按大小轮转，默认 5MB / 保留 2 份）
```

---

## 5. 关键流程

### 5.1 绑定流程（`/banbu`）

```
用户                    main.py / BindManager              拓竹云            本地存储
 │ /banbu bind 138xxxx                │                       │                 │
 │───────────────────────────────────►│ 校验格式(isdigit)      │                 │
 │                                    │ GET /api/csrf ────────►│                 │
 │                                    │ POST sendsmscode ─────►│ (常超时，忽略)   │
 │  ⚠️ 验证码已发送（5分钟内有效）      │◄───────────────────────│                 │
 │◄───────────────────────────────────│  pending={phone,region,expire}         │
 │ /banbu code 123456                 │                       │                 │
 │───────────────────────────────────►│ POST login ───────────►│                 │
 │                                    │◄── accessToken ────────│                 │
 │                                    │ GET preference ───────►│ uid             │
 │                                    │◄───────────────────────│                 │
 │                                    │ 加密写入 account.json ─────────────────►│
 │                                    │ 校验 token（可选试连 MQTT）──────────────►│
 │  ✅ 绑定成功 · 请输入 /banbu sn 01P00A...                    │                 │
 │◄───────────────────────────────────│ 记录本会话 umo 为推送目标 ──────────────►│
 │ /banbu sn 01P00A12345678           │                       │                 │
 │───────────────────────────────────►│ 建立 MQTT 基线 + 首轮快照               │
 │  ✅ 设备已绑定 · 当前 IDLE · 监控已开启                       │                 │
```

设计约束：

- **凭据只走命令，永不走 LLM**。手机号/验证码不进入 LLM 上下文、不进日志、不进事件流水。
- `pending` 只存内存（15 分钟 TTL），进程重启即失效，避免验证码残留落盘。
- `accessToken` 失效（401 / MQTT `rc≠0` 连续失败）→ 推送"需要重新绑定"，并暂停监控，不反复重试刷屏。
- 忘记序列号：预留 `GET /v1/iot-service/api/user/bind` 自动列举（端点进端点表，失败则提示手动 `/banbu sn`），本期不阻塞主流程。

### 5.2 凭据加密存储（`core/credentials.py`）

```
master_secret  ← 配置项 secret_key（secret:true；首次为空则随机生成 32B 并 save_config 回写）
machine_salt   ← SHA256(hostname + 首个 MAC + plugin_instance_id)
key            ← scrypt(master_secret, machine_salt, n=2^14, r=8, p=1) → 32B
密文           ← AES-256-GCM(key, nonce=随机12B, aad=b"bambu:/account")
                 account.json = {v:1, key_id:sha256(key)[:8], nonce, ct, updated_at}
```

- 落盘字段只有 `phone_masked`（`138****0000`）；完整手机号也只加密保存。
- `key_id` 用于判定"密钥已变"（换机 / 用户改了 `secret_key`）→ 明确报"凭据无法解密，请重新 `/banbu bind`"，不静默丢数据。
- 未安装 `cryptography` → **拒绝绑定**并提示安装，绝不降级明文。
- `/banbu resecret` 支持轮换 master_secret 并重加密（原子的 tmp+rename）。
- 序列号不属机密，但同样放在 account.json 里统一管理。

### 5.3 后台监控（`core/monitor.py`）

三种通道，配置项 `monitor_mode = auto | stream | poll`：

| 模式 | 行为 | 适用 |
|---|---|---|
| `poll` | 每 `poll_interval`（默认 30s）建一次连接、`pushall`、断连 | 最稳，原项目方案；延迟 ≤ 30s |
| `stream` | 一条长连接常驻，订阅 `report`，增量报文实时合并 | 低延迟（秒级）；需处理断线重连 |
| `auto`（默认） | 优先 `stream`，连续 N 次连接失败降级到 `poll`，恢复后再升级 | 兼顾 |

实现要点：

- paho 是**阻塞库** → 全部 `await asyncio.to_thread(...)`，专用单线程 executor，绝不在事件循环里 `sleep`。
- **快照合并**：`pushall` 建立基线，增量报文按 `print` 子树做深度 merge；未知新字段**原样保留**并计入漂移统计（第 9.3 节）。
- **任务基线**：`task_id`/`subtask_id` 变化视为新任务 → 重置规则去重、记录开始时间。
- **静默检测**：超过 `stale_after`（默认 3×poll_interval）无报文 → `offline` 事件（直推），恢复时 `online`。
- 抖动抑制：同一任务同一规则只触发一次；跨任务阈值规则有 `cooldown_s`；`PAUSE`→`RUNNING` 5 秒内来回不重复播报（原项目 auto_retry 场景）。
- `terminate()` 必须 cancel task、关闭连接、flush 存储。

### 5.4 事件检测（`core/events.py`）

`detect(prev: Snapshot|None, cur: Snapshot, rules: list[Rule]) -> list[DeviceEvent]`

事件对象（发送给渲染层、LLM 层、钩子层共用）：

```python
@dataclass
class DeviceEvent:
    type: str            # print_started / print_paused / print_resumed / print_finished /
                         # print_failed / layer_changed / rule_hit / offline / online /
                         # hms_error / filament_runout / temp_alarm / milestone
    severity: str        # info | warn | critical
    device: str          # device key, e.g. "bambu_a1"
    data: dict           # 结构化事实（温度、层号、错误码、任务名…）
    rule_id: str | None  # 命中哪条用户规则
    channel: str         # direct | llm | both   ← 路由决策，见 6.3
    dedup_key: str       # 任务内唯一指纹，用于去重与持久化
    ts: float
```

`detect` 是纯函数（无 IO、无 LLM、无 AstrBot 依赖），可直接喂 JSON 夹具做单测。

**首轮基线规则（0.1.4 事故后固化）**：绑定/重启后的第一次快照只建立基线，**不产生任何事件**。
跃迁（开始/完成/失败/暂停）、HMS 故障码、进度里程碑三类都必须同时满足
"有 prev、不是 first_poll、状态语义成立"才允许上报：

| 事件 | 额外前提 |
|---|---|
| `print_started` / `print_resumed` | 前态为 paused 时算恢复，其余算开始 |
| `print_finished` / `print_failed` | `prev_state` 不是同态，且非首轮 |
| `hms_error` | `prev` 存在且故障码集合**有新增** |
| `milestone` | `cur.state == "printing"` + `prev.percent` 已知 + 增量跨过阈值 |

> 反面教材：早期只对"跃迁类"做了首轮抑制，结果绑定一台停在 100% 的机器时，
> 一次性推送了 25%/50%/75%/90% 四条"进度"，每条还带"剩余 0 分钟"。
> 现在 milestone 要求"正在打印 + 增量"，渲染层也不再显示 0 分钟的剩余时间。

### 5.5 推送链路（`ui/push.py`）

```
DeviceEvent[] ─► 去重(dedup_key) ─► 路由(rule > llm_route > 默认) ─► PushCoalescer(窗口)
                                                                        │
                       ┌────────────────────────────────────────────────┤
                       │ channel=direct                                 │ channel=llm
                       ▼                                                ▼
              render.py 模板 ────────┐                      并行 llm/bridge.py 补写
                                     │                                 │
                                     └──── 合并(同设备/同订阅者) ◄───────┘
                                                    │        （超时/失败 ⇒ 只发模板）
                                                    ▼
                                       context.send_message(umo, chain)   ← 一条消息
```

- **推送目标 = 谁绑定就推给谁**：`/banbu bind` 成功时把当前会话的 `event.unified_msg_origin` 写入
  `subscribers.json`；之后所有推送发给这些 umo。不做群白名单、不做跨会话转发。
  想在另一个会话也收，就在那个会话里 `/banbu subscribe`（同一账号多会话 = 多条记录）。
- 每订阅者可设：`events`（订阅哪些类型）、`quiet_hours`（**可选项**，关闭/留空即不生效）。
- 消息形态**固定纯文本**（跨平台最稳，不受图片渲染依赖影响）；将来若要文转图，用
  `on_before_send` 钩子在消息链上换实现，核心与路由都不用改。
- 投递失败（平台不支持主动消息）→ 记 warning 并写 `events.jsonl`，不重试轰炸；`/banbu test-event` 可手动验证链路。
- 消息长度：单条超平台上限时按事件拆分，不截断关键数值。
- 合并/路由的细节见第 6.3 / 6.4 节；`ui/push.py` 只负责"拿到已经决定好的消息链并投递"，不做路由判断。

---

## 6. 【核心】LLM 边界：什么直推、什么过模型

判定依据三条：**时效性**（报警不能慢）、**准确性**（数值不能编）、**信息增益**（模型是否真的能多说出点什么）。

### 6.1 直推通道（不经过 LLM，模板渲染，`channel="direct"`）

> 下表是**出厂默认路由**。分界线不写死在代码里：每个事件类型 → `direct` / `llm` 的映射是配置项
> `llm_route`（WebUI 可改、即时生效），见 6.3。

| # | 事件/场景 | 触发 | 为什么不过 LLM |
|---|---|---|---|
| 1 | `print_started` 开始打印 | `→RUNNING` | 纯事实，1 行就说清 |
| 2 | `print_paused` 暂停（含暂停原因） | `RUNNING→PAUSE` | 断料/堵料必须秒级到达；原因来自 `stg_cur`/HMS，模型复述只会增加出错面 |
| 3 | `print_resumed` 恢复 | `PAUSE→RUNNING` | 同上 |
| 4 | `print_finished` 完成 | `→FINISH` | 耗时/耗材/任务名都是确定的数字 |
| 5 | `print_failed` 失败 | `→FAILED` | 失败必须立刻知道，错误码必须原样呈现 |
| 6 | `hms_error` 故障码 | `hms[]` 新增 | 错误码不能改写；建议可事后由 LLM 补 |
| 7 | `filament_runout` 断料 | AMS/`stg_cur` | 同上，且要抢在废件前 |
| 8 | `temp_alarm` 温度越限 | 规则阈值 | 安全类，毫秒级、可审计 |
| 9 | `offline` / `online` 离线与恢复 | 静默超时 | 这本身就是"链路异常"，不能再依赖 LLM 链路 |
| 10 | `milestone` 进度里程碑 | 25/50/75/90% 或指定层 | 用户自己设的规则，事实简单 |
| 11 | `rule_hit` 其他自定义规则 | 规则引擎 | 用户的规则输出应当是确定性的、可预期的 |
| 12 | `/banbu status` 等**命令回执** | 斜杠命令 | 秒回；同一条信息走模型要 2–10s 且更贵 |
| 13 | 绑定/解绑/配置变更回执 | `/banbu *` | 系统交互反馈，措辞固定更易排错 |
| 14 | 错误与降级提示（token 失效、MQTT 连不上、密钥变更） | 内部状态 | 运维信息，必须原样可见 |

> 判定口诀：**"状态跃迁 + 硬阈值 + 用户显式规则 + 系统自检" → 直推。**

### 6.2 LLM 通道（先过模型再推，`channel="llm"`）

> 同样是**默认值**，可在 `llm_route` 里逐项改。比如你希望"进度里程碑也带上模型的一句话打气"，
> 把 `milestone: llm` 即可，不用改代码。

| # | 场景 | 输入给模型的东西 | 输出/上限 |
|---|---|---|---|
| 1 | **完成复盘**：一个任务结束后 30s 内生成小结 | 任务名、总耗时、层数、耗材/AMS 用量、最高/平均温度、期间告警次数、与最近 5 次同类任务对比 | ≤ 120 字；数字必须引用输入，缺失就说"未知" |
| 2 | **异常诊断建议**：`print_failed` / `hms_error` 时补写（与事实**合并成同一条**，见 6.4；模型超时则只发事实） | 错误码、阶段码、暂停原因、当时温度与进度、最近事件序列 | ≤ 150 字；给出 2–3 条可执行排查建议 |
| 3 | **周期摘要**：每日/每 N 小时汇总 | 当日任务列表、成功率、总耗材、失败原因分布 | ≤ 200 字；安静时段合并到次日清晨 |
| 4 | **自然语言问答**：用户问"打得怎么样/还要多久/为什么停了" | 结构化 `Snapshot` JSON（由工具注入） | 直接回复，不二次推送 |
| 5 | **多事实对比/趋势**：温度异常、反复同一错误、进度停滞 | 最近 N 条快照序列 + 事件流水 | ≤ 150 字；仅在判定"值得说"时触发 |
| 6 | **提醒规则的自然语言→结构化**：用户说"打一半提醒我" | 用户原话 + 规则类型清单 | 输出 JSON 规则，由 `RuleEngine` 校验后落盘；**不由模型直接执行** |

> 判定口诀：**"需要解释 / 归因 / 跨时间对比 / 跨设备关联 / 把自然语言变结构化" → 过 LLM。**

### 6.3 分界线可配置：`llm_route`（核心）

`6.1 / 6.2` 是出厂默认，**真正的判据是配置项**。路由在事件进入推送队列前查表决定，改配置即时生效：

```json
{
  "llm_route": {
    "print_started":  "direct",
    "print_paused":   "direct",
    "print_resumed":  "direct",
    "print_finished": "llm",
    "print_failed":   "llm",
    "hms_error":      "llm",
    "filament_runout":"direct",
    "temp_alarm":     "direct",
    "offline":        "direct",
    "online":         "direct",
    "milestone":      "direct",
    "rule_hit":       "direct",
    "task_summary":   "llm",
    "periodic_digest":"llm"
  }
}
```

| `channel` | 行为 |
|---|---|
| `direct` | 模板渲染，永不经过模型（最省、毫秒级） |
| `llm` | 先取事实 → 模型补写 → 与事实**合并成一条**推送（6.4）；失败/超时/超预算则退回纯模板，**消息照发** |
| `both` | 事实模板 + 模型补充各发一条（一般不用，重连风暴时会更吵） |

实现要点：

- `EventEngine` 产出的 `DeviceEvent.channel` 由 `router.resolve(event, config.llm_route, rule)` 填充，
  优先级：**单规则 `rule.channel` > 事件类型 `llm_route` > 事件默认值**。
- 全局 `llm_mode = off` 时，`llm` 与 `both` 一律降级为 `direct`（一个总闸，避免"关掉 LLM 但配置里还写着 llm"的困惑）。
- 未在 `llm_route` 里声明的**新事件类型**默认 `direct`（宁可直推也不静默），并在 `/banbu protocol` 里提示"该类型尚未配置路由"。
- `temp_alarm` / `offline` / 凭据失效这类安全与自检事件，即使被改成 `llm`，`bridge` 也有 20s 硬超时兜底 → 最坏情况退化成模板直推，不会丢。
- `/banbu route list` / `/banbu route set <event> <channel>` 提供命令入口，自然语言工具 `bambu_set_route` 同样可以改。

### 6.4 合并推送：一条消息，不拆两条

原设计是"直推事实 + 异步补解释"两条消息，改为**合并窗口**：窗口内到达的事件与模型补充合成一条发出。

```
事件入队 ──► PushCoalescer（窗口 W，默认 3s；critical 0.8s）
              │
              ├─ channel=direct ─► 立即渲染 ─────────► 合并同设备事件 ─► 一条消息
              └─ channel=llm ────► 并行发起模型请求
                                    ├─ 在 W 内返回 ⇒ 事实块 + 解释块合为一条
                                    └─ 超 W / 失败 ⇒ 立刻发事实块（解释块丢弃，记 llm_fallback）
```

- **告警永不丢**：模型的返回时间是"可优化项"，窗口到点就先发事实，不等模型。
- **不拆两条**：成功时是一条带两段的消息（事实在上、解释在下，用分隔线区分）；失败时只有事实。
- 窗口值可配：`merge_window_s`（默认 3）、`merge_critical_window_s`（默认 0.8）；
  设成 `0` 即完全关闭合并（退回逐条即时发送）。
- 窗口内同设备多事件（例如 `print_failed` + `hms_error` + `rule_hit` 同时命中）合并成一条，
  按 `severity` 排序，`critical` 置顶。
- 合并只发生在**同设备、同订阅者**之间；跨订阅者不合并（各人安静时段不同）。
- 去重先于合并：先按 `dedup_key` 去重，再进窗口，避免"同一条重复报文进了窗口算两次"。

### 6.5 成本与稳定性护栏（`llm/bridge.py`）

| 护栏 | 默认 | 说明 |
|---|---|---|
| `llm_mode` | `summary_only` | `off` 全直推 / `summary_only` 只做完成复盘与问答 / `full` 含诊断与摘要。**WebUI 可改、即时生效**，与 `/banbu llm` 同一字段 |
| `llm_timeout` | 20s | 超时 → 放弃，退化为模板直推 |
| `llm_budget_per_hour` | 12 | 超预算直接走模板，并记一条 `budget_exceeded` |
| 路由缺失 | 新事件类型默认 `direct` | 未配置的事件永不静默；`/banbu protocol` 会列出未配置项 |
| `llm_mode=off` | 总闸 | `llm` / `both` 一律降级 `direct`，配置保留不清空 |
| 去重 | 同 `dedup_key` 不重复调用 | 断线重连、重复报文不重复计费 |
| 提示词缓存 | 不把每轮变化的状态写进 `system_prompt` | 状态作为单轮 user 内容注入（遵循 AstrBot 官方建议，避免破坏缓存） |
| 防编造 | 系统提示词硬约束 + 结构化输入 | "只能引用 `<facts>` 中的数值；缺失写'未知'；不得推测温度/时间" |
| 供应商选择 | `llm_provider_id`（可空） | 空则用当前会话 provider；失败可配 `llm_fallback_provider_id` |
| 敏感信息 | token/手机号/序列号**永不进 prompt** | 序列号仅以"设备 A1"代称 |

### 6.6 用户可覆盖（三个入口，同一份数据）

- 全局档位：`/banbu llm off|summary|full`（= 配置项 `llm_mode`）
- 逐事件路由：`/banbu route set print_finished direct`（= 配置项 `llm_route`）
- 单规则：`/banbu remind add ... --llm`（该规则命中时走 LLM 措辞，例如"温柔点提醒我"）
- 自然语言："以后每次打印完都给我写个小结" → 工具 `bambu_set_llm_pref`；"失败的时候别让模型瞎猜，直接告诉我错误码" → 工具 `bambu_set_route`

三个入口写的是同一份 `AstrBotConfig`，所以 WebUI 改完命令里能立刻看到，反之亦然。

---

## 7. 提醒规则模型（`core/rules.py`）

覆盖原项目 `NotifyRuleManager` 的全部 8 种触发类型 + 内置事件，且可扩展：

```json
{
  "id": "r_7f3a",
  "device": "bambu_a1",
  "type": "layer_reached",
  "value": 50,
  "value2": null,
  "scope": "job",
  "cooldown_s": 0,
  "channel": "push",
  "enabled": true,
  "label": "打到第 50 层提醒我",
  "created_by": "user",
  "created_at": 1750000000
}
```

| `type` | 语义 | 参数 |
|---|---|---|
| `print_complete` / `print_failed` / `print_paused` / `print_started` | 状态跃迁（内置，默认开） | — |
| `layer_reached` | 到达第 N 层 | `value` |
| `percent_reached` | 进度到 N% | `value` |
| `print_remaining` | **剩余** ≤ N 分钟 | `value` |
| `print_duration` | **已打印** ≥ N 分钟 | `value` |
| `nozzle_temp_above` / `below` | 喷头温度越限 | `value` |
| `bed_temp_above` / `below` | 热床温度越限 | `value` |
| `chamber_temp_above` | 腔温越限 | `value` |
| `temp_in_range` | 温度进入 `[value, value2]` 区间 | `value`, `value2` |
| `hms_code` | 出现指定 HMS 码（支持通配） | `value` |
| `filament_low` | AMS 余料 < N% | `value` |
| `offline` | 离线超过 N 分钟 | `value` |

- 规则统一为**数据**（JSON），`EventEngine` 只读数据不认类型 → 新增类型只改 `rules.py` 一处 + 映射表。
- LLM 生成的规则必须经 `RuleEngine.validate()`（类型白名单、范围校验、数量上限 50 条）才落盘。
- `/banbu remind list` 以表格输出；`--llm` 标记存在 `channel` 字段里。

---

## 8. 交互面

### 8.1 斜杠命令（`@filter.command_group("banbu")`，别名 `拓竹`、`竹子`）

| 命令 | 说明 | 直推 |
|---|---|---|
| `/banbu bind <手机号>` | 发送验证码 | ✅ |
| `/banbu code <验证码>` | 完成登录 + 加密存储 | ✅ |
| `/banbu sn <序列号>` | 绑定打印机并建立基线 | ✅ |
| `/banbu unbind` | 清除凭据（需二次确认） | ✅ |
| `/banbu status` | 当前状态卡（进度/层/温度/剩余/AMS） | ✅ |
| `/banbu monitor on\|off\|auto` | 后台监控开关/模式 | ✅ |
| `/banbu remind add <type> <value> [--llm]` / `list` / `del <id>` / `clear` | 提醒规则 | ✅ |
| `/banbu quiet on\|off` / `quiet <HH:MM-HH:MM>` | 安静时段（可选项，默认关闭） | ✅ |
| `/banbu subscribe` / `unsubscribe` | 本会话加入/退出推送 | ✅ |
| `/banbu llm off\|summary\|full` | LLM 总闸 | ✅ |
| `/banbu route list` / `route set <event> <direct\|llm\|both>` | **分界线逐项调整** | ✅ |
| `/banbu protocol` | 协议档案 / 漂移诊断 / 未配置路由的事件 | ✅ |
| `/banbu test-event <type>` | 手动造事件验证链路 | ✅ |
| `/banbu device list\|add` | 设备管理（烘干箱本期返回"未实现"） | ✅ |

参数解析用 AstrBot 的类型转换（`int` / `GreedyStr`）；若目标版本不支持 `GreedyStr`，回退到 `event.message_str` 自解析（在 `compat.py` 里做能力探测）。
命令只做**薄封装**：解析 → 调 `core` → 渲染，不含业务判断。

### 8.2 自然语言入口（LLM 工具）

| 工具名 | 用途 | 是否改动状态 |
|---|---|---|
| `bambu_get_status` | 取当前状态（返回结构化 + 建议表述） | 读 |
| `bambu_get_history` | 最近任务/事件 | 读 |
| `bambu_list_reminders` | 列出规则 | 读 |
| `bambu_add_reminder` | 新增规则（参数走校验） | 写（校验后） |
| `bambu_delete_reminder` | 删除规则 | 写 |
| `bambu_set_llm_pref` | 切换 LLM 档位（`llm_mode`） | 写 |
| `bambu_set_route` | 调整某类事件的走法（`llm_route`），如"失败别让模型瞎猜" | 写 |
| `bambu_control` | 灯光 / 暂停 / 继续 / 停止（**停止类需二次确认**） | 写 |
| `bambu_bind_guide` | 返回绑定步骤（**不接受手机号/验证码参数**） | 读 |

工具用 `@filter.llm_tool`，docstring 的 `Args:` 段严格按 AstrBot 要求书写（否则 schema 为空）。
工具**只返回事实 + 提示**，措辞交给主模型；避免工具内二次调用 LLM 造成双倍成本。

---

## 9. 兼容性设计（不写死）

> 需求原文："这两个项目都是我的，API 调用后期肯定要更新，注意兼容性，不要写死。"
> 目标：**云端改字段名/加字段/换域名时，改 JSON 不改 Python；协议大改时，换档案不改核心。**

### 9.1 三层可替换点

| 层 | 载体 | 变什么时改这里 |
|---|---|---|
| 端点/主题 | `protocol_profiles/*.json` → `endpoints` | 域名、路径、broker、topic 模板 |
| 字段语义 | 同上 → `field_map`（canonical → 候选路径数组） | 云端改名、层级调整、单位变化 |
| 枚举/文案 | 同上 → `state_map` / `pause_reasons` / `hms_notes` | 新增状态、错误码 |
| 设备行为 | `adapters/*.py` | 换机型、加烘干箱 |
| 事件判定 | `core/rules.py` + 规则 JSON | 新增提醒类型 |

### 9.2 字段映射示例（`field_map`）

```json
{
  "percent":      ["print.mc_percent", "print.percent", "print.progress.percent"],
  "layer":        ["print.layer_num", "print.layer"],
  "remaining_min":["print.mc_remaining_time", "print.remaining_time"],
  "nozzle.current":["print.nozzle_temper", "print.nozzle.temp"]
}
```

- 取值按**候选顺序**命中第一个；全部缺失 → 记 `field_miss:<canonical>` 到漂移统计（同进程只报一次 warning），返回 `None` 而不是 0，避免"0% 进度"这类假数据进消息。
- 数值带单位归一（`s→min`、`0–1→%`），归一函数也在档案里声明（`"scale": 0.01`）。
- 配置项 `protocol_overrides` 允许用户/我在不改代码的情况下临时补映射（深合并到档案）。

### 9.3 协议漂移检测

`compat.drift_report()` 收集：

- 收到但档案未声明的 `print.*` 字段（未知字段计数，保留原值）
- 档案声明但长时间没出现的字段
- `gcode_state` 出现未映射的新值
- HTTP 端点返回结构变化（登录响应缺 `accessToken` 等）

`/banbu protocol` 输出这张表 → 云端一改我就能立刻看到"哪几个字段开始飘了"，再决定改档案还是改代码。
`protocol_profile` 配置项切换档案（`a1_v1` → 未来的 `a1_v2` / `h2d_v1`），旧档案保留，便于回滚。

### 9.4 版本与依赖

- `metadata.yaml` 声明 `astrbot_version: ">=4.9.2"`（用到 KV 存储与 `add_llm_tools`）。
- 能力探测而非版本比较：`compat.has(capability)` 检查 `context.send_message` / `add_llm_tools` / `GreedyStr` 是否存在，缺失则走降级分支并明确提示。
- `requirements.txt`：`paho-mqtt>=2.0`、`requests`、`cryptography`；`curl_cffi` 列为可选（`try/except ImportError`）。

---

## 10. 扩展钩子与二期接口

> 二期目标已确认 = **把烘干箱接进来**。因此本节的 `DeviceAdapter` 抽象与钩子总线在 M1 就要定稿，
> 不允许出现"只有 A1 能用"的隐含假设：凡是设备相关的判断，一律走适配器 / 档案 / 规则三处之一。

### 10.1 内部事件总线（`hooks.py`）

同步、按名注册、异常隔离（一个钩子抛错不影响主流程，记 warning）：

| 钩子 | 时机 | 可做什么 |
|---|---|---|
| `on_snapshot(snapshot)` | 每次拿到（合并后）快照 | 落库、转发、画曲线 |
| `on_events(events, snapshot)` | 事件检测完、推送前 | 修改 `channel`、注入文案、抑制事件 |
| `on_before_send(payload, umo)` | 提交给平台前 | 改写消息链、加前缀、换渠道 |
| `on_bind_success(account)` | 绑定成功 | 触发首轮基线、初始化设备 |
| `on_unbind()` | 解绑 | 清理订阅与规则 |
| `on_llm_draft(prompt, draft, event)` | LLM 草案返回后 | 二次校验、拦截幻觉、替换为人设口吻 |
| `on_adapter_registered(key, adapter)` | 适配器注册 | 动态挂载新设备 |
| `on_monitor_error(err, phase)` | 监控异常 | 自定义告警/重试策略 |

对外也留一个薄封装：`from astrbot.api.event import filter` 的 `on_astrbot_loaded` 已在 `main.py` 用掉，
其他插件想联动，可用 AstrBot 的 `context.get_all_stars()` 拿到本插件实例后 `plugin.hooks.register(...)`。

### 10.2 烘干箱接口（本期只留骨架）

二期形态已明确：烘干箱**通过 WebSocket 长连接主动推送**状态。因此适配器的数据来源抽象成
**拉（Pull）+ 推（Push）两种源**，一个设备可以只有其中一种，也可以两者并存：

```python
# adapters/base.py
class PollSource(Protocol):
    """我们主动去问（A1 的 MQTT pushall、烘干箱的 TCP STATUS / GET /api/status 都属于这种）"""
    async def fetch(self) -> Snapshot: ...

class PushSource(Protocol):
    """设备主动送（烘干箱二期的 WebSocket 长连接；A1 的 report 增量也算）"""
    async def stream(self) -> AsyncIterator[PartialSnapshot]: ...   # 长连接 + 自动重连
    def supports_reconnect_backoff(self) -> bool: ...

class DeviceAdapter(ABC):
    key: str                                  # "bambu_a1" / "dryer_esp32"
    display_name: str
    capabilities: set[str]                    # {"status","control.light","control.pause",...}

    async def probe(self) -> bool: ...                        # 连通性
    def poll_source(self) -> PollSource | None: ...            # None = 只能推
    def push_source(self) -> PushSource | None: ...            # None = 只能拉
    async def execute(self, action: DeviceAction) -> ActionResult: ...
    def event_sources(self) -> list[str]: ...                 # 该设备能产出的事件类型
    def field_map(self) -> dict: ...                          # 复用 compat 档案
```

`MonitorEngine` 据此组合三种运行态，**对上层完全透明**：

| 设备提供 | 监控行为 |
|---|---|
| 只有 `PollSource` | 定时轮询（A1 的 `poll` 模式） |
| 只有 `PushSource` | 长连接常驻 + 断线指数退避重连 + `stale_after` 静默判离线 |
| 两者都有 | `auto`：长连接优先，连续失败降级轮询，恢复后升级（A1 的 `stream`/`poll`/`auto` 即此） |

```python
# adapters/dryer_esp32.py   —— 本期仅骨架
class DryerEsp32Adapter(DeviceAdapter):
    """TODO(二期)：
      - PushSource：WebSocket 长连接（设备主动推送状态），含 auth、心跳、断线重连
      - PollSource：兜底用，TCP 8080 行协议 STATUS / HTTP GET /api/status
      - 控制：TCP 8080 AUTH/SETTEMP/SETTIME/START/STOP/HEATER/FAN/SINGLESENSOR/RESTART
      - 字段映射：boxTemp→temperature.chamber.current、heaterTemp→temperature.heater.current …
      - 事件：drying_started / drying_done / overheat / sensor_fault / cooling
      - 与 A1 联动：print_finished 后询问式提议烘干（on_events 钩子，需二次确认）
    """
    key = "dryer_esp32"
    async def probe(self): raise NotImplementedError("dryer adapter 尚未实现（二期）")
    def poll_source(self): raise NotImplementedError("dryer adapter 尚未实现（二期）")
    def push_source(self): raise NotImplementedError("dryer adapter 尚未实现（二期）")
    async def execute(self, action): raise NotImplementedError("dryer adapter 尚未实现（二期）")
```

- 注册表在 `main.py` 启动时注册 `bambu_a1`；`dryer_esp32` 只在配置里出现时才 import，报"未实现"而不是崩。
- `Snapshot` / `DeviceEvent` / `Rule` 三层模型与设备无关，二期接入不需要动 `core`。
- **推模式需要的接收端**：WebSocket 需要宿主侧有个能接连接的入口。AstrBot 插件侧有两条路——
  ① 二期在插件内起一个只监听内网的 aiohttp 服务（`ws_url` 配置项，端口可配，默认关闭）；
  ② 若设备只支持"我连它"，则走 `PushSource.stream()`，插件作为客户端去连。
  本期不选型，但 `hooks.on_snapshot` 已经能接受外部注入的快照，两条路都不用改核心。
- 预留联动钩子语义：A1 `print_finished` → `on_events` 里可发"要不要顺手烘干耗材？"；
  是否先询问由配置项 `dryer.require_confirm` 决定（默认 true = 询问 + 二次确认，false = 按规则直接开始），二期由烘干箱适配器消费。

---

## 11. 配置项草案（`_conf_schema.json`）

| 分组 | key | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| 基础 | `enabled` | bool | true | 插件总开关 |
| 账号 | `region` | string | `China` | `China` / `Global`（端点表 key） |
| 账号 | `protocol_profile` | string | `a1_v1` | 协议档案 |
| 账号 | `protocol_overrides` | dict | `{}` | 临时覆盖字段映射 |
| 账号 | `serial` | string | `""` | 打印机序列号（也可 `/banbu sn`） |
| 安全 | `secret_key` | string(secret) | `""` | 主密钥，空则自动生成 |
| 监控 | `monitor_mode` | string | `auto` | `auto`/`stream`/`poll` |
| 监控 | `poll_interval` | int | 30 | 秒 |
| 监控 | `stale_after` | int | 90 | 秒，无报文判离线 |
| 监控 | `auto_start` | bool | true | 启动即监控 |
| 推送 | `push_targets` | list | `[]` | 额外 umo；默认推送目标就是**绑定会话**，此项一般不用手填 |
| 推送 | `quiet_hours_enabled` | bool | false | 安静时段**总开关**，关掉则完全不生效 |
| 推送 | `quiet_hours` | string | `23:00-07:30` | 安静时段区间（仅在上项为 true 时生效）；期间只放 `critical` |
| 推送 | `progress_milestones` | list | `[25,50,75,90]` | 进度里程碑 |
| 推送 | `merge_window_s` | int | 3 | 合并窗口（0 = 关闭合并，逐条即时发） |
| 推送 | `merge_critical_window_s` | int | 0.8 | `critical` 事件专用窗口（更短） |
| LLM | `llm_mode` | string | `summary_only` | `off`/`summary_only`/`full`，总闸 |
| LLM | `llm_route` | dict | 见 6.3 | **分界线**：事件类型 → `direct`/`llm`/`both`，WebUI 可逐项改 |
| LLM | `llm_provider_id` | string(_special:select_provider) | `""` | 留空用会话默认 |
| LLM | `llm_timeout` | int | 20 | 秒 |
| LLM | `llm_budget_per_hour` | int | 12 | 每小时上限 |
| 高级 | `mqtt_timeout` | int | 15 | 秒 |
| 高级 | `mqtt_retries` | int | 2 | 次 |
| 高级 | `log_level` | string | `INFO` | |
| 二期 | `dryer.require_confirm` | bool | true | 烘干箱联动是否需二次确认（false = 按规则全自动） |
| 二期 | `dryer.ws_url` | string | `""` | 烘干箱 WebSocket 地址（方向待二期定，先留位） |

---

## 12. 数据文件与生命周期

| 文件 | 写时机 | 读时机 | 失败策略 |
|---|---|---|---|
| `account.json` | 绑定/解绑/改密 | 启动、每次刷新前校验 | 解密失败 → 提示重新绑定，停止监控 |
| `subscribers.json` | subscribe/unsubscribe/安静时段 | 每次推送 | 损坏 → 备份为 `.bak` 并重建 |
| `rules.json` | 规则增删改 | 每轮 detect | 损坏 → 保留用户可读告警，不阻塞监控 |
| `snapshot.json` | 每次刷新（节流 5s） | 启动预热 | 缺失 → 等首轮 |
| `event_state.json` | 事件后 | 启动 | 缺失 → 首次只建基线（不误报） |
| `events.jsonl` | 每个事件 | `/banbu history`、LLM 复盘 | 超 5MB 轮转，保留 2 份 |

写文件统一走 `store.py` 的 `atomic_write`（`tmp` + `os.replace`），jsonl 用追加 + 定期压缩。

---

## 13. 安全与隐私

| 风险 | 应对 |
|---|---|
| token / 手机号泄露 | AES-256-GCM 加密落盘；日志与事件流水中仅 `138****0000`；token 永不进 LLM 上下文 |
| 拷走数据目录 | 密钥掺入机器绑定盐（hostname+MAC+实例 ID），换机不可解 |
| 验证码残留 | `pending` 仅内存 + 15 分钟 TTL |
| 危险操作 | `stop`/`unbind` 两步确认（`ConfirmManager` 模式，60s 内有效）；不开放控温与任意 G-code |
| 群聊泄露 | 状态推送默认只发私聊；群聊需显式 `/banbu subscribe`，且 `sn`/`unbind` 限管理员（`@filter.permission_type(ADMIN)`） |
| MQTT 证书不校验 | 拓竹 broker 现状所迫，保留 `CERT_NONE`，但在启动日志与 `/banbu protocol` 明示 |
| 云端反爬 | 保留 `curl_cffi` 指纹伪装、UA、CSRF 双提交；失败给出可读原因 |
| 频控 | 监控失败指数退避（30s→60s→…→10min 上限），避免被云端限流 |

---

## 14. 失败模式与降级矩阵

| 失败 | 检测 | 用户可见 | 系统行为 |
|---|---|---|---|
| 发码超时/504 | 异常 | "验证码已发送（响应异常，通常仍会收到）" | 继续等待 `/banbu code` |
| 登录失败 | HTTP 400 + `code` | 明确中文原因（过期/不正确） | 可重发 |
| token 失效 | 401 / MQTT `rc≠0` 连续 3 次 | "需要重新绑定"（一次，不刷屏） | 暂停监控，指数退避 |
| MQTT 连不上 | 连接异常 | 首次即报，之后静默 | 退避重试；`poll` 模式重连成本低 |
| 报文静默 | `stale_after` | `offline` 事件（直推） | 保持重连，恢复时报 `online` |
| 字段缺失 | `field_map` 全 miss | 消息里省略该行，不显示 0 | 记漂移统计 |
| LLM 超时/失败/超预算 | 异常/计数 | 只收到模板直推 | 记 `llm_fallback`，不影响主链路 |
| 平台不支持主动消息 | `send_message` 返回 False | 提示用户换会话 `/banbu subscribe` | 记日志，不重试 |
| 插件被卸载 | `terminate()` | — | cancel task、关连接、flush |

---

## 15. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1 骨架 | 目录、`metadata.yaml`、`_conf_schema.json`、`main.py` 空命令组、`store.py`、`compat.py`（档案 + 映射 + 漂移） | 插件能在 AstrBot 加载，`/banbu help` 有输出 |
| M2 绑定 | `credentials.py`（加密）、`auth.py`、bind/code/sn/unbind | 真实账号绑定成功，`account.json` 无明文 |
| M3 监控 | `mqtt.py`（poll+stream）、`monitor.py`、`events.py`、快照合并 | 打印中能看到实时进度变化；断网恢复 |
| M4 直推 | `render.py`、`push.py`、`router.py`（`llm_route` 查表）、`coalescer.py`（合并窗口）、`rules.py`、命令回执 | 完成/暂停/失败/断料/离线直推不重复；`llm_route` 改成 `llm` 后行为立刻变；合并成一条 |
| M5 LLM | `llm/bridge.py`、`tools.py`、完成复盘 + 诊断 + 自然语言问答 + 路由/档位的自然语言入口 | LLM 挂掉时告警仍准时到达，且只发一条 |
| M6 二期接口 | `adapters/base.py` 定稿（`PollSource` + `PushSource`）、`dryer_esp32.py` 骨架、hooks 全量 | 注册一个假适配器（推模式 + 拉模式各一个）即可跑通事件流 |
| M7 打磨 | `/banbu protocol` 漂移表、`/banbu route`、事件流水、文档与示例、`test-event` | 新云端字段/未配置路由都能被发现 |

单测重点（不依赖网络）：`events.detect` 状态跃迁与去重、`rules._match` 全部 12 种类型、
`router.resolve` 三级优先级与 `off` 总闸、`coalescer` 的窗口合并与超时先发、`compat` 字段映射与归一、
`credentials` 加解密与 `key_id` 变更。

---

## 16. 待定项与已定项

### 16.1 已定（不再讨论）

| 项 | 结论 | 落地位置 |
|---|---|---|
| LLM 档位可调 | `llm_mode` 是 WebUI 配置项，**改动即时生效**（不重启、不断监控）；`/banbu llm` 与自然语言 `bambu_set_llm_pref` 是同一个字段的第二/第三入口 | 第 6.5 / 8.1 / 11 节 |
| **LLM 分界线可配** | 事件类型 → `direct`/`llm`/`both` 的映射是配置项 `llm_route`，WebUI 逐项可改、即时生效；优先级 单规则 > `llm_route` > 事件默认；`llm_mode=off` 时全降级 `direct`；未配置的新事件默认 `direct` | 第 6.3 / 6.6 / 11 节 |
| **推送合并成一条** | 用 `PushCoalescer` 窗口（默认 3s / critical 0.8s）把"事实块 + 模型解释"合成一条；模型超窗或失败则只发事实块。**不拆两条，告警不丢** | 第 6.4 / 5.5 节 |
| 设备范围 | 本期单台 A1；`Snapshot`/`DeviceEvent`/`Rule` 都带 `device` 字段；二期目标 = 烘干箱接入 | 第 10 节 |
| **烘干箱推模式** | 二期用 **WebSocket 长连接被动推送**，故适配器抽象成 `PollSource` + `PushSource` 两种源；本期只定接口不实现 | 第 2.6 / 10.2 节 |
| 凭据加密 | AES-256-GCM + **机器绑定盐**（hostname+MAC+实例 ID）；无 `cryptography` 拒绝绑定。代价：**换机器/迁移需重新 `/banbu bind`** | 第 5.2 节 |
| 绑定入口 | 手机号/验证码只走 `/banbu` 命令，不进 LLM 上下文与日志 | 第 5.1 / 8.2 节 |
| **状态卡形态** | **纯文本**（跨平台最稳），不做文转图；将来要图可由 `on_before_send` 钩子外挂，不用改核心 | 第 5.5 / 8.1 节 |
| **推送目标** | **谁绑定就推给谁**：绑定/订阅时记录 `umo`，推送到这些会话；不做群白名单、不做跨会话转发 | 第 5.5 节 |
| **安静时段** | 是**设置里的可选项**：`quiet_hours` 留空 / 关闭即完全不生效；开启时只放 `critical`，其余静默累计 | 第 11 节 |
| **烘干箱二次确认** | 也做成**设置项**（二期 `dryer.require_confirm`，默认开），用户可关掉改成全自动 | 第 10.2 / 11 节 |

> 即时生效的实现细节：`main.py` 持有 `config` 引用，`router` / `bridge` 每次使用前读
> `self.config.get(...)`，**不缓存到实例属性**；配置热更新由 AstrBot 负责写回 JSON。

### 16.2 仍想听你意见

暂无。评审阶段的分叉已全部收敛；实现过程中若遇到新的真分叉（例如云端接口实际行为与档案不符、
或 WS 方向必须二选一），会在对应里程碑开工前单独问一次，不擅自替你决定。

---

## 17. 版本控制与工程规范
写代码阶段全程 git，**一次提交只做一件事**，每个里程碑打 tag 便于回滚。

### 17.1 仓库布局

本工作区 `E:\asterboot-bambulabMQTT` 就是插件仓库根，远端为
`https://github.com/zsy1023/banbu-asterboot.git`（`origin`；`main` 跟踪 `origin/main`）。
**仓库根 == 插件包根**：`metadata.yaml`、`main.py`、`bambu_a1/` 都在根，
这样"从仓库安装"与"市场 zip 安装"都能被宿主直接识别（宿主只在包根找 `main.py`）。

```
banbu-asterboot/                   ← git 仓库根 = 插件包根
├── .gitignore / .gitattributes
├── metadata.yaml                  # 市场元数据（author/name 即 plugin_id）
├── main.py                        # 插件入口
├── bambu_a1/                      # 逻辑包
├── README.md                      # 市场首页
├── ARCHITECTURE.md                # 本文件（export-ignore，不随包分发）
├── LICENSE / logo.png
├── .astrbot-plugin/i18n/          # 插件国际化
├── tests/                         # export-ignore
└── tools/                         # export-ignore
```

### 17.2 提交规范

- Conventional Commits：`feat(core):` / `fix(auth):` / `docs:` / `refactor:` / `test:` / `chore:`
- 一个提交对应一个可验证的单元（"绑定链路可跑"、"poll 监控可跑"），不提交半成品状态。
- 提交前自检：`python -m compileall` 通过、没有把真实手机号/token/序列号写进任何文件。

### 17.3 分支与标签

| 用途 | 命名 |
|---|---|
| 主干 | `main`（始终可用） |
| 里程碑开发 | `feat/m1-skeleton`、`feat/m2-bind` … 验收后 `--no-ff` 合回 `main` |
| 里程碑快照 | tag `v0.1.0-m1`、`v0.1.0-m2` … 最终 `v0.1.0` |
| 二期烘干箱 | `feat/m6-dryer-interface`（接口）与将来 `feat/dryer-ws`（实现）分开 |

### 17.4 `.gitignore` 底线

绝不入库：`account.json`、`subscribers.json`、`rules.json`、`snapshot.json`、`event_state.json`、
`events.jsonl`、任何 `data/plugin_data/` 下的运行期数据、`config.json`、`token.txt`、`*.crt`、`*.key`、
`__pycache__/`、`.venv/`。
`metadata.yaml` / `_conf_schema.json` / `data/protocol_profiles/*.json` **要**入库（它们是插件的一部分）。

### 17.5 与 AstrBot 的联调方式

```powershell
# 开发时用符号链接或直接拷贝到宿主插件目录，改完 reload 插件即可
git clone https://github.com/zsy1023/banbu-asterboot.git
# 需要试错时另开分支，别在 main 上堆未验证的实验代码
```

### 17.6 提交即推送（实时同步到远端）

本仓库装了 `post-commit` 钩子：**每次 commit 完立即 `git push origin <当前分支>`**，
推送失败只打印提示、不影响提交本身（离线也能正常提交，事后补推）。

```powershell
powershell -ExecutionPolicy Bypass -File tools\install-hooks.ps1            # 安装
powershell -ExecutionPolicy Bypass -File tools\install-hooks.ps1 -Uninstall # 卸载
$env:BAMBU_NO_AUTOPUSH=1     # 单次提交跳过自动推送（例如临时 WIP）
```

- 钩子只在本地 `.git/hooks/` 生效，不随仓库分发，换机器需重跑安装脚本。
- 推送依赖仓库本地代理配置：`git config http.proxy http://127.0.0.1:7897`（不需要就 `--unset`）。
- 钩子脚本保持 **纯 ASCII**：Windows PowerShell 5.1 会把无 BOM 的 UTF-8 当 ANSI 读，
  非 ASCII 字面量会直接语法错误。

---

## 18. 实现状态

### 18.1 已实现（本地 148 项测试全绿）

| 里程碑 | 内容 | 验证方式 |
|---|---|---|
| M1 骨架 | `metadata.yaml`、`_conf_schema.json`（25 项）、协议档案 `a1_v1`、`store`（原子写/轮转）、`compat`（字段映射/漂移/端点表） | `test_core.TestStore` / `TestCompat` |
| M2 绑定 | AES-256-GCM + 机器盐、发码/登录/换 uid/列举设备、`/banbu bind\|code\|sn\|devices\|unbind` | `TestCredentials` + 冒烟测试 `test_bind_flow_encrypts_token`（断言明文不落盘） |
| M3 监控 | MQTT poll + stream（增量深合并）、`monitor` 自动降级/退避/静默判离线、`events` 检测与去重、13 种规则 | `TestEventDetect` / `TestEventStateDedup` / `TestRules` |
| M4 直推 | `router` 三级优先级 + 总闸、`coalescer` 合并窗口、`render` 模板、`push` 订阅者/安静时段、8 个钩子 | `TestRouter` / `TestCoalescer` / `TestRender` / `TestQuietHours` |
| M5 LLM | `prompts` 防编造、`bridge` 超时/预算/降级、9 个 llm_tool、自然语言问答与规则解析 | 冒烟测试 `test_llm_merged_into_single_message`、`test_critical_event_reaches_push_even_if_llm_slow` |
| M6 二期接口 | `DeviceAdapter` + `PollSource`/`PushSource`、烘干箱骨架（`available=False`） | `test_command_group_registered`、`/banbu device list` |
| M7 打磨 | `/banbu protocol` 漂移表、`/banbu route`、`test-event`、事件流水、README | 冒烟测试全量 |

### 18.2 还没做 / 需要在真机验证

- **真机联调**：本机没有 AstrBot 与打印机，MQTT 实际收发、云端发码/登录、平台主动消息
  三者属于"必须在宿主里实测"的部分；代码层已按协议实现并把失败路径都做了可读降级。
- **烘干箱**：适配器骨架 + 配置位就绪，`available=False`；按 `dryer_esp32.py` 里的 TODO 清单填实现即可，不需改 `core`。
- **周期摘要**：保留了 `task_summary` / `periodic_digest` 事件类型与 `llm_route` 位，
  自动定时生成尚未接线（当前用 `/banbu digest` 手动触发），避免默认就产生周期性模型调用。
- **多打印机**：数据模型带 `device` 字段，注册表支持多适配器；当前只实例化单台 A1。

---

## 19. AstrBot 插件规范合规

依据（均为官方一手材料）：`docs.astrbot.app/dev/star/plugin-new.html`、`plugin-publish.html`、
`plugin-i18n.html`、插件市场 JSON 规范 `2026-06-27`，以及宿主加载器源码
`astrbot/core/star/updater.py`（`PLUGIN_METADATA_REQUIRED_FIELDS`、`inspect_plugin_directory`）
与 `astrbot/core/star/star_manager.py`（`_get_modules`、`_load_plugin_metadata`、`_load_plugin_i18n`）。

下面每条都有对应的自动化断言（`tests/test_packaging.py`），提交前跑一次就知道有没有违规。

| 规范要求 | 我们的做法 | 断言 |
|---|---|---|
| 包根必须有 `main.py` 或 `<dirname>.py` | 仓库根平铺 `main.py` | `test_required_files_at_repo_root`、`test_no_extra_package_level_nesting` |
| 插件类必须继承 `Star` | `class BambuA1Plugin(Star)` | `test_main_defines_star_subclass` |
| `metadata.yaml` 必需字段 `name/desc/version/author`，且为非空字符串 | 全部齐备 | `test_required_fields_non_empty_strings` |
| `metadata.yaml` ≤ 1MB | 1.8KB | `test_metadata_size_limit` |
| `version` 语义化版本、**不带 `v` 前缀**；市场记录必须与之一致 | `0.1.0`（早期写成 `v0.1.0`，已修正） | `test_version_is_semver_without_v_prefix` |
| `repo` 必须是 `https://github.com/{owner}/{repo}`（非 SSH/非子目录/非 release） | `https://github.com/zsy1023/banbu-asterboot`（早期指向不存在的 `astrbot_plugin_bambu_a1`，已修正） | `test_repo_is_github_repo_url`、`test_repo_matches_current_git_remote` |
| `author` + `name` 构成 `plugin_id`，`name` 全小写、`astrbot_plugin_` 前缀、无空格与斜杠 | `zsy1023/astrbot_plugin_bambu_a1` | `test_name_follows_plugin_naming` |
| `astrbot_version` 必须是合法 PEP 440 表达式（非法会阻止加载） | `">=4.9.2"` | `test_astrbot_version_is_valid_specifier` |
| `support_platforms` 只能填 `ADAPTER_NAME_2_TYPE` 的 key | 9 个已验证 key（早期多写了一个 `webchat`，已删） | `test_support_platforms_are_known_keys` |
| 打包 zip ≤ 16MB，且排除 `.git` / `__pycache__` / `node_modules` / 开发文件 | 全仓库约 0.2MB；`.gitignore` + `.gitattributes export-ignore` 排除 `tests/`、`tools/`、`ARCHITECTURE.md` | `test_package_under_market_limit`、`test_gitignore_keeps_runtime_data_out`、`test_gitattributes_excludes_dev_files_from_archive` |
| `_conf_schema.json` 必须存在，配置项要有 `type` | 23 项，含 `secret: true` 的密钥字段 | `test_config_schema_is_valid_json` |
| 国际化固定放在 `.astrbot-plugin/i18n/<locale>.json`，含 `metadata` / `config` / `views` 段 | `zh-CN.json` + `en-US.json`，覆盖全部配置项 | `test_i18n_files_in_expected_place`、`test_i18n_config_keys_exist_in_schema` |
| Logo 建议 1:1、256×256 | `logo.png`：32×32 像素风手绘后最近邻放大 8 倍，`tools/make_logo.py` 可复现/重画 | — |
| 建议提供 `README`，市场页直接展示 | `README.md` 含安装/绑定/命令/配置/自检 | — |

### 19.1 本次按规范做的三处改动

1. **目录重构**：`astrbot_plugin_bambu_a1/` 下的内容全部上移到仓库根。
   原因：宿主的 `_get_modules()` 只在"被克隆的目录根"里找 `main.py`，旧布局会导致
   "从仓库安装"装出一个空插件（日志 `has neither main.py nor ...`）。
2. **`metadata.yaml` 修正**：`version` 去掉 `v`；`repo` 指向真实的 `banbu-asterboot`；
   补 `tags` / `social_link`；`desc` 改为多行 Markdown（市场页会渲染）。
3. **`support_platforms` 去掉 `webchat`**：它不在官方允许的适配器 key 列表里，
   留着只会在 WebUI 里显示成未知平台。实际能力不受影响。

### 19.2 打包实测

`git archive --format=zip HEAD`（等价于市场下载的仓库 zip，只少了 GitHub 的顶层目录前缀）：

| 指标 | 结果 |
|---|---|
| 体积 | 约 122 KB（上限 16 MB） |
| 文件数 | 42 |
| 包根内容 | `metadata.yaml`、`main.py`、`_conf_schema.json`、`requirements.txt`、`bambu_a1/`、`.astrbot-plugin/`、`README.md`、`CHANGELOG.md`、`ARCHITECTURE.md`、`LICENSE`、`logo.png`、`banner.png` |
| 已排除 | `tests/`、`tools/`、`.gitignore`（`.gitattributes` 的 `export-ignore` 生效） |

### 19.3 发布前仍需人工确认

- `logo.png` 是 `tools/make_logo.py` 生成的 32×32 像素风图标（打印机 + 料盘 + 竹子 + 正在成型的层），
  想改配色或构图直接编辑脚本里的 `PALETTE` / `draw()`，再跑 `python tools/make_logo.py` 即可；
  `--preview` 会先打印 ASCII 网格方便确认构图。
- 市场发布需要在 AstrBot 插件发布页注册 AstrBot Cloud 账号后提交本仓库 URL；
  `plugin_id` 将是 `zsy1023/astrbot_plugin_bambu_a1`。
- 声明 `astrbot_version: ">=4.9.2"` 是硬门槛，低于该版本的宿主会拒绝加载（可在 WebUI 强制忽略）。
