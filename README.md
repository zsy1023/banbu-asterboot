# banbu-asterboot · 拓竹 A1 × AstrBot

在 [AstrBot](https://docs.astrbot.app/) 里绑定拓竹（Bambu Lab）账号，后台盯着打印机的 MQTT 状态，
**打印开始 / 暂停 / 断料 / 完成 / 失败 / 离线时主动推消息**；也能用自然语言问"现在打得怎么样"。

- 设计文档：[`ARCHITECTURE.md`](ARCHITECTURE.md)（分层、LLM 分界线、协议兼容层、二期接口）
- 插件本体：[`astrbot_plugin_bambu_a1/`](astrbot_plugin_bambu_a1)（含 `README.md` 使用说明）

## 这个仓库里有什么

| 路径 | 说明 |
|---|---|
| `astrbot_plugin_bambu_a1/` | 插件目录，整体拷进 `AstrBot/data/plugins/` 即可用 |
| `ARCHITECTURE.md` | 架构与取舍：为什么这样划分、哪些消息不过模型、字段怎么不写死 |
| `tools/` | 开发脚本（git 提交后自动推送的钩子安装器） |

## 快速开始

```powershell
git clone https://github.com/zsy1023/banbu-asterboot.git
Copy-Item -Recurse banbu-asterboot\astrbot_plugin_bambu_a1 <AstrBot>\data\plugins\
# 在 AstrBot 里重载插件，然后：
#   /banbu bind 13800001111   → 收短信
#   /banbu code 123456        → 完成登录（token 加密存本地）
#   /banbu sn <打印机序列号>   → 开始监控
```

完整命令表见 [`astrbot_plugin_bambu_a1/README.md`](astrbot_plugin_bambu_a1/README.md)。

## 两条核心设计

1. **哪些消息不过模型**：状态跃迁、硬阈值、用户自设规则、系统自检 → 模板直推（毫秒级、零幻觉）；
   完成复盘 / 失败诊断 / 小结 / 自然语言问答 → 过模型。分界线是配置项 `llm_route`，WebUI 可改。
2. **告警永不丢**：模型补写与事实合并成**一条**消息；模型超时或超预算就只发事实块。

## 自检

```powershell
cd astrbot_plugin_bambu_a1
python tests/run_tests.py     # 97 项，不联网、不需要 AstrBot
```

## 开发约定

每次提交会自动推送到本仓库（`post-commit` 钩子）。安装/卸载：

```powershell
powershell -ExecutionPolicy Bypass -File tools\install-hooks.ps1
powershell -ExecutionPolicy Bypass -File tools\install-hooks.ps1 -Uninstall
$env:BAMBU_NO_AUTOPUSH=1   # 单次提交跳过推送
```

推送依赖仓库本地的代理配置（如需要）：`git config http.proxy http://127.0.0.1:7897`。
