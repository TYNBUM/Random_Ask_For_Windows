import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  ArrowDown,
  Check,
  ClipboardCopy,
  Copy,
  Image as ImageIcon,
  LoaderCircle,
  MessageSquareText,
  Minus,
  Pin,
  PinOff,
  RotateCcw,
  Sparkles,
  Square,
  X,
} from 'lucide-react'
import type { AnswerSession } from '../types/electron'

function statusLabel(status: AnswerSession['status']) {
  if (status === 'queued') return '准备请求…'
  if (status === 'streaming') return '正在生成'
  if (status === 'completed') return '回答完成'
  if (status === 'stopped') return '已停止'
  return '生成失败'
}

export function AnswerWindow({ id }: { id: string }) {
  const [session, setSession] = useState<AnswerSession | null>(null)
  const [pinned, setPinned] = useState(false)
  const [copied, setCopied] = useState(false)
  const [showSource, setShowSource] = useState(false)
  const [autoFollow, setAutoFollow] = useState(true)
  const scrollerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    void window.randomAsk.answer.get(id).then(setSession)
    return window.randomAsk.answer.onUpdate((next) => {
      if (next.id === id) setSession(next)
    })
  }, [id])

  useEffect(() => {
    if (autoFollow && scrollerRef.current) {
      scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight
    }
  }, [session?.answer, session?.status, autoFollow])

  const copyAnswer = async () => {
    if (!session?.answer) return
    await window.randomAsk.clipboard.write(session.answer)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }

  if (!session) {
    return (
      <main className="answer-shell glass-surface answer-loading">
        <LoaderCircle className="spin" size={24} /><span>正在打开回答…</span>
      </main>
    )
  }

  const sourceSummary = session.kind === 'image'
    ? '屏幕框选'
    : `${session.sourceText.slice(0, 120)}${session.sourceText.length > 120 ? '…' : ''}`

  return (
    <main className="answer-shell glass-surface">
      <div className="answer-shell__glow" />
      <header className="answer-titlebar app-drag">
        <div className="answer-titlebar__identity">
          <span className="answer-logo"><Sparkles size={15} /></span>
          <div><strong>RandomAsk</strong><small>{session.model || '未配置模型'}</small></div>
        </div>
        <div className={`answer-status is-${session.status}`}>
          {session.status === 'streaming' && <LoaderCircle className="spin" size={12} />}
          {statusLabel(session.status)}
        </div>
        <div className="window-controls no-drag">
          <button onClick={async () => {
            const result = await window.randomAsk.window.control('toggle-pin')
            setPinned(Boolean(result?.pinned))
          }} aria-label={pinned ? '取消置顶' : '置顶窗口'} title={pinned ? '取消置顶' : '置顶'}>
            {pinned ? <PinOff size={15} /> : <Pin size={15} />}
          </button>
          <button onClick={() => void window.randomAsk.window.control('minimize')} aria-label="最小化"><Minus size={16} /></button>
          <button className="window-close" onClick={() => void window.randomAsk.window.control('close')} aria-label="关闭"><X size={17} /></button>
        </div>
      </header>

      <section className={`question-summary ${showSource ? 'is-open' : ''}`}>
        <button className="question-summary__main" onClick={() => setShowSource((value) => !value)} type="button">
          <span>{session.kind === 'image' ? <ImageIcon size={16} /> : <MessageSquareText size={16} />}</span>
          <div>
            <small>{session.question || (session.kind === 'image' ? '分析这张截图' : '理解并回答这段内容')}</small>
            <p>{sourceSummary}</p>
          </div>
          <span className="question-summary__more">{showSource ? '收起' : '查看'}</span>
        </button>
        {showSource && (
          <div className="question-source">
            {session.question && <p className="question-source__prompt"><strong>问题</strong>{session.question}</p>}
            {session.kind === 'image'
              ? <img src={session.imageDataUrl} alt="提问截图" />
              : <pre>{session.sourceText}</pre>}
          </div>
        )}
      </section>

      <div
        className="answer-scroller"
        ref={scrollerRef}
        onScroll={(event) => {
          const element = event.currentTarget
          const distance = element.scrollHeight - element.scrollTop - element.clientHeight
          setAutoFollow(distance < 64)
        }}
      >
        <article className="markdown-body">
          {!session.answer && session.status === 'streaming' && (
            <div className="answer-skeleton" aria-label="AI 正在思考">
              <i /><i /><i /><span><Sparkles size={17} /> 正在理解你的问题…</span>
            </div>
          )}
          {session.answer && (
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({ href, children }) => <a href={href} onClick={(event) => {
                  event.preventDefault()
                  if (href) void window.randomAsk.app.openExternal(href)
                }}>{children}</a>,
                code: ({ className, children, ...props }) => className
                  ? <code className={className} {...props}>{children}</code>
                  : <code {...props}>{children}</code>,
              }}
            >
              {session.answer}
            </ReactMarkdown>
          )}
          {session.status === 'streaming' && session.answer && <span className="stream-caret" aria-hidden="true" />}
          {session.status === 'error' && (
            <div className="answer-error" role="alert"><strong>没有拿到回答</strong><p>{session.error}</p></div>
          )}
          {session.status === 'stopped' && !session.answer && <div className="answer-empty">生成已停止，没有收到内容。</div>}
        </article>
      </div>

      {!autoFollow && (
        <button className="jump-latest" onClick={() => {
          setAutoFollow(true)
          if (scrollerRef.current) scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight
        }} type="button"><ArrowDown size={14} />回到最新</button>
      )}

      <footer className="answer-toolbar">
        <button onClick={() => void copyAnswer()} disabled={!session.answer} type="button">
          {copied ? <Check size={15} /> : <Copy size={15} />}{copied ? '已复制' : '复制答案'}
        </button>
        {session.status === 'streaming' ? (
          <button onClick={() => void window.randomAsk.answer.stop(id)} type="button"><Square size={13} fill="currentColor" />停止生成</button>
        ) : (
          <button onClick={() => void window.randomAsk.answer.retry(id)} type="button"><RotateCcw size={15} />重新生成</button>
        )}
        <button className="answer-toolbar__new" onClick={() => void window.randomAsk.app.showMain()} type="button"><ClipboardCopy size={15} />新提问</button>
      </footer>
    </main>
  )
}
