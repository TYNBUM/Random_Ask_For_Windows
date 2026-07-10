import { useEffect, useState } from 'react'
import { Check, MousePointer2 } from 'lucide-react'

interface BadgeData {
  state: 'armed' | 'captured'
  label: string
  hint?: string
}

export function StatusBadge() {
  const [data, setData] = useState<BadgeData>({ state: 'armed', label: '拖选一段文字', hint: 'Esc 取消' })
  useEffect(() => window.randomAsk.badge.onData(setData), [])
  const captured = data.state === 'captured'
  return (
    <div className={`status-badge ${captured ? 'is-captured' : ''}`}>
      <span className="status-badge__icon">{captured ? <Check size={16} /> : <MousePointer2 size={16} />}</span>
      <strong>{data.label}</strong>
      {data.hint && <><i /><small>{data.hint}</small></>}
    </div>
  )
}
