'use strict'

const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  clipboard,
  desktopCapturer,
  dialog,
  globalShortcut,
  ipcMain,
  nativeImage,
  powerMonitor,
  safeStorage,
  screen,
  shell,
  Notification,
} = require('electron')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { execFile, spawn } = require('node:child_process')
const { pathToFileURL } = require('node:url')
const readline = require('node:readline')
const {
  normalizeEndpoint,
  buildChatPayload,
  createSSEParser,
  extractDelta,
  describeHttpError,
} = require('./lib/api.cjs')
const { clampLuma, advanceAmbientMode, advanceForegroundWindow } = require('./lib/ambient.cjs')

const APP_NAME = 'RandomAsk 随问'
const MAIN_WIDTH = 392
const COLLAPSED_HEIGHT = 80
const DRAWER_HEIGHT = 416
const AMBIENT_POLL_MS = 250
const AMBIENT_DARK_ENTER = 0.42
const AMBIENT_LIGHT_ENTER = 0.62
const WINDOWS_BUILD = Number(os.release().split('.').pop()) || 0
const SUPPORTS_NO_FLASH_CAPTURE = process.platform === 'win32' && WINDOWS_BUILD >= 19041

const DEFAULT_SETTINGS = Object.freeze({
  text: {
    apiKey: '',
    baseUrl: 'https://api.openai.com/v1',
    model: 'gpt-4.1-mini',
  },
  vision: {
    apiKey: '',
    baseUrl: 'https://api.openai.com/v1',
    model: 'gpt-4.1-mini',
  },
  behavior: {
    launchAtLogin: false,
    shortcuts: true,
  },
})

let mainWindow = null
let captureWindow = null
let captureWindowReady = null
let badgeWindow = null
let tray = null
let isQuitting = false
let mainExpanded = false
let settingsCache = null
let captureContext = null
let textCaptureRunning = false
let snapTimer = null
let isSnapping = false
let screenCaptureWorker = null
let screenCaptureWorkerReady = null
let screenCaptureRequestId = 0
let backdropRefreshTimer = null
let backdropRefreshDueAt = 0
let backdropCaptureActive = false
let backdropRetryCount = 0
let backdropRefreshPending = false
let lastMainBackdrop = null
let ambientMonitorTimer = null
let ambientMonitorRunning = false
let ambientPaused = false
let ambientResumeTimer = null
let lastForegroundWindow = ''
let foregroundCandidateWindow = ''
let foregroundCandidateCount = 0
let mainNativeWindowHandle = ''
let captureExclusionVerified = false
let captureExclusionVerificationTimer = null
let captureExclusionVerificationRunning = false
let currentBackdropDark = null
let ambientCandidateDark = null
let ambientCandidateCount = 0
let lastBackdropRefreshAt = 0
let lastMainMoveAt = 0
let mainShouldBeVisible = true
let captureFlowActive = false
let captureCompleting = false
const ambientPauseReasons = new Set()
const screenCaptureRequests = new Map()
const answerWindows = new Map()
const answerSessions = new Map()
const abortControllers = new Map()

function settingsPath() {
  return path.join(app.getPath('userData'), 'settings.json')
}

function encryptSecret(value) {
  if (!value) return ''
  if (safeStorage.isEncryptionAvailable()) {
    return `dpapi:${safeStorage.encryptString(value).toString('base64')}`
  }
  return `fallback:${Buffer.from(value, 'utf8').toString('base64')}`
}

function decryptSecret(value) {
  if (!value) return ''
  try {
    if (value.startsWith('dpapi:')) {
      return safeStorage.decryptString(Buffer.from(value.slice(6), 'base64'))
    }
    if (value.startsWith('fallback:')) {
      return Buffer.from(value.slice(9), 'base64').toString('utf8')
    }
  } catch {
    return ''
  }
  return ''
}

function cleanModelConfig(value, fallback) {
  return {
    apiKey: String(value?.apiKey ?? fallback.apiKey).trim(),
    baseUrl: String(value?.baseUrl ?? fallback.baseUrl).trim(),
    model: String(value?.model ?? fallback.model).trim(),
  }
}

function loadSettings() {
  if (settingsCache) return structuredClone(settingsCache)
  let stored = {}
  try {
    stored = JSON.parse(fs.readFileSync(settingsPath(), 'utf8'))
  } catch {
    stored = {}
  }
  settingsCache = {
    text: {
      apiKey: decryptSecret(stored.text?.apiKey),
      baseUrl: stored.text?.baseUrl || DEFAULT_SETTINGS.text.baseUrl,
      model: stored.text?.model || DEFAULT_SETTINGS.text.model,
    },
    vision: {
      apiKey: decryptSecret(stored.vision?.apiKey),
      baseUrl: stored.vision?.baseUrl || DEFAULT_SETTINGS.vision.baseUrl,
      model: stored.vision?.model || DEFAULT_SETTINGS.vision.model,
    },
    behavior: {
      launchAtLogin: Boolean(stored.behavior?.launchAtLogin),
      shortcuts: stored.behavior?.shortcuts !== false,
    },
  }
  return structuredClone(settingsCache)
}

function saveSettings(value) {
  const current = loadSettings()
  const next = {
    text: cleanModelConfig(value?.text, current.text),
    vision: cleanModelConfig(value?.vision, current.vision),
    behavior: {
      launchAtLogin: Boolean(value?.behavior?.launchAtLogin),
      shortcuts: value?.behavior?.shortcuts !== false,
    },
  }
  fs.mkdirSync(path.dirname(settingsPath()), { recursive: true })
  const diskValue = {
    text: { ...next.text, apiKey: encryptSecret(next.text.apiKey) },
    vision: { ...next.vision, apiKey: encryptSecret(next.vision.apiKey) },
    behavior: next.behavior,
  }
  const tempPath = `${settingsPath()}.tmp`
  fs.writeFileSync(tempPath, JSON.stringify(diskValue, null, 2), 'utf8')
  fs.renameSync(tempPath, settingsPath())
  settingsCache = next
  app.setLoginItemSettings({ openAtLogin: next.behavior.launchAtLogin })
  registerGlobalShortcuts()
  return structuredClone(next)
}

function rendererUrl(mode, params = {}) {
  const base = process.env.VITE_DEV_SERVER_URL
    ? new URL(process.env.VITE_DEV_SERVER_URL)
    : new URL(pathToFileURL(path.join(__dirname, '..', 'dist', 'index.html')).toString())
  base.searchParams.set('mode', mode)
  for (const [key, value] of Object.entries(params)) base.searchParams.set(key, String(value))
  return base.toString()
}

