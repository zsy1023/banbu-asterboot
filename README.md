# 拓竹 A1 状态助手

<p align="center">
  <img src="./banner.png" alt="Banner">
</p>

<p align="center">
  <strong>打印完成、断料、异常 —— 第一时间推到你的聊天窗口</strong>
</p>

<p align="center">
  <a href="https://github.com/zsy1023/banbu-asterboot">
    <img src="https://img.shields.io/badge/AstrBot-Plugin-blue?style=flat-square" alt="AstrBot">
  </a>
  <img src="https://img.shields.io/badge/AstrBot-%3E%3D4.9.2-lightgrey?style=flat-square" alt="AstrBot Version">
  <img src="https://img.shields.io/badge/version-0.1.9-green?style=flat-square" alt="Version">
  <a href="./LICENSE">
    <img src="https://img.shields.io/badge/License-GPL--3.0-green?style=flat-square" alt="License">
  </a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Protocol-MQTT%208883-orange?style=flat-square" alt="MQTT">
  <img src="https://img.shields.io/badge/Platform-Windows%7CLinux%7CMacOS-lightgrey?style=flat-square" alt="Platform">
  <a href="https://github.com/zsy1023/banbu-asterboot/pulls">
    <img src="https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square" alt="PRs Welcome">
  </a>
  <a href="https://github.com/zsy1023">
    <img src="https://img.shields.io/badge/Author-zsy1023-orange?style=flat-square" alt="Author">
  </a>
</p>

> 把拓竹（Bambu Lab）云端的 MQTT 状态变成 AstrBot 的主动消息。
> 关键告警**不经过大模型**（毫秒级、零幻觉），只有复盘、诊断、小结和问答才用模型；
> 模型超时或超预算就自动退回模板 —— 告警永不丢失。

---

<details>
<summary><h2>✨ 功能一览</h2></summary>

### 1. 三步绑定拓竹账号
`/banbu bind <手机号>` → 收短信 → `/banbu code <验证码>` → `/banbu sn <序列号>`。
token 与手机号经 **AES-256-GCM** 加密落盘，密钥掺入机器指纹。

> 例：绑定成功后，你当前这个会话就自动成为推送目标，不用再配一遍。

### 2. 双通道状态监控
MQTT 长连接优先（秒级），连接不稳时自动降级为轮询（默认 30 秒一轮）；
失败按指数退避重试，上限 10 分钟，避免被云端限流。

> 例：`monitor_mode = auto` 时网络抖动会自动换成轮询，恢复后再切回长连接。

### 3. 关键事件主动推送
打印开始、暂停、恢复、完成、失败、HMS 故障码、耗材用尽（断料）、离线与恢复。

消息是**卡片式纯文字排版**，没有 emoji：标题行 `几何字形 + 设备 · 事件`，下面一条 `━` 制表线，
再往下是 `标签　值` 细节行和操作提示。

> 例：半夜断料会收到
> ```
> ◆ 拓竹 A1 · 耗材用尽（断料）
> ━━━━━━━━━━━━━━━━━━━━
> 任务　多功能PCB测试工作台_万向蛇…
> 进度　47%
>
> 换好料后发送 /banbu resume 继续
> ```

`/banbu status` 的状态卡带一张 4 行小打印机像素图和两条温度小进度条：

```
拓竹 A1 · 打印中
━━━━━━━━━━━━━━━━━━━━
██████
████████
▄▄▄▄▄▄▄▄▄▄

任务　多功能PCB测试工作台_万向蛇管机械臂_多探针…
进度　▏█████░░░░░░░▕ 40%
层次　36/91
时间　已 1 小时 30 分 · 剩 2 小时 40 分
喷头　▰▰▰▰▰▰▰▰ 215/215°C
热床　▰▰▰▰▱▱▱▱ 30/60°C
腔体　32°C
耗材　PLA 78%
设备　灯 开 · 信号 -52dBm · 12 秒前更新
```

细节都是自适应的：

- **像素图只画热床和上面的件**，层数跟着进度增长（0% 只有热床，1–33% 一层，
  34–67% 两层，68% 以上三层）。不画机身，所以跟 bedslinger / CoreXY 都无关；
  也不用空格补白，避免方块字符在中文客户端被当全角渲染把图弄歪
- **进度条**带端点 `▏…▕`，像仪表刻度
- **温度条**按 `当前/目标` 填充：升温半格、到温满格、超温按满格（clamp）；
  腔体没有目标温度就只给数值，不画空条
- **空闲/完成且进度为 0** 时不显示进度行（避免"进度 0%"这种废话），100% 会保留
- **底部提示**按状态给下一步：暂停 → `/banbu resume`，失败 → `/banbu protocol`，
  离线 → 检查网络；打印中不打扰

