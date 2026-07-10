import { useState } from 'react'
import { Bot, Eye, EyeOff, Image, LoaderCircle, TestTube2 } from 'lucide-react'
import type { ModelConfig } from '../types/electron'

interface ModelCardProps {
  kind: 'text' | 'vision'
  value: ModelConfig
  onChange(value: ModelConfig): void
}

export function ModelCard({ kind, value, onChange }: ModelCardProps) {
  const [showKey, setShowKey] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const isVision = kind === 'vision'

  const patch = (key: keyof ModelConfig, next: string) => onChange({ ...value, [key]: next })
  const test = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      setTestResult(await window.randomAsk.settings.test(kind, value))
    } finally {
      setTesting(false)
    }
  }

  return (
    <section className="model-card">
      <div className="model-card__title">
        <span className={`model-card__glyph ${isVision ? 'is-vision' : ''}`}>
          {isVision ? <Image size={16} /> : <Bot size={16} />}
        </span>
        <div>
          <h3>{isVision ? '视觉模型' : '文本模型'}</h3>
          <p>{isVision ? '用于框选截图' : '用于选中文本和剪贴板'}</p>
        </div>
        <button className="text-button" onClick={test} disabled={testing} type="button">
          {testing ? <LoaderCircle className="spin" size={14} /> : <TestTube2 size={14} />}
          测试
        </button>
      </div>

      <label className="field">
        <span>API Key</span>
        <div className="input-with-action">
          <input
            type={showKey ? 'text' : 'password'}
            value={value.apiKey}
            onChange={(event) => patch('apiKey', event.target.value)}
            placeholder="sk-…"
            autoComplete="off"
          />
          <button type="button" onClick={() => setShowKey((current) => !current)} aria-label={showKey ? '隐藏 API Key' : '显示 API Key'}>
            {showKey ? <EyeOff size={15} /> : <Eye size={15} />}
          </button>
        </div>
      </label>
      <div className="model-card__row">
        <label className="field field--wide">
          <span>Base URL</span>
          <input value={value.baseUrl} onChange={(event) => patch('baseUrl', event.target.value)} placeholder="https://api.openai.com/v1" />
        </label>
        <label className="field field--model">
          <span>模型名称</span>
          <input value={value.model} onChange={(event) => patch('model', event.target.value)} placeholder="模型 ID" />
        </label>
      </div>
      {testResult && (
        <div className={`test-result ${testResult.ok ? 'is-success' : 'is-error'}`} role="status">
          {testResult.message}
        </div>
      )}
    </section>
  )
}
