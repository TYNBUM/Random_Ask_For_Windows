import type { AnswerSession, AppSettings, CapturedContent, DesktopBackdrop, ModelConfig } from './types/electron'

// A tiny browser-only bridge makes `npm run dev` previewable in an ordinary browser.
// Electron always injects the real, privileged bridge first, so this never runs in the app.
if (!window.randomAsk) {
  let settings: AppSettings = {
    text: { apiKey: '', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4.1-mini' },
    vision: { apiKey: '', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4.1-mini' },
    behavior: { launchAtLogin: false, shortcuts: true },
  }
  const callbacks = {
    intro: new Set<() => void>(),
    backdrop: new Set<(value: DesktopBackdrop) => void>(),
    ambient: new Set<(value: { luma: number; dark: boolean }) => void>(),
    expanded: new Set<(value: boolean) => void>(),
    settings: new Set<() => void>(),
    content: new Set<(value: CapturedContent) => void>(),
    error: new Set<(value: string) => void>(),
    answer: new Set<(value: AnswerSession) => void>(),
    badge: new Set<(value: { state: 'armed' | 'captured'; label: string; hint?: string }) => void>(),
  }
  const listen = <T,>(set: Set<(value: T) => void>, callback: (value: T) => void) => {
    set.add(callback)
    return () => set.delete(callback)
  }
  const emitDemoText = (kind: 'selection' | 'clipboard') => {
    const content: CapturedContent = {
      kind,
      text: 'RandomAsk 让截图、选中文本与剪贴板提问变得更快，同时在真正发送前保留清晰的确认步骤。',
      createdAt: Date.now(),
    }
    callbacks.content.forEach((callback) => callback(content))
    callbacks.expanded.forEach((callback) => callback(true))
  }
  const demoSession: AnswerSession = {
    id: 'demo',
    kind: 'text',
    question: '请总结这段内容，并列出关键优势',
    sourceText: 'RandomAsk 是一个 Windows 桌面 AI 快问浮窗，支持框选截图、拖选文本和剪贴板提问。',
    imageDataUrl: '',
    model: 'gpt-4.1-mini',
    answer: 'RandomAsk 的核心价值是把 **AI 提问入口** 放到桌面边缘，让信息采集与提问形成一条短路径。\n\n### 关键优势\n\n- 支持截图、选中文本和剪贴板三种输入\n- 发送前可预览内容并补充问题\n- 文本与视觉模型独立配置\n- 流式 Markdown 回答，支持复制与重新生成\n\n```text\n采集 → 确认 → 提问 → 回答\n```',
    status: 'completed',
    error: '',
    createdAt: Date.now(),
  }
  const glassPreviewImage = `data:image/svg+xml;charset=utf-8,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="#697bd1"/><stop offset=".46" stop-color="#d99fc4"/><stop offset="1" stop-color="#69bcb5"/></linearGradient></defs><rect width="100%" height="100%" fill="url(#g)"/><circle cx="110" cy="470" r="150" fill="#2d477f" opacity=".72"/><circle cx="505" cy="120" r="180" fill="#ffd8a8" opacity=".72"/><path d="M0 300C150 210 260 400 600 230V600H0Z" fill="#426c87" opacity=".38"/></svg>')}`
  const darkGlassDemo = new URLSearchParams(window.location.search).get('ambient') === 'dark'
  const demoBackdrop = (): DesktopBackdrop => ({ dataUrl: glassPreviewImage, width: window.innerWidth, height: window.innerHeight, luma: darkGlassDemo ? 0.28 : 0.72, captureMs: 8, dark: darkGlassDemo })

  window.randomAsk = {
    settings: {
      get: async () => structuredClone(settings),
      save: async (value: AppSettings) => { settings = structuredClone(value); return structuredClone(settings) },
      test: async (_kind: 'text' | 'vision', value: ModelConfig) => value.baseUrl && value.model
        ? { ok: true, message: '连接成功（浏览器预览）' }
        : { ok: false, message: '请填写 Base URL 和模型名称' },
    },
    main: {
      setExpanded: async (value: boolean) => { callbacks.expanded.forEach((callback) => callback(value)) },
      getBackdrop: async () => demoBackdrop(),
      onIntro: (callback) => {
        callbacks.intro.add(callback)
        const timer = window.setTimeout(callback, 60)
        return () => { callbacks.intro.delete(callback); window.clearTimeout(timer) }
      },
      onBackdrop: (callback) => {
        callbacks.backdrop.add(callback)
        const timer = window.setTimeout(() => callback(demoBackdrop()), 30)
        return () => { callbacks.backdrop.delete(callback); window.clearTimeout(timer) }
      },
      onAmbient: (callback) => {
        callbacks.ambient.add(callback)
        const timer = window.setTimeout(() => callback({ luma: darkGlassDemo ? 0.28 : 0.72, dark: darkGlassDemo }), 45)
        return () => { callbacks.ambient.delete(callback); window.clearTimeout(timer) }
      },
      onExpanded: (callback) => listen(callbacks.expanded, callback),
      onOpenSettings: (callback) => { callbacks.settings.add(callback); return () => callbacks.settings.delete(callback) },
      onContent: (callback) => listen(callbacks.content, callback),
      onError: (callback) => listen(callbacks.error, callback),
    },
    actions: {
      capture: async () => { emitDemoText('selection'); return { ok: true } },
      grabText: async () => { emitDemoText('selection'); return { ok: true } },
      clipboard: async () => { emitDemoText('clipboard'); return { ok: true } },
    },
    capture: {
      getContext: async () => ({ imageDataUrl: '', live: true, width: 1440, height: 900, scaleFactor: 1 }),
      onReset: () => () => undefined,
      complete: async () => ({ ok: true }),
      cancel: async () => undefined,
    },
    ask: { submit: async () => ({ id: 'demo' }) },
    answer: {
      get: async () => demoSession,
      stop: async () => ({ ok: true }),
      retry: async () => ({ ok: true }),
      onUpdate: (callback) => listen(callbacks.answer, callback),
    },
    clipboard: { write: async () => ({ ok: true }) },
    app: {
      showMain: async () => undefined,
      openSettings: async () => undefined,
      openExternal: async () => undefined,
    },
    window: { control: async (action) => action === 'toggle-pin' ? { pinned: true } : null },
    badge: { onData: (callback) => listen(callbacks.badge, callback) },
  }
}