### 4. 13 种提醒规则
层数到达、进度百分比、剩余时间、已打印时长、喷头/热床/腔温上下限、温度区间、
HMS 故障码、AMS 余料、离线分钟数。命令或自然语言都能加。

> 例：`/banbu remind add layer_reached 50` —— 打到第 50 层提醒你；
> `/banbu remind add temp_in_range 30-40 --llm` —— 温度进 30~40°C 时让模型润色一句。

### 5. LLM 分界线自己定
每个事件类型都能单独设成 `direct`（不经过模型）/ `llm`（模型补写）/ `both`，
在 WebUI 或 `/banbu route set print_failed direct` 里改，**即时生效**。

> 例：觉得失败告警不需要解释，就设成 `direct`，从此只发错误码和进度。

### 6. 事实与解读合并成同一条
模型补写的内容和结构化事实在合并窗口内合成**一条**消息（分隔线区分）；
模型超窗、超预算或供应商挂了，就只发事实块。

> 例：打印完成时收到
> ```
> ✓ 拓竹 A1 · 打印完成
> ━━━━━━━━━━━━━━━━━━━━
> 任务　多功能PCB测试工作台
> 耗时　1 小时 42 分
>
> ━━━━━━━━━━━━━━━━━━━━
>
> 这次层纹比上次好，收尾建议顺手清一下喷嘴。
> ```

### 7. 自然语言交互
9 个 LLM 工具：查状态、查历史、列/加/删规则、切 LLM 档位、改分界线、控制打印机、绑定引导。

> 例：直接问"现在打得怎么样？还要多久？"，或说"打到一半提醒我"，模型会调对应工具。

### 8. 安静时段（可关）
开关默认关闭；开启后时段内只放行 `critical`，完成与里程碑静默累计。

> 例：`/banbu quiet 23:00-07:30` —— 半夜只推失败和断料，不推"进度 50%"。

### 9. 协议漂移诊断
字段映射、端点、状态码全部外置在 `bambu_a1/protocol_profiles/a1_v1.json`。
云端改了字段不用改代码，`/banbu protocol` 会告诉你哪几个字段开始飘。

> 例：拓竹把 `mc_percent` 改名后，`/banbu protocol` 会列出「档案声明但没读到的字段：percent×12」。

### 10. 安全子集远程控制
机箱灯、暂停、继续、停止。`pause` / `stop` / `unbind` 需要二次确认，不开放控温与任意 G-code。

> 例：`/banbu stop confirm` —— 少了 `confirm` 只返回一句提醒，不会真的停掉打印。

</details>

---

<details>
<summary><h2>📦 安装</h2></summary>

**方法一：从插件市场安装（推荐）**

AstrBot WebUI → 插件市场 → 搜索 **拓竹** 或 `astrbot_plugin_bambu_a1` → 安装。

**方法二：从仓库安装**

WebUI → 插件 → 安装 → 填仓库地址：

```text
https://github.com/zsy1023/banbu-asterboot
```

**方法三：手动安装**

```bash
cd AstrBot/data/plugins/
git clone https://github.com/zsy1023/banbu-asterboot.git astrbot_plugin_bambu_a1
```

然后在 WebUI 里重载插件。

> **依赖**：`paho-mqtt`、`requests`、`cryptography` 由插件管理器按 `requirements.txt` 自动安装。
> 建议额外装 `curl_cffi`（`pip install curl_cffi`）：拓竹云端有 Cloudflare，不装也能用，但发码偶发被拦。

</details>

---

<details>
<summary><h2>🚀 快速开始</h2></summary>

```bash
/banbu bind 13800001111      # 发送短信验证码（中国区账号）
/banbu code 123456           # 完成登录，token 加密存本地
/banbu sn 01P00A12345678     # 绑定打印机；不知道序列号可先 /banbu devices
```

绑定完成后：

- 当前会话自动成为推送目标（"谁绑定推给谁"）
- 想在别的会话也收，在那个会话里发 `/banbu subscribe`
- 想先确认链路通不通，发 `/banbu test-event print_finished` 造一条事件

</details>

---

<details>
<summary><h2>💬 命令一览</h2></summary>

