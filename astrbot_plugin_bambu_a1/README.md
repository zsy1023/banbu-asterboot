# astrbot_plugin_bambu_a1 · 拓竹 A1 状态助手

在 AstrBot 里绑定拓竹账号，插件后台盯着打印机的 MQTT 状态，
**打印开始/暂停/断料/完成/失败/离线时主动推给你**；也能用自然语言问它"现在打得怎么样"。

架构与设计取舍见仓库根目录的 [`ARCHITECTURE.md`](../ARCHITECTURE.md)。

## 安装

1. 把 `astrbot_plugin_bambu_a1/` 整个目录放进 `AstrBot/data/plugins/`
2. 在 /plugins 里重载插件（依赖 `paho-mqtt`、`requests`、`cryptography` 会由插件管理器安装）
3. 建议额外装 `curl_cffi`（`pip install curl_cffi`）：拓竹云端有 Cloudflare，
   不装也能用，但发码偶发被拦

## 绑定（三步）

```
/banbu bind 13800001111      # 发送短信验证码
/banbu code 123456           # 完成登录，token 加密存本地
/banbu sn 01P00A12345678     # 绑定打印机（不知道序列号可先 /banbu devices）
```

绑定成功的那个会话会自动成为推送目标。手机号与 token **只加密落盘**，
密钥由 `secret_key` + 机器指纹派生 —— 换机器或清空 `secret_key` 都需要重新绑定。

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

## 哪些消息不过模型？

默认 **状态跃迁 + 硬阈值 + 自定义规则 + 系统自检** 直推（毫秒级、零幻觉），
只有"完成复盘 / 失败诊断 / 周期小结 / 自然语言问答"才过模型，且模型超时或超预算时
**自动退回模板直推**，告警永不丢失。

改分界线不用改代码：WebUI 配置 `llm_route`，或 `/banbu route set print_failed direct`。

## 自检

```bash
cd astrbot_plugin_bambu_a1
python tests/run_tests.py     # 97 项，不联网、不需要 AstrBot
```

## 已知边界

- 只支持单台 A1；多设备字段已预留（`Snapshot.device`）。
- 烘干箱（`3D-dryer-for-esp32-c3`）二期接入，当前只有适配器骨架，
  `/banbu device list` 会显示 ⏳。
- MQTT 端不校验证书（拓竹 broker 现状所迫），介意的话请自行加白名单/防火墙。
