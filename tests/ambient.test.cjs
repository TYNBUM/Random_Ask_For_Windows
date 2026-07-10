'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const {
  clampLuma,
  advanceAmbientMode,
  advanceForegroundWindow,
} = require('../electron/lib/ambient.cjs')

test('ambient luma is clamped and initialized deterministically', () => {
  assert.equal(clampLuma(-1), 0)
  assert.equal(clampLuma(2), 1)
  assert.equal(advanceAmbientMode({}, 0.2).dark, true)
  assert.equal(advanceAmbientMode({}, 0.8).dark, false)
})

test('ambient mode requires two confirmed samples and uses hysteresis', () => {
  let state = { dark: true, candidateDark: null, candidateCount: 0 }
  state = advanceAmbientMode(state, 0.7)
  assert.equal(state.dark, true)
  assert.equal(state.candidateCount, 1)
  state = advanceAmbientMode(state, 0.72)
  assert.equal(state.dark, false)
  assert.equal(state.changed, true)

  state = advanceAmbientMode(state, 0.51)
  assert.equal(state.dark, false, 'middle luma should retain the current mode')
  state = advanceAmbientMode(state, 0.3)
  assert.equal(state.dark, false)
  state = advanceAmbientMode(state, 0.28)
  assert.equal(state.dark, true)
})

test('ambient confirmation is reset by a sample inside the deadband', () => {
  let state = { dark: true, candidateDark: null, candidateCount: 0 }
  state = advanceAmbientMode(state, 0.75)
  assert.equal(state.candidateCount, 1)
  state = advanceAmbientMode(state, 0.54)
  assert.equal(state.dark, true)
  assert.equal(state.candidateCount, 0)
  state = advanceAmbientMode(state, 0.76)
  assert.equal(state.dark, true, 'a non-consecutive sample must not switch the material')
})

test('ambient thresholds are inclusive and do not repeat change events', () => {
  let light = { dark: false, candidateDark: null, candidateCount: 0 }
  light = advanceAmbientMode(light, 0.42)
  assert.equal(light.dark, false)
  light = advanceAmbientMode(light, 0.42)
  assert.equal(light.dark, true)
  assert.equal(light.changed, true)
  light = advanceAmbientMode(light, 0.2)
  assert.equal(light.changed, false)

  let dark = { dark: true, candidateDark: null, candidateCount: 0 }
  dark = advanceAmbientMode(dark, 0.62)
  assert.equal(dark.dark, true)
  dark = advanceAmbientMode(dark, 0.62)
  assert.equal(dark.dark, false)
})

test('foreground identity must remain stable for two samples', () => {
  let state = { accepted: '100', candidate: '', candidateCount: 0 }
  state = advanceForegroundWindow(state, '200', '999')
  assert.equal(state.accepted, '100')
  assert.equal(state.changed, false)
  state = advanceForegroundWindow(state, '300', '999')
  assert.equal(state.accepted, '100', 'a transient foreground window replaces the candidate')
  state = advanceForegroundWindow(state, '300', '999')
  assert.equal(state.accepted, '300')
  assert.equal(state.changed, true)

  state = advanceForegroundWindow(state, '999', '999')
  assert.equal(state.accepted, '300', 'the RandomAsk window is ignored')
})

test('foreground baseline does not emit a refresh and interruptions reset its candidate', () => {
  let state = { accepted: '', candidate: '', candidateCount: 0 }
  state = advanceForegroundWindow(state, '100')
  state = advanceForegroundWindow(state, '100')
  assert.equal(state.accepted, '100')
  assert.equal(state.changed, false)

  state = advanceForegroundWindow(state, '200')
  state = advanceForegroundWindow(state, '')
  state = advanceForegroundWindow(state, '200')
  assert.equal(state.accepted, '100')
  assert.equal(state.candidateCount, 1)
})