| 命令 | 说明 |
|:---|:---|
| `/banbu bind <手机号>` | 发送短信验证码 |
| `/banbu code <验证码>` | 完成登录并加密保存 token |
| `/banbu sn <序列号>` | 绑定打印机并建立状态基线 |
| `/banbu devices` | 尝试从云端列举账号下已绑定设备 |
| `/banbu unbind confirm` | 清除凭据与推送目标 |
| `/banbu status` | 状态卡（进度/层数/温度/剩余/耗材） |
| `/banbu monitor [on\|off\|auto\|poll\|stream\|status]` | 后台监控开关与通道 |
| `/banbu remind add <类型> <值> [--llm]` | 添加提醒规则 |
| `/banbu remind list` / `del <id>` / `clear` | 规则管理 |
| `/banbu quiet <23:00-07:30\|off>` | 安静时段 |
| `/banbu milestones <25,50,75\|off>` | 进度里程碑（**默认关闭**，你不要求就不推） |
| `/banbu subscribe` / `unsubscribe` | 本会话加入/退出推送 |
| `/banbu light [on\|off]` | 机箱灯 |
| `/banbu resume` | 继续打印 |
| `/banbu pause confirm` / `stop confirm` | 暂停 / 停止（需确认） |
| `/banbu llm off\|summary\|full` | LLM 总闸 |
| `/banbu route [list\|set <事件> <通道>]` | 查看/修改 LLM 分界线 |
| `/banbu digest` | 最近打印小结（有模型时由模型组织措辞） |
| `/banbu protocol` | 协议档案与漂移诊断 |
| `/banbu test-event <事件类型>` | 造一条事件验证推送链路 |
| `/banbu device list` | 已注册适配器（烘干箱为二期，显示 ⏳） |

也可以完全用自然语言：问状态、设提醒、切档位、调分界线、开关灯。

</details>

---

<details>
<summary><h2>⚙️ 配置说明</h2></summary>

安装后在 AstrBot WebUI → 插件管理 → **拓竹 A1 状态助手** → 配置 中设置（共 25 项）：

| 配置项 | 类型 | 默认值 | 说明 |
|:---|:---:|:---:|:---|
| **插件总开关** | 布尔 | 开 | 关掉后不启动后台监控 |
| **拓竹账号区域** | 下拉 | 中国区 | 收码区域与登录区域必须一致，否则验证码被判为不存在 |
| **协议档案** | 下拉 | `a1_v1` | 云端改字段时优先换档案，不用改代码 |
| **协议档案覆盖** | 字典 | 空 | 深合并到档案，临时给字段改名打补丁 |
| **打印机序列号** | 字符串 | 空 | 也可以 `/banbu sn` 绑定 |
| **凭据主密钥** | 密文 | 空 | 留空则首次绑定自动生成；改了需要重新绑定 |
| **监控通道** | 下拉 | 自动 | 自动 / 长连接 / 轮询 |
| **轮询间隔** | 整数 | 30 | 秒 |
| **无报文判离线** | 整数 | 90 | 秒 |
| **启动时自动监控** | 布尔 | 开 | 插件加载即开始 |
| **额外推送目标** | 列表 | 空 | 一般不用填，默认推给绑定会话 |
| **启用安静时段** | 布尔 | 关 | 关掉则安静时段完全不生效 |
| **安静时段区间** | 字符串 | `23:00-07:30` | 期间只推 critical |
| **进度里程碑** | 列表 | `[]`（不推） | 留空即不主动推进度；要收就 `/banbu milestones 25,50,75,90` |
| **合并窗口** | 整数 | 3 | 秒；0 表示逐条即时发 |
| **critical 合并窗口** | 浮点 | 0.8 | 秒；告警用更短的窗口 |
| **LLM 总闸** | 下拉 | 仅复盘与问答 | `off` / `summary_only` / `full` |
| **LLM 分界线** | 字典 | 空（用内置默认） | 事件 → `direct` / `llm` / `both` |
| **LLM 供应商** | 下拉 | 空 | 留空用会话默认 |
| **LLM 超时** | 整数 | 20 | 秒；超时退回模板 |
| **LLM 每小时上限** | 整数 | 12 | 超出后本次走模板，告警照发 |
| **MQTT 超时** | 整数 | 15 | 秒 |
| **MQTT 重试次数** | 整数 | 2 | 次 |
| **日志级别** | 下拉 | INFO | |
| **烘干箱（二期）** | 对象 | 关闭 | `enabled` / `require_confirm` / `ws_url`，本期仅占位 |

### 配置示例

**场景一：只想在出问题时被打扰**

```text
LLM 总闸：关闭（全部模板直推）
进度里程碑：留空（默认）
安静时段：开启 23:00-07:30
```
> 只在开始/暂停/完成/失败这类节点收到消息，半夜只推失败和断料。
> 想额外要半程提醒就单独加一条规则：`/banbu remind add percent_reached 50`

**场景二：想要"复盘感"**

```text
LLM 总闸：完整
LLM 分界线：print_finished=llm, print_failed=llm, milestone=direct
LLM 每小时上限：20
```
> 完成和失败带模型的两句解释，进度里程碑仍然直推保持毫秒级。

**场景三：长连接不稳的网络**

```text
监控通道：轮询
轮询间隔：20
无报文判离线：120
```
> 放弃低延迟换稳定，每 20 秒主动拉一次。

</details>

---

<details>
<summary><h2>🧠 哪些消息不过模型</h2></summary>