function secureWindow(win) {
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (event, url) => {
    if (url !== win.webContents.getURL()) event.preventDefault()
  })
}

function commonWebPreferences() {
  return {
    preload: path.join(__dirname, 'preload.cjs'),
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    spellcheck: false,
  }
}

function screenCaptureWorkerPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'app.asar.unpacked', 'electron', 'screen-capture-worker.ps1')
  }
  return path.join(__dirname, 'screen-capture-worker.ps1')
}

function failScreenCaptureRequests(error) {
  for (const pending of screenCaptureRequests.values()) {
    clearTimeout(pending.timer)
    pending.reject(error)
  }
  screenCaptureRequests.clear()
}

function startScreenCaptureWorker() {
  if (screenCaptureWorker && screenCaptureWorkerReady) return screenCaptureWorkerReady
  let readyResolve
  let readyReject
  let readySettled = false
  screenCaptureWorkerReady = new Promise((resolve, reject) => {
    readyResolve = resolve
    readyReject = reject
  })
  const worker = spawn('powershell.exe', [
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy', 'Bypass',
    '-File', screenCaptureWorkerPath(),
  ], {
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  screenCaptureWorker = worker
  const lines = readline.createInterface({ input: worker.stdout })
  lines.on('line', (line) => {
    let message
    try { message = JSON.parse(String(line).replace(/^\uFEFF/, '')) } catch { return }
    if (message.type === 'ready') {
      readySettled = true
      readyResolve(true)
      return
    }
    if (message.type !== 'capture' && message.type !== 'probe' && message.type !== 'affinity') return
    const pending = screenCaptureRequests.get(String(message.id))
    if (!pending) return
    screenCaptureRequests.delete(String(message.id))
    clearTimeout(pending.timer)
    if (message.ok) {
      if (message.type === 'affinity') {
        pending.resolve({ affinity: Number(message.affinity) || 0 })
        return
      }
      const parsedLuma = Number(message.luma)
      const result = {
        luma: Number.isFinite(parsedLuma) ? parsedLuma : 0.5,
        captureMs: Number(message.captureMs) || 0,
      }
      if (message.type === 'capture' && message.data) {
        result.dataUrl = `data:image/png;base64,${message.data}`
      }
      if (message.type === 'probe') result.foreground = String(message.foreground || '')
      if ((message.type === 'capture' && result.dataUrl) || message.type === 'probe') pending.resolve(result)
      else pending.reject(new Error('GDI capture returned no image'))
    } else {
      pending.reject(new Error(message.error || 'GDI capture failed'))
    }
  })
  const workerFailed = (error) => {
    if (screenCaptureWorker === worker) {
      screenCaptureWorker = null
      screenCaptureWorkerReady = null
    }
    if (!readySettled) {
      readySettled = true
      readyReject(error)
    }
    failScreenCaptureRequests(error)
  }
  worker.on('error', workerFailed)
  worker.on('exit', (code) => workerFailed(new Error(`Screen capture worker exited (${code ?? 'unknown'})`)))
  return screenCaptureWorkerReady
}

async function requestScreenWorker(type, bounds, extra = {}, timeoutMs = 2500) {
  await startScreenCaptureWorker()
  if (!screenCaptureWorker || screenCaptureWorker.killed) throw new Error('Screen capture worker is unavailable')
  const id = String(++screenCaptureRequestId)
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      screenCaptureRequests.delete(id)
      reject(new Error('Screen capture timed out'))
    }, timeoutMs)
    screenCaptureRequests.set(id, { resolve, reject, timer })
    screenCaptureWorker.stdin.write(`${JSON.stringify({
      type,
      id,
      x: Math.round(bounds.x),
      y: Math.round(bounds.y),
      width: Math.max(1, Math.round(bounds.width)),
      height: Math.max(1, Math.round(bounds.height)),
      ...extra,
    })}\n`)
  })
}

function captureScreenRegion(bounds) {
  return requestScreenWorker('capture', bounds)
}

function probeScreenContext(bounds) {
  return requestScreenWorker('probe', bounds, { margin: 0 }, 800)
}

function queryWindowDisplayAffinity(windowHandle) {
  return requestScreenWorker('affinity', { x: 0, y: 0, width: 1, height: 1 }, { windowHandle }, 800)
}

function dipBoundsToPhysical(bounds) {
  if (process.platform === 'win32' && typeof screen.dipToScreenPoint === 'function') {
    const topLeft = screen.dipToScreenPoint({ x: bounds.x, y: bounds.y })
    const bottomRight = screen.dipToScreenPoint({ x: bounds.x + bounds.width, y: bounds.y + bounds.height })
    return {
      x: topLeft.x,
      y: topLeft.y,
      width: Math.max(1, bottomRight.x - topLeft.x),
      height: Math.max(1, bottomRight.y - topLeft.y),
    }
  }
  const display = screen.getDisplayMatching(bounds)
  const scale = display.scaleFactor || 1
  return {
    x: display.bounds.x + Math.round((bounds.x - display.bounds.x) * scale),
    y: display.bounds.y + Math.round((bounds.y - display.bounds.y) * scale),
    width: Math.max(1, Math.round(bounds.width * scale)),
    height: Math.max(1, Math.round(bounds.height * scale)),
  }
}

function publishAmbientState(luma = 0.5) {
  if (!mainWindow || mainWindow.isDestroyed() || currentBackdropDark === null) return
  mainWindow.webContents.send('main:ambient', {
    luma,
    dark: currentBackdropDark,
  })
}

function nativeWindowHandleId(win) {
  try {
    const handle = win.getNativeWindowHandle()
    if (handle.length >= 8) return handle.readBigUInt64LE(0).toString()
    return String(handle.readUInt32LE(0))
  } catch {
    return ''
  }
}

async function verifyMainCaptureExclusion() {
  const win = mainWindow
  if (!SUPPORTS_NO_FLASH_CAPTURE || captureExclusionVerificationRunning || !win || win.isDestroyed() || !win.isVisible()) return false
  captureExclusionVerificationRunning = true
  try {
    const result = await queryWindowDisplayAffinity(mainNativeWindowHandle)
    if (win !== mainWindow || win.isDestroyed()) return false
    const wasVerified = captureExclusionVerified
    captureExclusionVerified = result.affinity === 0x11
    if (captureExclusionVerified && !wasVerified) scheduleMainBackdropRefresh(30)
    return captureExclusionVerified
  } catch {
    captureExclusionVerified = false
    return false
  } finally {
    captureExclusionVerificationRunning = false
  }
}

