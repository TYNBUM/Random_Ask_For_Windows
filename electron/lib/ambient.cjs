'use strict'

function clampLuma(value) {
  const parsed = Number(value)
  return Math.max(0, Math.min(1, Number.isFinite(parsed) ? parsed : 0.5))
}

function advanceAmbientMode(state, rawLuma, options = {}) {
  const darkEnter = options.darkEnter ?? 0.42
  const lightEnter = options.lightEnter ?? 0.62
  const confirmations = options.confirmations ?? 2
  const luma = clampLuma(rawLuma)
  const hadMode = typeof state?.dark === 'boolean'
  let dark = hadMode ? state.dark : luma < 0.52
  let candidateDark = state?.candidateDark ?? null
  let candidateCount = Number(state?.candidateCount) || 0
  let desiredDark = null

  if (dark && luma >= lightEnter) desiredDark = false
  if (!dark && luma <= darkEnter) desiredDark = true

  let changed = !hadMode
  if (desiredDark === null) {
    candidateDark = null
    candidateCount = 0
  } else {
    if (candidateDark === desiredDark) candidateCount += 1
    else {
      candidateDark = desiredDark
      candidateCount = 1
    }
    if (candidateCount >= confirmations) {
      dark = desiredDark
      candidateDark = null
      candidateCount = 0
      changed = true
    }
  }

  return { dark, candidateDark, candidateCount, changed, luma }
}

function advanceForegroundWindow(state, rawWindow, ownWindow = '', confirmations = 2) {
  const foreground = String(rawWindow || '')
  const accepted = String(state?.accepted || '')
  let candidate = String(state?.candidate || '')
  let candidateCount = Number(state?.candidateCount) || 0

  if (!foreground || foreground === String(ownWindow || '') || foreground === accepted) {
    return { accepted, candidate: '', candidateCount: 0, changed: false }
  }

  if (candidate === foreground) candidateCount += 1
  else {
    candidate = foreground
    candidateCount = 1
  }

  if (candidateCount >= Math.max(1, Number(confirmations) || 1)) {
    return { accepted: foreground, candidate: '', candidateCount: 0, changed: Boolean(accepted) }
  }

  return { accepted, candidate, candidateCount, changed: false }
}

module.exports = { clampLuma, advanceAmbientMode, advanceForegroundWindow }
