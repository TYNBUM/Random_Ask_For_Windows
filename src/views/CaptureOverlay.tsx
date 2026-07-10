import { useEffect, useMemo, useRef, useState } from 'react'
import { MousePointer2 } from 'lucide-react'

interface Point { x: number; y: number }
interface Rect { x: number; y: number; width: number; height: number }
interface CaptureContext { imageDataUrl: string; live?: boolean; width: number; height: number; scaleFactor: number }

function rectFromPoints(start: Point, end: Point): Rect {
  return {
    x: Math.min(start.x, end.x),
    y: Math.min(start.y, end.y),
    width: Math.abs(end.x - start.x),
    height: Math.abs(end.y - start.y),
  }
}

export function CaptureOverlay() {
  const [context, setContext] = useState<CaptureContext | null>(null)
  const [start, setStart] = useState<Point | null>(null)
  const [end, setEnd] = useState<Point | null>(null)
  const [dragging, setDragging] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const reset = () => {
      setStart(null)
      setEnd(null)
      setDragging(false)
      void window.randomAsk.capture.getContext().then(setContext)
    }
    reset()
    const removeReset = window.randomAsk.capture.onReset(reset)
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') void window.randomAsk.capture.cancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      removeReset()
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [])

  const rect = useMemo(() => start && end ? rectFromPoints(start, end) : null, [start, end])
  const scale = context?.scaleFactor || 1

  return (
    <div
      ref={rootRef}
      className={`capture-overlay ${context?.live ? 'is-live' : ''} ${dragging ? 'is-dragging' : ''}`}
      onContextMenu={(event) => { event.preventDefault(); void window.randomAsk.capture.cancel() }}
      onPointerDown={(event) => {
        if (event.button !== 0) return
        const point = { x: event.clientX, y: event.clientY }
        setStart(point)
        setEnd(point)
        setDragging(true)
        event.currentTarget.setPointerCapture(event.pointerId)
      }}
      onPointerMove={(event) => {
        if (dragging) setEnd({ x: event.clientX, y: event.clientY })
      }}
      onPointerUp={(event) => {
        if (!dragging || !start) return
        const finished = rectFromPoints(start, { x: event.clientX, y: event.clientY })
        setEnd({ x: event.clientX, y: event.clientY })
        setDragging(false)
        if (finished.width >= 8 && finished.height >= 8) void window.randomAsk.capture.complete(finished)
        else {
          setStart(null)
          setEnd(null)
        }
      }}
    >
      {context?.imageDataUrl && <img className="capture-background" src={context.imageDataUrl} alt="" draggable={false} />}
      {!rect || (rect.width < 1 && rect.height < 1) ? (
        <div className="capture-dim capture-dim--full" />
      ) : (
        <>
          <div className="capture-dim" style={{ left: 0, top: 0, right: 0, height: rect.y }} />
          <div className="capture-dim" style={{ left: 0, top: rect.y, width: rect.x, height: rect.height }} />
          <div className="capture-dim" style={{ left: rect.x + rect.width, top: rect.y, right: 0, height: rect.height }} />
          <div className="capture-dim" style={{ left: 0, top: rect.y + rect.height, right: 0, bottom: 0 }} />
          <div className="selection-rect" style={{ left: rect.x, top: rect.y, width: rect.width, height: rect.height }}>
            <i className="selection-handle selection-handle--tl" />
            <i className="selection-handle selection-handle--tr" />
            <i className="selection-handle selection-handle--bl" />
            <i className="selection-handle selection-handle--br" />
            {rect.width > 70 && rect.height > 36 && (
              <span className="selection-size">
                {Math.round(rect.width * scale)} × {Math.round(rect.height * scale)}
              </span>
            )}
          </div>
        </>
      )}
      {!dragging && (
        <div className="capture-instruction">
          <MousePointer2 size={17} />
          <span>拖拽框选要提问的区域</span>
          <kbd>Esc</kbd><small>取消</small>
        </div>
      )}
    </div>
  )
}
