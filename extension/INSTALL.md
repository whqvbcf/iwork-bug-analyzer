# 安装 iWork 证据采集扩展

此目录可直接作为 Chromium Manifest V3 解压扩展加载；不需要 npm、构建或下载依赖。建议 Chrome / Chromium 120 或更新版本。

1. 按仓库根目录 README 安装本地助手，并为下方固定扩展 ID 注册 Native Messaging 主机。
2. 打开 `chrome://extensions`，启用「开发者模式」，选择「加载已解压的扩展程序」，选中本 `extension/` 目录。
3. 确认扩展 ID 为 **`oblbdhfcbmklhaebkonhfdidglfhinac`**。`manifest.json` 的公开 `key` 固定了此 ID；保留该字段。目录移动后仍使用同一 ID。
4. 打开扩展的「设置」，点击「检查本地助手」。如果未连接，请先完成本地助手注册并重新加载扩展。主机名称为 `com.iwork.collector`，`allowed_origins` 必须包含 `chrome-extension://oblbdhfcbmklhaebkonhfdidglfhinac/`。
5. 打开已登录的问题详情页，点击扩展图标 →「采集当前 Bug」。扩展会自动展开详情、评论、附件和视频/日志区域，并在页面登录上下文读取 iWork 的详情、评论树和变更记录接口；不需要手动展开每个单号，也不需要逐单填写选择器。

浏览器下载先写入默认 Downloads 目录中的 `iwork-collector/<job-id>/<task-id>/<原始文件名>`；不要手动改名、移动下载，或在浏览器「另存为」中更改登记路径。助手验证路径后将证据整理到 `~/iwork_space/bug_<YYMMDD>_<Bug编号>/`，删除中转副本和工具空目录；启动时还会清理旧版遗留的工具空目录。旧 Bug 目录自动换序命名，保留原日期和任务进度。下载完成后即便关闭弹窗，扩展仍通过事件与 30 秒轮询同步；本地助手暂时不可用时，下载回执保存在本机等待重连。

升级到 0.1.8 后在 `chrome://extensions` 点击重新加载，再刷新工单页面并点击「重试 / 继续」。无扩展名的明确 `log_…` 标识会按文件检索，不再当作群名；旧任务重试时会修正此误识别。群消息附件先核验原消息资源，再按资源类型传输；fileId 使用分片下载。报告会显示评论作者、回复链接与评论原图。图片下载需要原工单标签页保持打开；可关闭扩展弹窗。

## 页面适配

默认选择器对应 `tests/fixtures/iwork-ticket.html`。生产 iWork 缺陷页如果没有这些 `data-*` 标记，扩展会自动从当前可见详情文本中识别缺陷描述、重现步骤、期望/实际结果、测试环境、视频/日志信息、日志文件名和“见：【群名】钉钉群”线索；通常不需要逐单填写 CSS 选择器。只有自动识别仍缺内容时，才进入「设置」填入实际页面的 CSS 选择器：

| 内容 | 默认选择器 | 属性约定 |
| --- | --- | --- |
| Bug 编号 | `[data-bug-id]` | 数字 `data-bug-id` / `data-ticket-id` / `data-issue-id` / `value` / 文本 |
| 标题、正文 | `[data-iwork-title]`、`[data-iwork-description]` | 可见文本 |
| 属性 | `[data-iwork-field]` | `data-iwork-field` / `data-name` 是字段名 |
| 评论 | `[data-iwork-comment]` | 可选 `data-id`、`data-time` |
| 附件 | `[data-iwork-attachment]` | `href` / `data-url`；`download` / `data-name` / 文本是原始文件名 |
| 图片 | `[data-iwork-image]` | `src` / `data-url`；`data-name` / `alt` 是原始文件名 |
| 群 | `[data-iwork-group]` | 群名文本；可选 `data-conversation-id` |
| 提及的文件 | `[data-iwork-file-mention]` | `data-name` / 文本；可选 `data-group`、`data-target-id` |

编号也接受 `/bug/123456`、`/ticket/123456`、`/issue/123456` 路径及 `bugId` / `bug_id` / `ticketId` / `ticket_id` / `issueId` / `issue_id` / `entityId` 查询参数；生产 iWork 缺陷页使用 `entityId`。标题中的年份或任意数字不会被当作编号。来自明确字段和 URL 的编号冲突会阻止采集。选择器只定位主页面；跨域 iframe、虚拟列表和分页内容不自动遍历，当前页面内可识别的折叠区会自动展开并等待渲染。

## 权限与数据

只使用 `activeTab`、`scripting`、`storage`、`nativeMessaging`、`downloads`、`alarms` 六项权限；没有全站访问权限，没有常驻页面脚本。正文只在主动点击采集时读取。不会写入问题或发送聊天消息。

扩展本地保存选择器、任务摘要、下载 ID / 登记路径及未送达的下载回执；不会保存完整采集快照或附件授权 URL。附件临时授权 URL 通过原生请求传给本地助手用于下载，由助手负责在归档快照、报告中隐藏凭据。页面地址与正文中的常见凭据在采集时隐藏。浏览器负责登录会话与下载认证；过期链接、浏览器不支持的认证方式、`blob:` / `data:` 图片会记录为缺口。

页面采集始终标记为部分覆盖；请核对报告中的「仍需补充」。单次结果上限 700 KiB；超限会省略条目并增加缺口。附件/图片/评论各最多 500 项；字段/群/文件提及各最多 100 项。

## 离线验证

在仓库根目录运行：

```sh
node --test tests/js/*.test.mjs
```

`extension/lib/collector.mjs` 导出 `collectTicket(selectors)`，该函数完全独立，可直接传给 `chrome.scripting.executeScript({target, func: collectTicket, args: [selectors]})` 或由浏览器测试在示例页面执行。真实 iWork DOM、登录会话下载、企业浏览器策略需要在目标环境完成首次验证。若页面接口受权限或企业策略限制，报告会区分“已查询但为空”和“接口未取得”，并保留缺口。
