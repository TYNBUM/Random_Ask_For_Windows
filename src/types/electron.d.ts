export type ContentKind = 'image' | 'selection' | 'clipboard'

export interface CapturedContent {
  kind: ContentKind
  text?: string
  imageDataUrl?: string
  size?: { width: number; height: number }
  createdAt: number
}

export interface ModelConfig {
  apiKey: string
  baseUrl: string
  model: string
}

export interface AppSettings {
  text: ModelConfig
  vision: ModelConfig
  behavior: {
    launchAtLogin: boolean
    shortcuts: boolean
  }
}

export interface AnswerSession {
  id: string
  kind: 'image' | 'text'
  question: string
  sourceText: string
  imageDataUrl: string
  model: string
  answer: string
  status: 'queued' | 'streaming' | 'completed' | 'stopped' | 'error'
  error: string
  createdAt: number
}

export interface DesktopBackdrop {
  dataUrl: string
  width: number
  height: number
  luma: number
  captureMs: number
  dark?: boolean
}

interface RandomAskBridge {
  settings: {
    get(): Promise<AppSettings>
    save(value: AppSettings): Promise<AppSettings>
    test(kind: 'text' | 'vision', value: ModelConfig): Promise<{ ok: boolean; message: string }>
  }
  main: {
    setExpanded(expanded: boolean, height?: number): Promise<void>
    getBackdrop(): Promise<DesktopBackdrop | null>
    onIntro(callback: () => void): () => void
    onBackdrop(callback: (value: DesktopBackdrop) => void): () => void
    onAmbient(callback: (value: { luma: number; dark: boolean }) => void): () => void
    onExpanded(callback: (value: boolean) => void): () => void
    onOpenSettings(callback: () => void): () => void
    onContent(callback: (value: CapturedContent) => void): () => void
    onError(callback: (message: string) => void): () => void
  }
  actions: {
    capture(): Promise<{ ok: boolean; reason?: string }>
    grabText(): Promise<{ ok: boolean; reason?: string }>
    clipboard(): Promise<{ ok: boolean }>
  }
  capture: {
    getContext(): Promise<{ imageDataUrl: string; live?: boolean; width: number; height: number; scaleFactor: number } | null>
    onReset(callback: () => void): () => void
    complete(rect: { x: number; y: number; width: number; height: number }): Promise<{ ok: boolean; reason?: string }>
    cancel(): Promise<void>
  }
  ask: {
    submit(input: { kind: 'image' | 'text'; question: string; text?: string; imageDataUrl?: string }): Promise<{ id: string }>
  }
  answer: {
    get(id: string): Promise<AnswerSession | null>
    stop(id: string): Promise<{ ok: boolean }>
    retry(id: string): Promise<{ ok: boolean }>
    onUpdate(callback: (value: AnswerSession) => void): () => void
  }
  clipboard: { write(text: string): Promise<{ ok: boolean }> }
  app: {
    showMain(): Promise<void>
    openSettings(): Promise<void>
    openExternal(url: string): Promise<void>
  }
  window: {
    control(action: 'minimize' | 'close' | 'toggle-pin'): Promise<{ pinned?: boolean } | null>
  }
  badge: {
    onData(callback: (value: { state: 'armed' | 'captured'; label: string; hint?: string }) => void): () => void
  }
}

declare global {
  interface Window {
    randomAsk: RandomAskBridge
  }
}

export {}
