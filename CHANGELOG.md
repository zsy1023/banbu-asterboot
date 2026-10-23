# 更新日志

本项目的版本号与 `metadata.yaml` 的 `version` 保持一致（语义化版本，不带 `v` 前缀）。
每个版本对应的 git tag 为 `v<version>`。

## 0.1.4

**修复：绑定瞬间喷出四条假进度（25%/50%/75%/90%，还带"剩余 0 分钟"）**

- 现象：`/banbu sn` 成功后立刻收到「进度 25% / 50% / 75% / 90%，剩余 0 分钟」四条消息，
  而机器其实停在「已完成 100%」
- 根因：首轮快照只对"状态跃迁类"事件做了抑制，**进度里程碑没抑制**；
  且里程碑只比较 `prev.percent`（首轮按 0 算），没检查"是否正在打印"
- 修复：
  - `milestone` 需同时满足：`cur.state == "printing"`、`prev` 存在且 `prev.percent` 已知、非首轮
  - `hms_error` 同样加上首轮抑制（机器上已有的故障码不该被当成"刚发生"再报一次）
  - 渲染层不再输出"剩余 0 分钟"/"已 0 分钟"：`0` 与负值直接省略
  - 里程碑消息补上任务名，便于分辨是哪次打印
- 新增 5 项回归测试：首轮不报里程碑（传不传 first_poll 都不报）、暂停/完成时不报里程碑、
  prev 进度未知时不报、首轮不报 HMS、绑定时机器停在 100% 不产生任何事件
- `ARCHITECTURE` 5.4 补「首轮基线规则」对照表，把这次事故写成反面教材

## 0.1.3

**修复：收到 504 且短信没下发（curl_cffi 漏装）**

- 现象：`/banbu bind` 返回「验证码已发送」，但手机一直收不到短信；日志里 `HTTP 504`
- 根因：拓竹云端在 Cloudflare 后面。`requirements.txt` 里把 `curl_cffi` 写成了注释（可选），
  于是实际用的是裸 `requests`：请求挂到 Cloudflare 60s 超时返回 504，短信并未下发；
  而代码按参考项目语义"发出去就算成功"，把这个失败吞掉了
- 修复：
  - `requirements.txt` 把 `curl_cffi>=0.6.0` 变成**真正生效的依赖**（插件加载时自动安装）
  - `auth.send_sms` 与 `banbu-a1-Status-acquisition/auth.py` 的 `send_sms` 逐行对齐：
    记录完整请求 URL、响应码、响应体前 200 字，显式 `session.close()`
  - 新增分级提示：5xx 且未装 curl_cffi → 明确告知"短信大概率没下发 + 怎么装"；
    5xx 且已装 → "上游超时，通常仍会到达，等 1 分钟再重发"
  - 未装 curl_cffi 时在发码前就记一条 WARNING，不用等失败才发现
- 新增 `tests/test_auth.py`（14 项）：发码成功/504 两种分支文案、CSRF 头与端点断言、
  登录错误码 1/2 翻译、缺 accessToken、网络异常、uid 获取、两区端点差异
- 新增合规断言：`requirements.txt` 必须声明 `curl_cffi`（已生效行，不是注释）

## 0.1.2

**修复：一条日志把命令打挂（`Formatting field not found in record: 'plugin_tag'`）**

- 现象：`/banbu bind` 直接报错，堆栈落在 `bambu_a1/auth.py` 的 `logger.info(...)`
- 根因：各模块用 `logging.getLogger("astrbot.plugin.bambu_a1.*")` 自建 logger，
  名字落在宿主的 `astrbot.*` 命名空间，但不是通过 `LogManager.get_plugin_logger()`
  创建的，缺少 `_RecordEnricherFilter` 注入的 `plugin_tag`；
  记录冒泡到宿主 `astrbot` logger 上的 `LogQueueHandler` 时
  `self.format(record)` 抛 `ValueError`（该 handler 不走 `handleError`，异常直接冒出来）
- 修复：新增 `bambu_a1/logging_compat.py` 日志适配层
  - 插件 `__init__` 里 `bind_plugin_logger(self.logger)`，之后所有模块日志都走宿主插件 logger
    （WebUI 日志面板里带正确 `[插件名]` 标签，也能按插件调日志级别）
  - 未绑定前回退到 `bambu_a1` logger（刻意不在 `astrbot.*` 下）
  - 门面内部吞掉日志系统异常：**日志永远不能影响业务**
  - 模块名以 `[auth]` `[mqtt]` 这类前缀保留在消息里，排查体验不变
- 新增 `tests/test_logging.py`（9 项）：AST 检查禁止自建 `astrbot.*` logger、
  严格 formatter 复现宿主行为、绑定前后都能正常记录、handler 抛异常不打断业务
- 顺带移除 `main.py` 里未使用的 `astrbot.api.logger` 导入

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
