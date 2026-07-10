import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { MainWindow } from './views/MainWindow'
import { CaptureOverlay } from './views/CaptureOverlay'
import { AnswerWindow } from './views/AnswerWindow'
import { StatusBadge } from './views/StatusBadge'
import './devBridge'
import './styles.css'

const params = new URLSearchParams(window.location.search)
const mode = params.get('mode') || 'main'
document.body.dataset.mode = mode

function Root() {
  if (mode === 'capture') return <CaptureOverlay />
  if (mode === 'answer') return <AnswerWindow id={params.get('id') || ''} />
  if (mode === 'badge') return <StatusBadge />
  return <MainWindow />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