function scheduleCaptureExclusionVerification(delay = 70) {
  clearTimeout(captureExclusionVerificationTimer)
  captureExclusionVerificationTimer = setTimeout(() => {
    captureExclusionVerificationTimer = null
    void verifyMainCaptureExclusion()
  }, delay)
}

function applyCapturedAmbient(luma, publish = true) {
  const normalized = clampLuma(luma)
  const nextMode = advanceAmbientMode({
    dark: currentBackdropDark,
    candidateDark: null,
    candidateCount: 0,
  }, normalized, {
    darkEnter: AMBIENT_DARK_ENTER,
    lightEnter: AMBIENT_LIGHT_ENTER,
    confirmations: 1,
  })
  currentBackdropDark = nextMode.dark
  ambientCandidateDark = null
  ambientCandidateCount = 0
  if (publish) publishAmbientState(normalized)
  return currentBackdropDark
}

function handleAmbientProbe(probe) {
  const luma = clampLuma(probe?.luma)
  const nextForeground = advanceForegroundWindow({
    accepted: lastForegroundWindow,
    candidate: foregroundCandidateWindow,
    candidateCount: foregroundCandidateCount,
  }, probe?.foreground, mainNativeWindowHandle, 2)
  lastForegroundWindow = nextForeground.accepted
  foregroundCandidateWindow = nextForeground.candidate
  foregroundCandidateCount = nextForeground.candidateCount
  if (nextForeground.changed) {
    scheduleMainBackdropRefresh(130)
  }

  const nextMode = advanceAmbientMode({
    dark: currentBackdropDark,
    candidateDark: ambientCandidateDark,
    candidateCount: ambientCandidateCount,
  }, luma, {
    darkEnter: AMBIENT_DARK_ENTER,
    lightEnter: AMBIENT_LIGHT_ENTER,
    confirmations: 2,
  })
  currentBackdropDark = nextMode.dark
  ambientCandidateDark = nextMode.candidateDark
  ambientCandidateCount = nextMode.candidateCount
  if (nextMode.changed) {
    publishAmbientState(luma)
    const drawerIsActive = mainExpanded && mainWindow?.isFocused()
    if (!drawerIsActive && Date.now() - lastBackdropRefreshAt > 650) scheduleMainBackdropRefresh(70)
  }
}

function shouldProbeAmbient() {
  if (isQuitting || ambientPaused || !mainWindow || mainWindow.isDestroyed() || !mainWindow.isVisible()) return false
  if (!captureExclusionVerified) return false
  if (textCaptureRunning || backdropCaptureActive) return false
  if (captureFlowActive || captureCompleting || captureContext) return false
  if (captureWindow && !captureWindow.isDestroyed() && captureWindow.isVisible()) return false
  if (Date.now() - lastMainMoveAt < 360) return false
  return true
}

async function runAmbientMonitor() {
  ambientMonitorTimer = null
  if (ambientMonitorRunning || isQuitting) return
  ambientMonitorRunning = true
  try {
    if (shouldProbeAmbient()) {
      const bounds = mainWindow.getBounds()
      const physicalBounds = dipBoundsToPhysical(bounds)
      const probe = await probeScreenContext(physicalBounds)
      if (shouldProbeAmbient()) handleAmbientProbe(probe)
    }
  } catch {
    // The full backdrop path remains available if ambient probing is unavailable.
  } finally {
    ambientMonitorRunning = false
    if (!isQuitting && !ambientPaused) {
      ambientMonitorTimer = setTimeout(() => void runAmbientMonitor(), AMBIENT_POLL_MS)
    }
  }
}

function startAmbientMonitor() {
  if (ambientPaused || ambientMonitorTimer || ambientMonitorRunning) return
  ambientMonitorTimer = setTimeout(() => void runAmbientMonitor(), AMBIENT_POLL_MS)
}

function stopAmbientMonitor() {
  clearTimeout(ambientMonitorTimer)
  ambientMonitorTimer = null
}

function resetAmbientTracking() {
  lastForegroundWindow = ''
  foregroundCandidateWindow = ''
  foregroundCandidateCount = 0
  ambientCandidateDark = null
  ambientCandidateCount = 0
}

function pauseAmbientTracking(reason) {
  ambientPauseReasons.add(reason)
  ambientPaused = true
  clearTimeout(ambientResumeTimer)
  ambientResumeTimer = null
  stopAmbientMonitor()
  clearTimeout(backdropRefreshTimer)
  backdropRefreshTimer = null
  backdropRefreshDueAt = 0
  backdropRefreshPending = false
}

function resumeAmbientTracking(reason) {
  ambientPauseReasons.delete(reason)
  if (ambientPauseReasons.size || isQuitting) return
  clearTimeout(ambientResumeTimer)
  ambientResumeTimer = setTimeout(() => {
    ambientResumeTimer = null
    if (ambientPauseReasons.size || isQuitting) return
    ambientPaused = false
    resetAmbientTracking()
    backdropRefreshPending = false
    startAmbientMonitor()
    scheduleMainBackdropRefresh(80)
  }, 420)
}

function backdropRefreshBlocked() {
  return isQuitting || ambientPaused || textCaptureRunning || captureFlowActive || captureCompleting || Boolean(captureContext)
}

function flushPendingBackdropRefresh(delay = 0) {
  if (!backdropRefreshPending || backdropCaptureActive || backdropRefreshBlocked()) return
  backdropRefreshPending = false
  scheduleMainBackdropRefresh(delay)
}

