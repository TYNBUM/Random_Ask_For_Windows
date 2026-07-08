# 随问 (random ask)

> 当前分支：`chn-version`，这是中文版本（chn version）。

随问（random ask）是一个 Windows 桌面浮窗原型：点击浮窗后可以在原应用里拖选文本并自动复制提问，也可以拖拽框选屏幕区域，将截图发送给支持视觉的 LLM 进行解答。

## 使用方式

1. 安装 Python 3.10+。
2. 在本目录复制 `.env.example` 为 `.env`，填入 `OPENAI_API_KEY`。
3. 如使用 OpenAI 兼容服务，设置 `OPENAI_BASE_URL`、`VISION_MODEL` 和 `TEXT_MODEL`。如果框选问和拖文本要走不同接入，额外配置各自的专用 key 和 base URL。
4. 双击 `run_random_ask.bat` 启动。

启动后会出现一个置顶小浮窗：

- `框选问`：隐藏浮窗，拖拽框选屏幕区域，输入可选问题后发送截图给模型。
- `拖文本`：隐藏浮窗，在目标应用中拖选文本；松开鼠标后自动 `Ctrl+C`，复制成功会在鼠标附近出现确认角标，点击 `确认` 后直接发送给模型。
- `剪贴板`：读取当前剪贴板文本，输入可选问题后发送给模型。

启动脚本会后台启动应用，依赖检查完成后终端窗口会自动关闭。程序会在 Windows “显示隐藏图标”的托盘折叠区显示图标，右键菜单支持 `显示浮窗` 和 `退出`。

主浮窗内部已去掉装饰元素，只保留胶囊型/体育场型交互按钮；外围也使用真正的胶囊框。胶囊左端保留一个动态齿轮拖动区，方便拖动浮窗。抽屉、文本区和关键交互角标仍保持赛博朋克风格：深黑蓝底、霓虹青/品红/黄色分层描边、扫描线、电路线和状态灯。

文本预览、问题输入和回答区域的滚动轨道/滑块也已改成自绘赛博朋克样式，包含霓虹轨道、发光滑块和端点状态灯。

提问窗口和回答窗口会从主浮窗上方以抽屉方式展开；浮窗默认靠屏幕下方显示，方便给上展开抽屉留出空间。

主浮窗已移除左侧 AUX 信息框和内部装饰，只保留胶囊型交互按钮；拖动主浮窗时，已展开的抽屉会同步跟随。

`框选问` 和 `剪贴板` 的输入抽屉不会重复打开：已有抽屉时再次点击同一按钮会折叠/恢复该抽屉，保留当前内容。

## 配置

`.env` 支持以下变量：

```env
OPENAI_API_KEY=sk-your-key
OPENAI_BASE_URL=
VISION_OPENAI_API_KEY=
VISION_OPENAI_BASE_URL=
VISION_MODEL=qwen3-vl-flash
TEXT_OPENAI_API_KEY=
TEXT_OPENAI_BASE_URL=
TEXT_MODEL=qwen3.6-flash
LLM_MAX_TOKENS=1200
LLM_TEMPERATURE=0.2
```

`OPENAI_API_KEY` 和 `OPENAI_BASE_URL` 是全局默认值。`框选问` 会优先使用 `VISION_OPENAI_API_KEY` / `VISION_OPENAI_BASE_URL`，缺省时回退到全局配置；`拖文本` 和 `剪贴板` 会优先使用 `TEXT_OPENAI_API_KEY` / `TEXT_OPENAI_BASE_URL`，缺省时回退到全局配置。

`框选问` 使用 `VISION_MODEL`，需要支持图片输入。`拖文本` 和 `剪贴板` 使用 `TEXT_MODEL`，文本模型即可。

## 当前边界

- 框选模式发送的是截图，不依赖本机 OCR。模型会直接读图并回答。
- `拖文本` 依赖目标应用支持鼠标选中文本和 `Ctrl+C` 复制；PDF 图片、Canvas、远程桌面等复制失败时请改用 `框选问`。
- 若要长期后台运行，可把 `run_random_ask.bat` 放到 Windows 启动项，或后续用 PyInstaller 打包为 exe。
