import { Sparkles } from 'lucide-react'

export function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`brand-mark ${compact ? 'brand-mark--compact' : ''}`} aria-label="RandomAsk 随问">
      <span className="brand-mark__icon"><Sparkles size={compact ? 14 : 16} strokeWidth={2.4} /></span>
      {!compact && <span className="brand-mark__text">RandomAsk <em>随问</em></span>}
    </div>
  )
}