async function refreshMainBackdrop({ skipMoveCooldown = false } = {}) {
  const win = mainWindow
  if (!win || win.isDestroyed()) return lastMainBackdrop
  if (!mainShouldBeVisible) return lastMainBackdrop
  if (win.isVisible() && !captureExclusionVerified) return lastMainBackdrop
  const moveAge = Date.now() - lastMainMoveAt
  if (!skipMoveCooldown && moveAge < 360) {
    scheduleMainBackdropRefresh(380 - moveAge)
    return lastMainBackdrop
  }
  if (backdropCaptureActive || backdropRefreshBlocked()) {
    backdropRefreshPending = true
    return lastMainBackdrop
  }
  backdropCaptureActive = true
  try {
    const bounds = win.getBounds()
    const captured = await captureScreenRegion(dipBoundsToPhysical(bounds))
    if (backdropRefreshBlocked()) {
      backdropRefreshPending = true
      return lastMainBackdrop
    }
    lastMainBackdrop = {
      dataUrl: captured.dataUrl,
      width: bounds.width,
      height: bounds.height,
      luma: captured.luma,
      captureMs: captured.captureMs,
    }
    lastMainBackdrop.dark = applyCapturedAmbient(captured.luma, false)
    backdropRetryCount = 0
    lastBackdropRefreshAt = Date.now()
    if (!win.isDestroyed()) win.webContents.send('main:backdrop', lastMainBackdrop)
    return lastMainBackdrop
  } catch {
    backdropRetryCount += 1
    if (!isQuitting && mainShouldBeVisible && backdropRetryCount <= 3) {
      scheduleMainBackdropRefresh(180 * (2 ** (backdropRetryCount - 1)))
    }
    return lastMainBackdrop
  } finally {
    backdropCaptureActive = false
    flushPendingBackdropRefresh()
  }
}

function scheduleMainBackdropRefresh(delay = 260) {
  const dueAt = Date.now() + Math.max(0, Number(delay) || 0)
  if (backdropRefreshTimer && backdropRefreshDueAt <= dueAt) return
  clearTimeout(backdropRefreshTimer)
  backdropRefreshDueAt = dueAt
  backdropRefreshTimer = setTimeout(() => {
    backdropRefreshTimer = null
    backdropRefreshDueAt = 0
    void refreshMainBackdrop()
  }, Math.max(0, dueAt - Date.now()))
}

function handleDisplayConfigurationChanged() {
  resetAmbientTracking()
  lastMainMoveAt = Date.now()
  scheduleEdgeSnap()
  scheduleMainBackdropRefresh(420)
}

function stopScreenCaptureWorker() {
  if (!screenCaptureWorker) return
  try { screenCaptureWorker.stdin.write('{"type":"quit"}\n') } catch { /* already closed */ }
  const worker = screenCaptureWorker
  setTimeout(() => { if (!worker.killed) worker.kill() }, 250)
}

function defaultMainBounds() {
  const point = screen.getCursorScreenPoint()
  const display = screen.getDisplayNearestPoint(point)
  const { workArea } = display
  return {
    x: workArea.x + workArea.width - MAIN_WIDTH - 18,
    y: workArea.y + workArea.height - COLLAPSED_HEIGHT - 18,
    width: MAIN_WIDTH,
    height: COLLAPSED_HEIGHT,
  }
}

function createMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) return mainWindow
  mainWindow = new BrowserWindow({
    ...defaultMainBounds(),
    title: APP_NAME,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    focusable: false,
    show: false,
    webPreferences: commonWebPreferences(),
  })
  mainWindow.setAlwaysOnTop(true, 'floating')
  if (SUPPORTS_NO_FLASH_CAPTURE) mainWindow.setContentProtection(true)
  mainNativeWindowHandle = nativeWindowHandleId(mainWindow)
  secureWindow(mainWindow)
  void mainWindow.loadURL(rendererUrl('main'))
  mainWindow.once('ready-to-show', async () => {
    await Promise.race([
      refreshMainBackdrop({ skipMoveCooldown: true }),
      new Promise((resolve) => setTimeout(resolve, 320)),
    ])
    if (mainShouldBeVisible) {
      mainWindow?.showInactive()
      scheduleCaptureExclusionVerification()
    }
    setTimeout(() => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('main:intro')
    }, 60)
  })
  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault()
      mainShouldBeVisible = false
      mainWindow?.hide()
    }
  })
  mainWindow.on('hide', () => {
    captureExclusionVerified = false
    clearTimeout(captureExclusionVerificationTimer)
    captureExclusionVerificationTimer = null
  })
  mainWindow.on('show', () => {
    if (SUPPORTS_NO_FLASH_CAPTURE) mainWindow?.setContentProtection(true)
    scheduleCaptureExclusionVerification(100)
  })
  mainWindow.on('move', () => {
    lastMainMoveAt = Date.now()
    scheduleEdgeSnap()
  })
  mainWindow.on('moved', () => {
    lastMainMoveAt = Date.now()
    scheduleMainBackdropRefresh(220)
  })
  mainWindow.on('closed', () => {
    clearTimeout(captureExclusionVerificationTimer)
    captureExclusionVerificationTimer = null
    captureExclusionVerified = false
    mainWindow = null
    mainNativeWindowHandle = ''
  })
  return mainWindow
}

function scheduleEdgeSnap() {
  if (!mainWindow || mainWindow.isDestroyed() || isSnapping) return
  clearTimeout(snapTimer)
  snapTimer = setTimeout(() => {
    if (!mainWindow || mainWindow.isDestroyed()) return
    const bounds = mainWindow.getBounds()
    const display = screen.getDisplayMatching(bounds)
    const area = display.workArea
    const margin = 12
    let x = Math.max(area.x + margin, Math.min(bounds.x, area.x + area.width - bounds.width - margin))
    let y = Math.max(area.y + margin, Math.min(bounds.y, area.y + area.height - bounds.height - margin))
    if (Math.abs(bounds.x - area.x) < 28) x = area.x + margin
    if (Math.abs(bounds.x + bounds.width - (area.x + area.width)) < 28) {
      x = area.x + area.width - bounds.width - margin
    }
    x = Math.round(x)
    y = Math.round(y)
    if (x === bounds.x && y === bounds.y) return
    isSnapping = true
    mainWindow.setPosition(x, y, false)
    setTimeout(() => { isSnapping = false }, 0)
  }, 180)
}

function setMainExpanded(expanded, requestedHeight = DRAWER_HEIGHT) {
  if (!mainWindow || mainWindow.isDestroyed()) return
  mainShouldBeVisible = true
  const current = mainWindow.getBounds()
  const display = screen.getDisplayMatching(current)
  const area = display.workArea
  const bottom = Math.min(current.y + current.height, area.y + area.height - 12)
  const height = expanded
    ? Math.min(Math.max(requestedHeight, COLLAPSED_HEIGHT), area.height - 24)
    : COLLAPSED_HEIGHT
  const y = Math.max(area.y + 12, bottom - height)
  mainExpanded = expanded
  mainWindow.setFocusable(expanded)
  mainWindow.setBounds({ x: current.x, y, width: MAIN_WIDTH, height }, false)
  scheduleMainBackdropRefresh(100)
  if (expanded) {
    mainWindow.show()
    mainWindow.focus()
  } else {
    mainWindow.showInactive()
  }
  scheduleCaptureExclusionVerification()
  mainWindow.webContents.send('main:expanded', expanded)
}

