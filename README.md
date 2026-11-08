# 运行信息 · 真实读数 (`astrbot_plugin_runtime_info`)

让 AI 在**被问到**「你用的什么模型」时，查到**真实**的当前型号、累计 token 用量与真实缓存命中率；并提供一个 miuix 风格的 Web 仪表盘，随时查看运行数据。

- 按需调用：`get_runtime_info` 是 `llm_tool`，**只有模型被问到型号时才查**，平时不往上下文塞东西。
- 只读不改：只读 `cost_control` 的 `supplement.db` 与主库 `provider_stats`，全程 `mode=ro` + `query_only`，**不碰 `system_prompt`**，不破坏提示词缓存前缀稳定性。
- 真实口径：型号取自 `context.get_using_provider_async().meta()`；命中率复刻 `cost_control` 权威算法 `max(cache_read, token_input_cached)`。

---

## 功能特性

- **`get_runtime_info` 工具**：返回当前供应商 / 型号、累计 token、真实缓存命中率、最近一次命中率。AI 被问型号时调用，答完即止，不主动补充没人问的数据。
- **miuix 风格 Web 仪表盘**（插件页 / 独立访问同源）：
  - 蓝色 hero 卡：当前型号 + 最近一次命中率圆环
  - 四格 Token（缓存读 / 非缓存输入 / 缓存创建 / 输出）
  - 命中率滑块、按模型排行、按会话排行
  - 每日 token 堆叠柱状图、最近请求列表
  - 深浅主题、60s 自动刷新
- **3 个只读 Web API**：`/stats`、`/page`、`/health`（均需已登录 WebUI）。

---

## 安装

### 方式一：插件市场（推荐）
前往 [AstrBot Cloud 发布页](https://cloud.astrbot.app/) 提交本仓库，审核通过后即可在 AstrBot 仪表盘「插件」中一键安装。本仓库 `metadata.yaml` 已按市场格式补全。

### 方式二：从 GitHub 安装
AstrBot 仪表盘 → 插件 → 从 GitHub 安装，填入：
```
https://github.com/Gpan666/astrbot_plugin_runtime_info
```

### 方式三：手动 zip 自装
把插件目录打成 zip（顶层目录名 = `astrbot_plugin_runtime_info`），在 AstrBot 插件页上传安装。详见仓库内 `使用说明.txt`。

---

## 配置项

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `default_window` | 工具与 Web 页未指定时的统计窗口：`24h` / `today` / `weekly` / `all` | `24h` |
| `recent_limit` | Web「最近请求」显示条数（仅展示，不影响 token 总量口径） | `12` |
| `main_db_path` | 主库 `data_v4.db` 绝对路径，留空自动探测；仅补充库缺失时兜底 | 空 |
| `enable_page_sync` | 启动时把 `web/index.html` 同步到 `pages/dashboard/index.html` | `true` |

---

## AI 自我认知（worldbook 配合）

单独装这个插件，AI 也只会在**被调用工具**时才知道型号。要让你问「你是什么模型」时它自然去查，建议在世界书加一条**常用条目（common）**：

- 名称：`型号问答·读数`（≤10 字）
- 优先级：`1`
- 正则触发（1 条）：
  ```
  (?i)(什么|啥|哪|用|跑).{0,6}模型|模型.{0,6}(吗|啥|哪|叫|版本)
  ```
- 提示词（7 行）：
  ```
  【型号问答 · 按需生效】
  只有当当前这条消息确实在问你「用的是什么模型」时，才执行下面规则；普通闲聊请忽略本段，继续正常聊天。
  回答前先调用 get_runtime_info，按工具返回的真实型号作答（说出型号名即可，必要时带上供应商）。
  型号由后台配置、会随时切换，每次被问都要重新查，不要复用上次的答案，也不要凭印象猜。
  不要照搬聊天里别人提到的型号，也不要编参数量、版本或公司名。
  工具不可用或读不到就直说「现在读不到」，不编名字。
  答完就回到正常聊天，不要主动补充 token、缓存、成本这些没人问的数据；如果对方其实在讨论别人的模型，不要插话报自己的型号。
  ```

> 正则故意不加光秃秃的「模型」：worldbook 注入是往 `system_prompt` 追加，触发越频繁 prompt cache 越容易抖；是否「在问我」交给上面提示词第一行门卫判定即可。

---

## 数据源与权限

- 补充库：`<astrbot_data>/plugin_data/astrbot_plugin_cost_control/supplement.db`（`cost_supplements` 表，含 `cache_read` / `cache_creation` / `provider_model` 等）。
- 主库：`data_v4.db` → `provider_stats`（无 cache 细分，仅兜底）。
- 只读访问：`sqlite3` 以 `file:...?mode=ro` + `PRAGMA query_only=ON` 打开，**任何写操作都不会发生**。
- 不注入 `system_prompt`：型号/读数只在工具被调用时返回，不做每轮常驻，避免破坏提示词缓存。

---

## 注意事项

- 用量在 `on_llm_response` 才入库，工具返回的是「截至上一次已完成请求」，**不含本轮**——文案已注明，不要说成「这次用了多少」。
- 命中率口径严格复刻 `cost_control`：分母 = 缓存读 + 非缓存输入 + 缓存创建；只看 `cache_read` 会低估，本插件用 `max(cache_read, token_input_cached)`。
- 插件纯标准库实现（`sqlite3` / `re` / `math`），无第三方依赖。

---

## 许可证

本插件供个人使用与二次分发；如需指定许可证，请在仓库根目录补充 `LICENSE`。
