# 拓竹 A1 状态助手 · astrbot_plugin_bambu_a1

在 [AstrBot](https://docs.astrbot.app/) 里绑定拓竹（Bambu Lab）账号，插件后台盯着打印机的 MQTT 状态，
**打印开始 / 暂停 / 断料 / 完成 / 失败 / 离线时主动推送消息**；也能用自然语言问"现在打得怎么样"。

- 架构与取舍：[`ARCHITECTURE.md`](ARCHITECTURE.md)
- 本仓库根目录**就是插件包根**（`metadata.yaml` + `main.py` 在根），符合 AstrBot 插件市场规范

## 安装

**方式一：插件市场**（推荐）
在 AstrBot WebUI → 插件市场搜索"拓竹"或 `astrbot_plugin_bambu_a1` 直接安装。

**方式二：从仓库安装**
WebUI → 插件 → 安装 → 填 `https://github.com/zsy1023/banbu-asterboot`

**方式三：手动**

```powershell
git clone https://github.com/zsy1023/banbu-asterboot.git <AstrBot>\data\plugins\astrbot_plugin_bambu_a1
# 然后在 WebUI 里重载插件
```

依赖 `paho-mqtt`、`requests`、`cryptography` 会由插件管理器按 `requirements.txt` 安装。
建议额外装 `curl_cffi`（`pip install curl_cffi`）：拓竹云端有 Cloudflare，不装也能用，但发码偶发被拦。

## 绑定（三步）

```
/banbu bind 13800001111      # 发送短信验证码
/banbu code 123456           # 完成登录，token 加密存本地
/banbu sn 01P00A12345678     # 绑定打印机（不知道序列号可先 /banbu devices）
```

绑定成功的那个会话会自动成为推送目标。手机号与 token **只加密落盘**（AES-256-GCM，
密钥由 `secret_key` + 机器指纹派生）——换机器或清空 `secret_key` 都需要重新绑定。

## 常用命令

| 命令 | 作用 |
|---|---|
| `/banbu status` | 状态卡（进度/层数/温度/剩余/耗材） |
| `/banbu monitor [on\|off\|auto\|status]` | 后台监控开关与通道 |
| `/banbu remind add layer_reached 50` | 打到第 50 层提醒 |
| `/banbu remind add temp_in_range 30-40 --llm` | 温度进区间提醒，命中时让模型润色 |
| `/banbu remind list` / `del <id>` / `clear` | 规则管理 |
| `/banbu route list` | 看哪些消息走模型、哪些直推 |
| `/banbu llm off\|summary\|full` | LLM 总闸（改完即时生效） |
| `/banbu quiet 23:00-07:30` | 安静时段（期间只推 critical） |
| `/banbu light on` / `resume` / `pause confirm` / `stop confirm` | 控制 |
| `/banbu protocol` | 协议档案与漂移诊断（云端改字段时先看这个） |
| `/banbu test-event print_finished` | 造一条事件，验证推送链路 |

直接跟机器人说"打到一半提醒我""为什么停了"也可以，模型会调用对应工具。

## 两条核心设计

1. **哪些消息不过模型**：状态跃迁、硬阈值、用户自设规则、系统自检 → 模板直推（毫秒级、零幻觉）；
   完成复盘 / 失败诊断 / 小结 / 自然语言问答 → 过模型。分界线是配置项 `llm_route`，WebUI 逐个事件可改，
   `/banbu route set print_failed direct` 也能改。
2. **告警永不丢**：模型补写与事实合并成**一条**消息；模型超时、超预算或供应商不可用时只发事实块。

## 配置

所有配置项都在 WebUI 的插件设置里（`_conf_schema.json` 共 23 项），常用的几项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `llm_mode` | `summary_only` | `off` 全直推 / `summary_only` 只做复盘与问答 / `full` 含诊断与摘要 |
| `llm_route` | 见架构文档 6.3 | 逐事件设置 `direct` / `llm` / `both` |
| `monitor_mode` | `auto` | 长连接优先，失败降级轮询 |
| `quiet_hours_enabled` | `false` | 安静时段总开关（关掉就完全不生效） |
| `merge_window_s` | `3` | 合并窗口；`0` 表示逐条即时发 |
| `progress_milestones` | `[25,50,75,90]` | 进度里程碑 |

## 自检

```powershell
python tests/run_tests.py     # 115 项，不联网、不需要 AstrBot
```

## 已知边界

- 只支持单台 A1；多设备字段已预留（`Snapshot.device`）。
- 烘干箱（[3D-dryer-for-esp32-c3](https://github.com/zsy1023/3D-dryer-for-esp32-c3)）二期接入，
  当前只有适配器骨架，`/banbu device list` 会显示 ⏳。
- MQTT 端不校验证书（拓竹 broker 现状所迫），介意的话请自行加白名单/防火墙。

## 开发约定

每次提交会自动推送到本仓库（`post-commit` 钩子）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\install-hooks.ps1            # 安装
powershell -ExecutionPolicy Bypass -File tools\install-hooks.ps1 -Uninstall # 卸载
$env:BAMBU_NO_AUTOPUSH=1   # 单次提交跳过推送
```

推送依赖仓库本地的代理配置（如需要）：`git config http.proxy http://127.0.0.1:7897`。

## 许可

GPL-3.0（与 [banbu-a1-Status-acquisition](https://github.com/zsy1023/banbu-a1-Status-acquisition)、
[3D-dryer-for-esp32-c3](https://github.com/zsy1023/3D-dryer-for-esp32-c3) 保持一致）。
MQTT / 云端协议行为参考了前者的实现，代码本身为全新编写。