function showMain(expand = false) {
  const win = createMainWindow()
  mainShouldBeVisible = true
  if (expand) setMainExpanded(true)
  else {
    win.showInactive()
    scheduleCaptureExclusionVerification()
  }
}

function toggleMain() {
  const win = createMainWindow()
  if (mainShouldBeVisible) {
    mainShouldBeVisible = false
    win.hide()
  } else {
    mainShouldBeVisible = true
    win.showInactive()
    scheduleCaptureExclusionVerification()
    scheduleMainBackdropRefresh(80)
  }
}

function showSettings() {
  showMain(true)
  setMainExpanded(true, 650)
  mainWindow?.webContents.send('settings:open')
}

function trayIcon() {
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
      <defs><linearGradient id="g" x1="4" y1="2" x2="29" y2="30"><stop stop-color="#9183ff"/><stop offset="1" stop-color="#46c7c3"/></linearGradient></defs>
      <rect x="2" y="2" width="28" height="28" rx="9" fill="url(#g)"/>
      <path d="M9 10.5A3.5 3.5 0 0 1 12.5 7h7a3.5 3.5 0 0 1 3.5 3.5V15a3.5 3.5 0 0 1-3.5 3.5H17l-4.5 4v-4A3.5 3.5 0 0 1 9 15v-4.5Z" fill="white"/>
      <path d="M14.2 11.8c.2-1.2 1.2-2 2.5-2 1.4 0 2.5.9 2.5 2.1 0 1.8-2.1 1.8-2.1 3.2" fill="none" stroke="#6d72eb" stroke-width="1.6" stroke-linecap="round"/>
      <circle cx="17.1" cy="16.6" r=".8" fill="#6d72eb"/>
    </svg>`
  return nativeImage.createFromDataURL(`data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`).resize({ width: 20, height: 20 })
}

function createTray() {
  tray = new Tray(trayIcon())
  tray.setToolTip(`${APP_NAME} — 就绪`)
  const menu = Menu.buildFromTemplate([
    { label: '显示 / 隐藏 RandomAsk', click: toggleMain },
    { type: 'separator' },
    { label: '框选问', accelerator: 'Ctrl+Alt+S', click: () => void beginScreenCapture() },
    { label: '拖文本', accelerator: 'Ctrl+Alt+T', click: () => void beginTextCapture() },
    { label: '询问剪贴板', accelerator: 'Ctrl+Alt+V', click: () => deliverClipboardText() },
    { type: 'separator' },
    { label: '模型设置', click: showSettings },
    { label: '关于 RandomAsk', click: () => void dialog.showMessageBox({
      type: 'info',
      title: '关于 RandomAsk',
      message: 'RandomAsk 随问',
      detail: '在桌面边缘，随时向 AI 提问。\n版本 0.1.0',
      buttons: ['知道了'],
    }) },
    { type: 'separator' },
    { label: '退出', click: () => { isQuitting = true; app.quit() } },
  ])
  tray.setContextMenu(menu)
  tray.on('click', toggleMain)
}

function registerGlobalShortcuts() {
  globalShortcut.unregisterAll()
  if (!loadSettings().behavior.shortcuts) return
  globalShortcut.register('Control+Alt+A', toggleMain)
  globalShortcut.register('Control+Alt+S', () => void beginScreenCapture())
  globalShortcut.register('Control+Alt+T', () => void beginTextCapture())
  globalShortcut.register('Control+Alt+V', deliverClipboardText)
}

function sendActionError(message) {
  showMain(false)
  mainWindow?.webContents.send('action:error', String(message))
}

function deliverContent(content) {
  showMain(true)
  setMainExpanded(true, DRAWER_HEIGHT)
  mainWindow?.webContents.send('content:ready', content)
}

function contentAcquisitionBusy() {
  return textCaptureRunning || captureFlowActive || captureCompleting || Boolean(captureContext)
}

function deliverClipboardText() {
  if (contentAcquisitionBusy()) return { ok: false, reason: 'busy' }
  const text = clipboard.readText('clipboard').trim()
  if (!text) {
    sendActionError('剪贴板中没有可用文本')
    return { ok: false }
  }
  deliverContent({ kind: 'clipboard', text, createdAt: Date.now() })
  return { ok: true }
}

function textCaptureScriptPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'app.asar.unpacked', 'electron', 'text-capture.ps1')
  }
  return path.join(__dirname, 'text-capture.ps1')
}

function runPowerShellTextCapture() {
  return new Promise((resolve) => {
    execFile('powershell.exe', [
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy', 'Bypass',
      '-STA',
      '-File', textCaptureScriptPath(),
    ], { windowsHide: true, timeout: 18000 }, (error, stdout) => {
      const result = String(stdout || '').trim()
      if (result.startsWith('OK:')) resolve({ ok: true })
      else if (result === 'CANCEL' || error?.code === 2) resolve({ ok: false, reason: 'cancel' })
      else if (result === 'TIMEOUT' || error?.code === 4) resolve({ ok: false, reason: 'timeout' })
      else resolve({ ok: false, reason: 'empty' })
    })
  })
}

async function readClipboardWithRetry() {
  for (let index = 0; index < 5; index += 1) {
    const value = clipboard.readText('clipboard').trim()
    if (value) return value
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  return ''
}

function closeBadge() {
  if (badgeWindow && !badgeWindow.isDestroyed()) badgeWindow.close()
  badgeWindow = null
}

function createBadge(data, nearCursor = false) {
  closeBadge()
  const point = screen.getCursorScreenPoint()
  const display = screen.getDisplayNearestPoint(point)
  const area = display.workArea
  const width = data.state === 'armed' ? 276 : 206
  const height = 54
  let x = area.x + Math.round((area.width - width) / 2)
  let y = area.y + 28
  if (nearCursor) {
    x = Math.min(point.x + 14, area.x + area.width - width - 10)
    y = Math.min(point.y + 18, area.y + area.height - height - 10)
  }
  badgeWindow = new BrowserWindow({
    x, y, width, height,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    focusable: false,
    resizable: false,
    show: false,
    webPreferences: commonWebPreferences(),
  })
  badgeWindow.setAlwaysOnTop(true, 'screen-saver')
  badgeWindow.setIgnoreMouseEvents(true)
  secureWindow(badgeWindow)
  void badgeWindow.loadURL(rendererUrl('badge'))
  badgeWindow.once('ready-to-show', () => {
    badgeWindow?.showInactive()
    badgeWindow?.webContents.send('badge:data', data)
  })
  badgeWindow.on('closed', () => { badgeWindow = null })
}

async function beginTextCapture() {
  if (contentAcquisitionBusy()) return { ok: false, reason: 'busy' }
  textCaptureRunning = true
  mainWindow?.hide()
  createBadge({ state: 'armed', label: '拖选一段文字', hint: 'Esc 取消' })
  try {
    const result = await runPowerShellTextCapture()
    closeBadge()
    if (!result.ok) {
      if (result.reason !== 'cancel') {
        sendActionError(result.reason === 'timeout' ? '文本选取已超时，请重试' : '未读取到选中文本')
      } else {
        showMain(false)
      }
      return result
    }
    const text = await readClipboardWithRetry()
    if (!text) {
      sendActionError('未读取到选中文本')
      return { ok: false, reason: 'empty' }
    }
    createBadge({ state: 'captured', label: `已读取 ${text.length} 字` }, true)
    setTimeout(closeBadge, 1600)
    deliverContent({ kind: 'selection', text, createdAt: Date.now() })
    return { ok: true }
  } finally {
    textCaptureRunning = false
    flushPendingBackdropRefresh(100)
    if (mainShouldBeVisible) scheduleMainBackdropRefresh(140)
  }
}

async function captureDisplayImage(display) {
  const scale = display.scaleFactor || 1
  const sources = await desktopCapturer.getSources({
    types: ['screen'],
    thumbnailSize: {
      width: Math.max(1, Math.round(display.bounds.width * scale)),
      height: Math.max(1, Math.round(display.bounds.height * scale)),
    },
    fetchWindowIcons: false,
  })
  const matched = sources.find((source) => String(source.display_id) === String(display.id))
  if (matched) return matched
  if (sources.length === 1) return sources[0]
  throw new Error('Unable to match the active display')
}

function restoreCapturedWindows(showMainWindow = true) {
  const hidden = captureContext?.hiddenWindows || []
  for (const win of hidden) {
    if (!win.isDestroyed() && win !== mainWindow) win.show()
  }
  if (showMainWindow && mainShouldBeVisible && mainWindow && !mainWindow.isDestroyed()) mainWindow.showInactive()
}

function ensureCaptureWindow(display) {
  if (captureWindow && !captureWindow.isDestroyed()) {
    captureWindow.setBounds(display.bounds, false)
    return captureWindowReady || Promise.resolve(captureWindow)
  }
  captureWindow = new BrowserWindow({
    ...display.bounds,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    show: false,
    webPreferences: commonWebPreferences(),
  })
  captureWindow.setAlwaysOnTop(true, 'screen-saver')
  secureWindow(captureWindow)
  captureWindowReady = new Promise((resolve, reject) => {
    captureWindow.webContents.once('did-finish-load', () => resolve(captureWindow))
    captureWindow.webContents.once('did-fail-load', (_event, code, description) => reject(new Error(`${description} (${code})`)))
  })
  void captureWindow.loadURL(rendererUrl('capture'))
  captureWindow.on('close', (event) => {
    if (isQuitting) return
    event.preventDefault()
    captureWindow?.hide()
    if (captureContext && !captureCompleting) {
      captureCompleting = true
      restoreCapturedWindows(true)
      captureContext = null
      captureFlowActive = false
      captureCompleting = false
      flushPendingBackdropRefresh(100)
      if (mainShouldBeVisible) scheduleMainBackdropRefresh(140)
    }
  })
  captureWindow.on('closed', () => {
    if (captureContext && !isQuitting) restoreCapturedWindows(true)
    captureWindow = null
    captureWindowReady = null
    captureContext = null
    captureFlowActive = false
    captureCompleting = false
    if (!isQuitting && mainShouldBeVisible) scheduleMainBackdropRefresh(140)
  })
  return captureWindowReady
}

async function beginScreenCapture() {
  if (contentAcquisitionBusy() || (captureWindow && !captureWindow.isDestroyed() && captureWindow.isVisible())) {
    return { ok: false, reason: 'busy' }
  }
  captureFlowActive = true
  closeBadge()
  const hiddenWindows = BrowserWindow.getAllWindows().filter((win) => win.isVisible())
  captureContext = {
    display: null,
    hiddenWindows,
    finishing: false,
  }
  try {
    for (const win of hiddenWindows) win.hide()
    await new Promise((resolve) => setTimeout(resolve, 42))
    const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint())
    captureContext.display = display
    await ensureCaptureWindow(display)
    captureWindow.webContents.send('capture:reset')
    captureWindow.show()
    captureWindow.focus()
    return { ok: true }
  } catch (error) {
    captureCompleting = true
    captureWindow?.hide()
    restoreCapturedWindows(true)
    captureContext = null
    captureFlowActive = false
    captureCompleting = false
    flushPendingBackdropRefresh(100)
    sendActionError(error?.message || '无法开始框选')
    return { ok: false }
  }
}

async function finishCapture(rect) {
  if (!captureContext?.display || captureContext.finishing) return { ok: false }
  const cssWidth = captureContext.display.bounds.width
  const cssHeight = captureContext.display.bounds.height
  const left = Math.max(0, Math.min(Number(rect?.x) || 0, cssWidth))
  const top = Math.max(0, Math.min(Number(rect?.y) || 0, cssHeight))
  const width = Math.max(0, Math.min(Number(rect?.width) || 0, cssWidth - left))
  const height = Math.max(0, Math.min(Number(rect?.height) || 0, cssHeight - top))
  if (width < 8 || height < 8) return { ok: false, reason: 'small' }
  captureContext.finishing = true
  const context = captureContext
  captureCompleting = true
  captureWindow?.hide()
  await new Promise((resolve) => setTimeout(resolve, 40))
  try {
    let dataUrl = ''
    let physicalSize = null
    try {
      const absoluteDipBounds = {
        x: context.display.bounds.x + left,
        y: context.display.bounds.y + top,
        width,
        height,
      }
      const physicalBounds = dipBoundsToPhysical(absoluteDipBounds)
      const captured = await captureScreenRegion(physicalBounds)
      dataUrl = captured.dataUrl
      physicalSize = { width: physicalBounds.width, height: physicalBounds.height }
    } catch {
      const source = await captureDisplayImage(context.display)
      if (!source || source.thumbnail.isEmpty()) throw new Error('无法读取当前屏幕')
      const imageSize = source.thumbnail.getSize()
      const scaleX = imageSize.width / cssWidth
      const scaleY = imageSize.height / cssHeight
      const crop = {
        x: Math.max(0, Math.round(left * scaleX)),
        y: Math.max(0, Math.round(top * scaleY)),
        width: Math.max(1, Math.min(imageSize.width, Math.round(width * scaleX))),
        height: Math.max(1, Math.min(imageSize.height, Math.round(height * scaleY))),
      }
      if (crop.x + crop.width > imageSize.width) crop.width = imageSize.width - crop.x
      if (crop.y + crop.height > imageSize.height) crop.height = imageSize.height - crop.y
      dataUrl = source.thumbnail.crop(crop).toDataURL()
      physicalSize = { width: crop.width, height: crop.height }
    }
    restoreCapturedWindows(false)
    deliverContent({ kind: 'image', imageDataUrl: dataUrl, size: physicalSize, createdAt: Date.now() })
    return { ok: true }
  } catch (error) {
    restoreCapturedWindows(true)
    sendActionError(error?.message || '截图失败，请重试')
    return { ok: false }
  } finally {
    if (captureContext === context) captureContext = null
    captureFlowActive = false
    captureCompleting = false
    flushPendingBackdropRefresh(100)
    if (mainShouldBeVisible) scheduleMainBackdropRefresh(140)
  }
}

function cancelCapture() {
  captureCompleting = true
  captureWindow?.hide()
  restoreCapturedWindows(true)
  captureContext = null
  captureFlowActive = false
  captureCompleting = false
  flushPendingBackdropRefresh(100)
  if (mainShouldBeVisible) scheduleMainBackdropRefresh(140)
}

function publicSession(session) {
  return {
    id: session.id,
    kind: session.kind,
    question: session.question,
    sourceText: session.sourceText,
    imageDataUrl: session.imageDataUrl,
    model: session.model,
    answer: session.answer,
    status: session.status,
    error: session.error,
    createdAt: session.createdAt,
  }
}

function createAnswerWindow(session) {
  const currentMainBounds = mainWindow?.getBounds()
  const display = screen.getDisplayMatching(currentMainBounds || defaultMainBounds())
  const area = display.workArea
  const width = 690
  const height = Math.min(760, area.height - 48)
  const win = new BrowserWindow({
    x: Math.max(area.x + 20, area.x + area.width - width - 24),
    y: Math.max(area.y + 24, area.y + area.height - height - 24),
    width,
    height,
    minWidth: 480,
    minHeight: 420,
    title: `回答 · ${APP_NAME}`,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: true,
    show: false,
    webPreferences: commonWebPreferences(),
  })
  secureWindow(win)
  answerWindows.set(session.id, win)
  void win.loadURL(rendererUrl('answer', { id: session.id }))
  win.once('ready-to-show', () => win.show())
  win.on('closed', () => answerWindows.delete(session.id))
  return win
}

function publishSession(session) {
  const win = answerWindows.get(session.id)
  if (win && !win.isDestroyed()) win.webContents.send('answer:update', publicSession(session))
}

function friendlyNetworkError(error) {
  const message = String(error?.message || '')
  if (/abort/i.test(message)) return '请求已停止。'
  if (/fetch failed|ECONNREFUSED|ENOTFOUND|network/i.test(message)) return '无法连接到 AI 服务，请检查网络和 Base URL。'
  return message || '请求失败，请稍后重试。'
}

async function startAnswer(session) {
  if (session.status !== 'queued') return
  const config = loadSettings()[session.kind === 'image' ? 'vision' : 'text']
  session.model = config.model
  session.status = 'streaming'
  session.error = ''
  publishSession(session)
  const controller = new AbortController()
  abortControllers.set(session.id, controller)
  const timeout = setTimeout(() => controller.abort('timeout'), 300000)
  try {
    if (!config.model) throw new Error(`尚未配置${session.kind === 'image' ? '视觉' : '文本'}模型`)
    const endpoint = normalizeEndpoint(config.baseUrl)
    const payload = buildChatPayload({
      kind: session.kind,
      question: session.question,
      text: session.sourceText,
      imageDataUrl: session.imageDataUrl,
    }, config.model)
    const headers = { 'Content-Type': 'application/json', Accept: 'text/event-stream, application/json' }
    if (config.apiKey) headers.Authorization = `Bearer ${config.apiKey}`
    const response = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    if (!response.ok) {
      let body = null
      try { body = await response.json() } catch { body = null }
      throw new Error(describeHttpError(response.status, body))
    }

    const contentType = response.headers.get('content-type') || ''
    if (contentType.includes('application/json')) {
      const data = await response.json()
      session.answer = data?.choices?.[0]?.message?.content || ''
      publishSession(session)
    } else if (response.body) {
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let publishTimer = null
      const schedulePublish = () => {
        if (publishTimer) return
        publishTimer = setTimeout(() => {
          publishTimer = null
          publishSession(session)
        }, 32)
      }
      const parser = createSSEParser((eventData) => {
        if (eventData === '[DONE]') return
        try {
          const data = JSON.parse(eventData)
          const delta = extractDelta(data)
          if (delta) {
            session.answer += delta
            schedulePublish()
          }
        } catch {
          // Ignore provider keep-alive or non-JSON SSE frames.
        }
      })
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        parser.push(decoder.decode(value, { stream: true }))
      }
      parser.push(decoder.decode())
      parser.flush()
      if (publishTimer) clearTimeout(publishTimer)
    }
    session.status = 'completed'
    publishSession(session)
    const win = answerWindows.get(session.id)
    if (Notification.isSupported() && (!win || !win.isFocused())) {
      const notification = new Notification({ title: APP_NAME, body: '回答已生成完成' })
      notification.on('click', () => { win?.show(); win?.focus() })
      notification.show()
    }
  } catch (error) {
    if (controller.signal.aborted) {
      session.status = 'stopped'
      session.error = controller.signal.reason === 'timeout' ? '响应超时，请重试。' : ''
    } else {
      session.status = 'error'
      session.error = friendlyNetworkError(error)
    }
    publishSession(session)
  } finally {
    clearTimeout(timeout)
    abortControllers.delete(session.id)
    tray?.setToolTip(`${APP_NAME} — 就绪`)
  }
}

function submitAnswer(input) {
  const kind = input?.kind === 'image' ? 'image' : 'text'
  if (kind === 'image' && !String(input?.imageDataUrl || '').startsWith('data:image/')) {
    throw new Error('没有可发送的截图')
  }
  if (kind === 'text' && !String(input?.text || '').trim()) throw new Error('没有可发送的文本')
  const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
  const config = loadSettings()[kind === 'image' ? 'vision' : 'text']
  const session = {
    id,
    kind,
    question: String(input?.question || '').trim(),
    sourceText: kind === 'text' ? String(input.text).slice(0, 120000) : '',
    imageDataUrl: kind === 'image' ? String(input.imageDataUrl) : '',
    model: config.model,
    answer: '',
    status: 'queued',
    error: '',
    createdAt: Date.now(),
  }
  answerSessions.set(id, session)
  createAnswerWindow(session)
  tray?.setToolTip(`${APP_NAME} — 正在生成回答`)
  if (answerSessions.size > 30) {
    const first = answerSessions.keys().next().value
    if (first && first !== id) answerSessions.delete(first)
  }
  return { id }
}

async function testModelConnection(kind, candidate) {
  const config = cleanModelConfig(candidate, kind === 'vision' ? DEFAULT_SETTINGS.vision : DEFAULT_SETTINGS.text)
  if (!config.baseUrl || !config.model) return { ok: false, message: '请填写 Base URL 和模型名称' }
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 15000)
  try {
    const endpoint = normalizeEndpoint(config.baseUrl)
    const onePixel = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
    const payload = buildChatPayload(kind === 'vision'
      ? { kind: 'image', question: '只回答 OK', imageDataUrl: onePixel }
      : { kind: 'text', question: '只回答 OK', text: '连接测试' }, config.model)
    payload.stream = false
    payload.max_tokens = 8
    const headers = { 'Content-Type': 'application/json' }
    if (config.apiKey) headers.Authorization = `Bearer ${config.apiKey}`
    const response = await fetch(endpoint, {
      method: 'POST', headers, body: JSON.stringify(payload), signal: controller.signal,
    })
    if (!response.ok) {
      let body = null
      try { body = await response.json() } catch { body = null }
      return { ok: false, message: describeHttpError(response.status, body) }
    }
    return { ok: true, message: '连接成功' }
  } catch (error) {
    return { ok: false, message: friendlyNetworkError(error) }
  } finally {
    clearTimeout(timer)
  }
}

function setupIpc() {
  ipcMain.handle('settings:get', () => loadSettings())
  ipcMain.handle('settings:save', (_event, value) => saveSettings(value))
  ipcMain.handle('settings:test', (_event, kind, value) => testModelConnection(kind, value))
  ipcMain.handle('main:set-expanded', (_event, expanded, height) => setMainExpanded(Boolean(expanded), Number(height) || DRAWER_HEIGHT))
  ipcMain.handle('main:get-backdrop', () => lastMainBackdrop)
  ipcMain.handle('action:clipboard', () => deliverClipboardText())
  ipcMain.handle('action:grab-text', () => beginTextCapture())
  ipcMain.handle('action:start-capture', () => beginScreenCapture())
  ipcMain.handle('capture:get-context', () => captureContext ? {
    imageDataUrl: '',
    live: true,
    width: captureContext.display.bounds.width,
    height: captureContext.display.bounds.height,
    scaleFactor: captureContext.display.scaleFactor,
  } : null)
  ipcMain.handle('capture:complete', (_event, rect) => finishCapture(rect))
  ipcMain.handle('capture:cancel', () => cancelCapture())
  ipcMain.handle('ask:submit', (_event, input) => submitAnswer(input))
  ipcMain.handle('answer:get', (_event, id) => {
    const session = answerSessions.get(String(id))
    if (!session) return null
    if (session.status === 'queued') setImmediate(() => void startAnswer(session))
    return publicSession(session)
  })
  ipcMain.handle('answer:stop', (_event, id) => {
    abortControllers.get(String(id))?.abort('user')
    return { ok: true }
  })
  ipcMain.handle('answer:retry', (_event, id) => {
    const session = answerSessions.get(String(id))
    if (!session || session.status === 'streaming') return { ok: false }
    session.answer = ''
    session.error = ''
    session.status = 'queued'
    publishSession(session)
    setImmediate(() => void startAnswer(session))
    return { ok: true }
  })
  ipcMain.handle('clipboard:write', (_event, text) => {
    clipboard.writeText(String(text || ''))
    return { ok: true }
  })
  ipcMain.handle('app:open-external', (_event, url) => {
    if (/^https?:\/\//i.test(String(url))) void shell.openExternal(String(url))
  })
  ipcMain.handle('app:show-main', () => showMain(true))
  ipcMain.handle('app:open-settings', () => showSettings())
  ipcMain.handle('window:control', (event, action) => {
    const win = BrowserWindow.fromWebContents(event.sender)
    if (!win) return null
    if (action === 'minimize') win.minimize()
    if (action === 'close') {
      if (win === mainWindow) {
        mainShouldBeVisible = false
        win.hide()
      }
      else win.close()
    }
    if (action === 'toggle-pin') {
      const next = !win.isAlwaysOnTop()
      win.setAlwaysOnTop(next, 'floating')
      return { pinned: next }
    }
    return null
  })
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => showMain(false))
  app.whenReady().then(() => {
    app.setName(APP_NAME)
    setupIpc()
    void startScreenCaptureWorker().catch(() => {})
    createMainWindow()
    void ensureCaptureWindow(screen.getPrimaryDisplay()).catch(() => {})
    startAmbientMonitor()
    powerMonitor.on('suspend', () => pauseAmbientTracking('suspend'))
    powerMonitor.on('resume', () => resumeAmbientTracking('suspend'))
    powerMonitor.on('lock-screen', () => pauseAmbientTracking('lock-screen'))
    powerMonitor.on('unlock-screen', () => resumeAmbientTracking('lock-screen'))
    screen.on('display-metrics-changed', handleDisplayConfigurationChanged)
    screen.on('display-added', handleDisplayConfigurationChanged)
    screen.on('display-removed', handleDisplayConfigurationChanged)
    createTray()
    registerGlobalShortcuts()
  })
  app.on('activate', () => showMain(false))
  app.on('before-quit', () => {
    isQuitting = true
    stopAmbientMonitor()
    clearTimeout(ambientResumeTimer)
    clearTimeout(backdropRefreshTimer)
    clearTimeout(captureExclusionVerificationTimer)
    backdropRefreshTimer = null
    backdropRefreshPending = false
    stopScreenCaptureWorker()
  })
  app.on('will-quit', () => globalShortcut.unregisterAll())
  app.on('window-all-closed', () => {
    // The tray owns the application lifecycle on Windows.
  })
}