| 直推（模板渲染，毫秒级） | 过模型（补写解释后合并） |
|:---|:---|
| 开始 / 暂停 / 恢复 / 完成 / 失败 | 完成复盘（耗时、层数、与近期对比） |
| HMS 故障码、耗材用尽、温度越限 | 失败与故障的排查建议 |
| 离线与恢复、进度里程碑 | 周期小结（`/banbu digest`） |
| 用户自设规则命中、所有命令回执 | 自然语言问答 |

**为什么这么分**：报警要快、数值不能编、用户设的规则应该可预期；
而"为什么失败、下一步查什么、这次比上次好还是差"才是模型能加分的地方。

**兜底**：模型这条路径永远不阻塞告警 —— 超时、超预算、供应商不可用都只发事实块。

</details>

---

<details>
<summary><h2>🔧 兼容性</h2></summary>

- **AstrBot 版本**：`>= 4.9.2`（用到插件数据目录 API 与 `add_llm_tools`，低于此版本会拒绝加载）
- **支持平台**：aiocqhttp、Telegram、QQ 官方机器人、企微、飞书、钉钉、Discord、Slack
- **额外依赖**：`paho-mqtt`、`requests`、`cryptography`、`curl_cffi`（后者是发码必需，插件管理器会自动安装）
- **设备**：拓竹 A1（单台）。烘干箱 `3D-dryer-for-esp32-c3` 为二期，当前只有适配器骨架

### 已知边界

- MQTT 端不校验证书（拓竹 broker 现状所迫），介意的话请自行加白名单/防火墙
- 换机器或清空 `secret_key` 需要重新 `/banbu bind`（密钥含机器指纹，这是有意的）
- 部分平台不支持主动消息，`/banbu test-event` 可最快验证

</details>

---

<details>
<summary><h2>📁 文件结构</h2></summary>

```text
banbu-asterboot/
├── main.py                    # 插件入口：命令 + LLM 工具 + 生命周期
├── metadata.yaml              # 插件元数据
├── _conf_schema.json          # WebUI 配置项定义（25 项）
├── requirements.txt           # 依赖声明
├── logo.png / banner.png      # 图标与顶部图（像素风，脚本生成）
├── README.md / CHANGELOG.md
├── LICENSE                    # GPL-3.0
├── .astrbot-plugin/i18n/      # 中英文案
├── bambu_a1/
│   ├── compat.py              # 协议档案：端点/字段映射/漂移统计
│   ├── credentials.py         # AES-256-GCM 凭据存储
│   ├── auth.py                # 发码 / 登录 / 换 uid
│   ├── mqtt.py                # 轮询 + 长连接双通道
│   ├── monitor.py             # 后台监控循环
│   ├── events.py / rules.py   # 事件检测与 13 种提醒规则
│   ├── router.py              # LLM 分界线判定
│   ├── coalescer.py           # 合并窗口
│   ├── render.py / push.py    # 文本渲染与投递
│   ├── bridge.py / prompts.py # LLM 封装与防编造提示词
│   ├── tools.py               # 自然语言工具实现
│   ├── adapters/              # 设备适配器（A1 已实现，烘干箱骨架）
│   └── protocol_profiles/     # 可热更新字段映射
└── tests/                     # 127 项本地测试（不随插件包分发）
```

</details>

---

<details>
<summary><h2>🧪 自检</h2></summary>

```bash
python tests/run_tests.py     # 127 项，不联网、不需要 AstrBot
```

覆盖：事件检测与去重、13 种规则、路由三级优先级、合并窗口（含模型超窗仍发事实）、
渲染、安静时段跨零点、AES-GCM 加解密、监控引擎完整打印生命周期、
用 stub 宿主跑通"绑定 → 事件 → 路由 → 合并 → 推送"，以及插件市场规范合规（元数据 / 体积 / i18n / 目录布局）。

架构与取舍见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

</details>

---

<details>
<summary><h2>📄 更新日志</h2></summary>

详见 [CHANGELOG.md](./CHANGELOG.md)

</details>

---

<details>
<summary><h2>👤 作者</h2></summary>

**zsy1023**

- 拓竹 A1 状态采集：[banbu-a1-Status-acquisition](https://github.com/zsy1023/banbu-a1-Status-acquisition)
- 3D 烘干箱固件：[3D-dryer-for-esp32-c3](https://github.com/zsy1023/3D-dryer-for-esp32-c3)

</details>

---

<details>
<summary><h2>📄 许可证</h2></summary>

GPL-3.0 —— 见 [LICENSE](./LICENSE)。

MQTT / 云端协议行为参考了 [banbu-a1-Status-acquisition](https://github.com/zsy1023/banbu-a1-Status-acquisition)，
插件代码为独立编写。

</details>
