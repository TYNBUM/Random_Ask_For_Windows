import { useEffect, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import {
  ChevronDown,
  ChevronUp,
  Clipboard,
  Image as ImageIcon,
  LoaderCircle,
  ScanLine,
  Send,
  Settings,
  TextCursorInput,
  Trash2,
  X,
} from 'lucide-react'
import { BrandMark } from '../components/BrandMark'
import { ModelCard } from '../components/ModelCard'
import type { AppSettings, CapturedContent, DesktopBackdrop } from '../types/electron'

const EMPTY_SETTINGS: AppSettings = {
  text: { apiKey: '', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4.1-mini' },
  vision: { apiKey: '', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4.1-mini' },
  behavior: { launchAtLogin: false, shortcuts: true },
}

type View = 'compose' | 'settings'
type BusyAction = 'capture' | 'selection' | 'clipboard' | 'send' | null

function sourceLabel(content: CapturedContent | null) {
  if (!content) return '尚未添加内容'
  if (content.kind === 'image') return '屏幕框选'
  if (content.kind === 'selection') return '选中文本'
  return '剪贴板文本'
}

export function MainWindow() {
  const [introPending, setIntroPending] = useState(true)
  const [booting, setBooting] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [view, setView] = useState<View>('compose')
  const [content, setContent] = useState<CapturedContent | null>(null)
  const [question, setQuestion] = useState('')
  const [settings, setSettings] = useState<AppSettings>(EMPTY_SETTINGS)
  const [busy, setBusy] = useState<BusyAction>(null)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [desktopBackdrop, setDesktopBackdrop] = useState<DesktopBackdrop | null>(null)
  const [backdropSlots, setBackdropSlots] = useState<[DesktopBackdrop | null, DesktopBackdrop | null]>([null, null])
  const [activeBackdropSlot, setActiveBackdropSlot] = useState(0)
  const [ambientDark, setAmbientDark] = useState<boolean | null>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const activeBackdropSlotRef = useRef(0)
  const backdropSequenceRef = useRef(0)
  const lastBackdropSwitchAtRef = useRef(0)

  useEffect(() => {
    let introFrame = 0
    let introPaintFrame = 0
    let backdropFrame = 0
    let backdropDelayTimer = 0
    const removeIntro = window.randomAsk.main.onIntro(() => {
      setIntroPending(true)
      setBooting(false)
      introFrame = window.requestAnimationFrame(() => {
        introPaintFrame = window.requestAnimationFrame(() => {
          setIntroPending(false)
          setBooting(true)
        })
      })
    })
    const applyBackdrop = (value: DesktopBackdrop) => {
      const sequence = ++backdropSequenceRef.current
      window.clearTimeout(backdropDelayTimer)
      const image = new Image()
      let committed = false
      let queued = false
      const commit = () => {
        if (committed || sequence !== backdropSequenceRef.current) return
        committed = true
        const nextSlot = activeBackdropSlotRef.current === 0 ? 1 : 0
        setBackdropSlots((current) => {
          const next: [DesktopBackdrop | null, DesktopBackdrop | null] = [current[0], current[1]]
          next[nextSlot] = value
          return next
        })
        window.cancelAnimationFrame(backdropFrame)
        backdropFrame = window.requestAnimationFrame(() => {
          if (sequence !== backdropSequenceRef.current) return
          activeBackdropSlotRef.current = nextSlot
          lastBackdropSwitchAtRef.current = window.performance.now()
          setActiveBackdropSlot(nextSlot)
          setDesktopBackdrop(value)
          if (typeof value.dark === 'boolean') setAmbientDark(value.dark)
        })
      }
      const queueCommit = () => {
        if (queued || sequence !== backdropSequenceRef.current) return
        queued = true
        const elapsed = window.performance.now() - lastBackdropSwitchAtRef.current
        const wait = Math.max(0, 190 - elapsed)
        if (wait > 0) backdropDelayTimer = window.setTimeout(commit, wait)
        else commit()
      }
      image.onload = queueCommit
      image.onerror = () => undefined
      image.src = value.dataUrl
      if (typeof image.decode === 'function') void image.decode().then(queueCommit).catch(() => {})
    }
    void window.randomAsk.main.getBackdrop().then((value) => { if (value) applyBackdrop(value) })
    const removeBackdrop = window.randomAsk.main.onBackdrop(applyBackdrop)
    const removeAmbient = window.randomAsk.main.onAmbient((value) => setAmbientDark(value.dark))
    void window.randomAsk.settings.get().then(setSettings)
    const removeExpanded = window.randomAsk.main.onExpanded(setExpanded)
    const removeSettings = window.randomAsk.main.onOpenSettings(() => {
      setView('settings')
      setExpanded(true)
    })
    const removeContent = window.randomAsk.main.onContent((next) => {
      setContent(next)
      setView('compose')
      setBusy(null)
      setError('')
      setExpanded(true)
      requestAnimationFrame(() => inputRef.current?.focus())
    })
    const removeError = window.randomAsk.main.onError((message) => {
      setBusy(null)
      setError(message)
      setExpanded(true)
      window.setTimeout(() => setError(''), 4200)
    })
    return () => {
      window.cancelAnimationFrame(introFrame)
      window.cancelAnimationFrame(introPaintFrame)
      window.cancelAnimationFrame(backdropFrame)
      window.clearTimeout(backdropDelayTimer)
      backdropSequenceRef.current += 1
      removeIntro()
      removeBackdrop()
      removeAmbient()
      removeExpanded()
      removeSettings()
      removeContent()
      removeError()
    }
  }, [])

  const setDrawer = async (open: boolean, nextView: View = view) => {
    if (open) {
      setIntroPending(false)
      setBooting(false)
    }
    setView(nextView)
    setExpanded(open)
    await window.randomAsk.main.setExpanded(open, nextView === 'settings' ? 650 : 416)
    if (open && nextView === 'compose') requestAnimationFrame(() => inputRef.current?.focus())
  }

  const runAction = async (action: Exclude<BusyAction, 'send' | null>) => {
    setError('')
    setBusy(action)
    try {
      if (action === 'capture') await window.randomAsk.actions.capture()
      if (action === 'selection') await window.randomAsk.actions.grabText()
      if (action === 'clipboard') await window.randomAsk.actions.clipboard()
    } finally {
      if (action !== 'capture') setBusy(null)
    }
  }

  const submit = async () => {
    if (!content || busy === 'send') return
    setBusy('send')
    setError('')
    try {
      await window.randomAsk.ask.submit({
        kind: content.kind === 'image' ? 'image' : 'text',
        question,
        text: content.text,
        imageDataUrl: content.imageDataUrl,
      })
      setContent(null)
      setQuestion('')
      await setDrawer(false, 'compose')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '发送失败，请重试')
    } finally {
      setBusy(null)
    }
  }

  const saveSettings = async () => {
    setBusy('send')
    setError('')
    try {
      setSettings(await window.randomAsk.settings.save(settings))
      setSaved(true)
      window.setTimeout(() => setSaved(false), 1800)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '保存失败')
    } finally {
      setBusy(null)
    }
  }

  const modelName = content?.kind === 'image' ? settings.vision.model : settings.text.model
  const backdropStyle = desktopBackdrop ? {
    '--snapshot-luma': String(desktopBackdrop.luma),
  } as CSSProperties : undefined
  const backdropLayerStyle = (backdrop: DesktopBackdrop | null) => backdrop ? {
    backgroundImage: `url("${backdrop.dataUrl}")`,
    backgroundSize: `${backdrop.width}px ${backdrop.height}px`,
  } as CSSProperties : undefined
  const backdropLayers = backdropSlots.map((backdrop, index) => backdrop && (
    <div
      className={`sampled-backdrop__layer ${index === activeBackdropSlot ? 'is-active' : ''}`}
      key={index}
      style={backdropLayerStyle(backdrop)}
    />
  ))
  const useDarkBackdrop = ambientDark ?? desktopBackdrop?.dark ?? Boolean(desktopBackdrop && desktopBackdrop.luma < 0.52)

  return (
    <main
      className={`main-shell ${expanded ? 'is-expanded' : ''} ${desktopBackdrop ? 'has-sampled-backdrop' : ''} ${useDarkBackdrop ? 'has-dark-backdrop' : ''} ${introPending && !expanded ? 'is-intro-pending' : ''} ${booting && !expanded ? 'is-booting' : ''}`}
      style={backdropStyle}
    >
      {expanded && (
        <section className="drawer glass-surface" aria-label={view === 'settings' ? '模型设置' : '提问抽屉'}>
          <div className="sampled-backdrop sampled-backdrop--drawer">{backdropLayers}</div>
          <div className="drawer__glow" />
          <header className="drawer__header app-drag">
            <BrandMark />
            <div className="drawer__header-actions no-drag">
              {view === 'compose' && (
                <button className="icon-button" onClick={() => setDrawer(true, 'settings')} aria-label="打开设置" title="模型设置">
                  <Settings size={16} />
                </button>
              )}
              {view === 'settings' && (
                <button className="text-button" onClick={() => setDrawer(true, 'compose')} type="button">
                  返回提问
                </button>
              )}
              <button className="icon-button" onClick={() => setDrawer(false)} aria-label="收起抽屉" title="收起">
                <ChevronDown size={17} />
              </button>
            </div>
          </header>

          {view === 'compose' ? (
            <div className="compose-panel">
              <div className={`source-preview ${content ? 'has-content' : ''}`}>
                {content?.kind === 'image' ? (
                  <img className="source-preview__image" src={content.imageDataUrl} alt="框选截图预览" />
                ) : (
                  <span className="source-preview__icon">
                    {content ? <TextCursorInput size={18} /> : <ImageIcon size={18} />}
                  </span>
                )}
                <div className="source-preview__body">
                  <div className="source-preview__meta">
                    <strong>{sourceLabel(content)}</strong>
                    {content?.kind === 'image' && content.size && <span>{content.size.width} × {content.size.height}px</span>}
                    {content?.text && <span>{content.text.length.toLocaleString()} 字</span>}
                  </div>
                  <p>{content?.text || (content?.kind === 'image' ? '截图仅在点击发送后上传' : '从下方选择一种输入方式')}</p>
                </div>
                {content && (
                  <button className="icon-button icon-button--subtle" onClick={() => setContent(null)} aria-label="移除内容" title="移除">
                    <Trash2 size={15} />
                  </button>
                )}
              </div>

              <label className="question-box">
                <textarea
                  ref={inputRef}
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                      event.preventDefault()
                      void submit()
                    }
                    if (event.key === 'Escape') void setDrawer(false)
                  }}
                  placeholder="补充问题（可选）"
                  rows={3}
                />
                <span className="question-box__hint">Enter 发送 · Shift+Enter 换行</span>
              </label>

              <div className="compose-actions">
                <span className={`model-chip ${content?.kind === 'image' ? 'is-vision' : ''}`}>
                  <span className="model-chip__dot" />
                  {content?.kind === 'image' ? '视觉' : '文本'} · {modelName || '未配置'}
                </span>
                <button className="primary-button" disabled={!content || busy === 'send'} onClick={() => void submit()} type="button">
                  {busy === 'send' ? <LoaderCircle className="spin" size={16} /> : <Send size={16} />}
                  发送
                </button>
              </div>
            </div>
          ) : (
            <div className="settings-panel">
              <div className="settings-panel__intro">
                <div><h2>模型设置</h2><p>文本与视觉通道独立配置，API Key 使用 Windows 系统加密保存。</p></div>
              </div>
              <div className="settings-scroll">
                <ModelCard kind="text" value={settings.text} onChange={(text) => setSettings({ ...settings, text })} />
                <ModelCard kind="vision" value={settings.vision} onChange={(vision) => setSettings({ ...settings, vision })} />
                <section className="behavior-card">
                  <label className="toggle-row">
                    <span><strong>全局快捷键</strong><small>Ctrl+Alt+A / S / T / V</small></span>
                    <input type="checkbox" checked={settings.behavior.shortcuts} onChange={(event) => setSettings({ ...settings, behavior: { ...settings.behavior, shortcuts: event.target.checked } })} />
                    <i aria-hidden="true" />
                  </label>
                  <label className="toggle-row">
                    <span><strong>开机启动</strong><small>登录 Windows 后自动运行</small></span>
                    <input type="checkbox" checked={settings.behavior.launchAtLogin} onChange={(event) => setSettings({ ...settings, behavior: { ...settings.behavior, launchAtLogin: event.target.checked } })} />
                    <i aria-hidden="true" />
                  </label>
                </section>
              </div>
              <div className="settings-footer">
                <span className={saved ? 'save-state is-visible' : 'save-state'}>已安全保存</span>
                <button className="primary-button" onClick={() => void saveSettings()} disabled={busy === 'send'} type="button">
                  {busy === 'send' ? <LoaderCircle className="spin" size={16} /> : null}
                  保存设置
                </button>
              </div>
            </div>
          )}

          {error && (
            <div className="inline-toast" role="alert">
              <span>{error}</span>
              <button onClick={() => setError('')} aria-label="关闭提示"><X size={14} /></button>
            </div>
          )}
        </section>
      )}

      <section
        className="dock glass-surface app-drag"
        aria-label="RandomAsk 快捷操作"
        onAnimationEnd={(event) => {
          if (event.target === event.currentTarget) setBooting(false)
        }}
      >
        <div className="sampled-backdrop sampled-backdrop--dock">{backdropLayers}</div>
        <div className="dock__shine" />
        <div className="drag-grip" title="拖动浮窗"><i /><i /><i /><i /><i /><i /></div>
        <div className="dock__actions no-drag">
          <button className="action-button action-button--capture" onClick={() => void runAction('capture')} disabled={busy !== null} type="button" title="Ctrl+Alt+S">
            <span>{busy === 'capture' ? <LoaderCircle className="spin" size={20} /> : <ScanLine size={20} />}</span>
            <em>框选问</em>
          </button>
          <button className="action-button action-button--text" onClick={() => void runAction('selection')} disabled={busy !== null} type="button" title="Ctrl+Alt+T">
            <span>{busy === 'selection' ? <LoaderCircle className="spin" size={20} /> : <TextCursorInput size={20} />}</span>
            <em>拖文本</em>
          </button>
          <button className="action-button action-button--clipboard" onClick={() => void runAction('clipboard')} disabled={busy !== null} type="button" title="Ctrl+Alt+V">
            <span>{busy === 'clipboard' ? <LoaderCircle className="spin" size={20} /> : <Clipboard size={20} />}</span>
            <em>剪贴板</em>
          </button>
        </div>
        <div className="dock__rail no-drag">
          <button className="rail-button" onClick={() => setDrawer(true, 'settings')} aria-label="模型设置" title="模型设置"><Settings size={16} /></button>
          <button className={`rail-button ${content ? 'has-draft' : ''}`} onClick={() => setDrawer(!expanded, view)} aria-label={expanded ? '收起抽屉' : '展开抽屉'} title={expanded ? '收起' : '展开'}>
            {expanded ? <ChevronDown size={17} /> : <ChevronUp size={17} />}
          </button>
        </div>
      </section>
    </main>
  )
}
