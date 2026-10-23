# 更新日志

本项目的版本号与 `metadata.yaml` 的 `version` 保持一致（语义化版本，不带 `v` 前缀）。
每个版本对应的 git tag 为 `v<version>`。

## 0.1.1

**按 AstrBot 插件市场规范调整（破坏性：安装方式）**

- 插件文件从 `astrbot_plugin_bambu_a1/` 子目录上移到仓库根
  —— 宿主的 `_get_modules()` 只在被克隆目录根找 `main.py`，
  旧布局会让"从仓库安装"装出一个空插件
- `metadata.yaml` 修正：`version` 去掉 `v` 前缀、`repo` 指向真实仓库、
  补 `tags` / `social_link`、`desc` 改为多行 Markdown
- `support_platforms` 移除 `webchat`（不在官方 `ADAPTER_NAME_2_TYPE` key 列表内）
- 新增 `.astrbot-plugin/i18n/{zh-CN,en-US}.json`（插件名与全部配置项文案）
- 新增 `LICENSE`（GPL-3.0）、`logo.png`（32×32 像素风）、`banner.png`（README 顶部图）
- 新增 `tests/test_packaging.py`：把市场规范做成 18 项可回归断言
- `.gitattributes` 增加 `export-ignore`：市场 zip 不带 `tests/`、`tools/`、`ARCHITECTURE.md`

## 0.1.0

**首个可用版本**

- 三步绑定：手机号发码 → 验证码登录 → 序列号绑定；token 与手机号 AES-256-GCM 加密落盘（含机器指纹）
- 监控：MQTT 轮询与长连接双通道，`auto` 模式自动降级与恢复，失败指数退避，静默超时判离线
- 事件：开始 / 暂停 / 恢复 / 完成 / 失败 / HMS 故障码 / 断料 / 离线与恢复 / 进度里程碑，持久化去重
- 规则：13 种提醒类型，支持 `--llm` 让模型润色
- 推送：`llm_route` 配置分界线（单规则 > 配置 > 默认），`llm_mode` 总闸，
  合并窗口把事实与模型补写合成一条，模型超窗只发事实
- LLM：超时 / 每小时预算 / 失败降级；9 个自然语言工具；防编造提示词
- 控制：灯光、暂停、继续、停止（危险操作需二次确认）
- 诊断：`/banbu protocol` 协议漂移表、`/banbu test-event` 链路自检
- 二期接口：`DeviceAdapter` + `PollSource`/`PushSource`，烘干箱骨架（`available=False`）
- 测试：118 项（核心逻辑 + stub 宿主冒烟 + 规范合规 + 文档链接检查），不联网、不需要 AstrBot
