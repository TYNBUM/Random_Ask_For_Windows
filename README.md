# RandomAsk（随问）

RandomAsk 是一个面向 Windows 的桌面 AI 快问浮窗。它常驻桌面边缘，通过框选截图、拖选文本和读取剪贴板三种方式采集内容；内容会先在本地预览，只有点击“发送”后才交给所配置的 AI 服务。

## 已实现

- 置顶 Liquid Glass 浮窗：采样桌面背景并进行模糊、轻微折射和自适应明暗处理
- 前台应用稳定切换时自动刷新玻璃快照，并以 250ms 周期在浮窗自身范围内采样亮度；刷新无需隐藏浮窗，新旧快照交叉淡化，迟滞判定避免临界明暗闪烁
- 预热透明框选窗，先立即框选、松手后仅截最终区域；支持 DPI 感知裁切与 `Esc` / 右键取消
- 一次性“拖文本”模式：等待下一次真实鼠标拖选，再自动复制
- 纯文本剪贴板读取，不读取图片、文件或 HTML
- 文本模型与视觉模型独立的 API Key、Base URL、模型名称
- OpenAI Chat Completions 兼容接口及流式 Markdown 回答
- 独立回答窗口：停止、重试、复制、置顶、回到最新
- Windows 托盘、单实例、开机启动和全局快捷键
- API Key 优先通过 Windows DPAPI（Electron `safeStorage`）加密落盘

## 本地运行

```powershell
npm install
npm run dev
```

如果所在网络无法从 GitHub 下载 Electron，可先配置镜像：

```powershell
$env:ELECTRON_MIRROR='https://npmmirror.com/mirrors/electron/'
npm install
```

首次启动后，打开浮窗右侧的设置按钮，分别填写文本和视觉模型配置。Base URL 可以填写 `https://api.openai.com/v1`，也可以填写其他 OpenAI 兼容服务地址。

## 快捷键

| 快捷键 | 操作 |
| --- | --- |
| `Ctrl+Alt+A` | 显示或隐藏主浮窗 |
| `Ctrl+Alt+S` | 框选截图 |
| `Ctrl+Alt+T` | 拖选文本 |
| `Ctrl+Alt+V` | 读取剪贴板 |

“拖文本”被触发后，会出现不抢焦点的提示角标。请在 15 秒内于原应用中拖选一段文字；松开鼠标后 RandomAsk 会发送一次 `Ctrl+C` 并读取复制结果。Windows 的权限隔离会阻止普通权限应用读取以管理员身份运行的应用，遇到这种情况会显示“未读取到选中文本”。

## 验证与打包

```powershell
npm test
npm run build
npm run pack
# 或生成 NSIS 安装包
npm run dist
```

构建产物位于 `release/`。

> Windows 无闪快照依赖 `WDA_EXCLUDEFROMCAPTURE`，因此 Electron 固定为 `33.4.11`。升级 Electron 时必须重新验证主窗的 Display Affinity 为 `17`；验证失败时应用只保留启动快照，不会退回隐藏窗口截图。

## 隐私说明

- 截图、选区和剪贴板默认不会在采集后自动上传。
- 模型请求只发送到用户配置的 Base URL。
- 应用不会长期监听鼠标或剪贴板；文本拖选监听最多持续 15 秒。
- 桌面背景快照只在本机内存中用于玻璃材质渲染，不会随模型请求上传。
- 配置文件位于 Electron 的用户数据目录，API Key 在 Windows 上使用 DPAPI 加密。
