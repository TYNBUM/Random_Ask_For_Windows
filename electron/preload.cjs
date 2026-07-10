'use strict'

const { contextBridge, ipcRenderer } = require('electron')

function on(channel, callback) {
  const listener = (_event, value) => callback(value)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

contextBridge.exposeInMainWorld('randomAsk', {
  settings: {
    get: () => ipcRenderer.invoke('settings:get'),
    save: (value) => ipcRenderer.invoke('settings:save', value),
    test: (kind, value) => ipcRenderer.invoke('settings:test', kind, value),
  },
  main: {
    setExpanded: (expanded, height) => ipcRenderer.invoke('main:set-expanded', expanded, height),
    getBackdrop: () => ipcRenderer.invoke('main:get-backdrop'),
    onIntro: (callback) => on('main:intro', callback),
    onBackdrop: (callback) => on('main:backdrop', callback),
    onAmbient: (callback) => on('main:ambient', callback),
    onExpanded: (callback) => on('main:expanded', callback),
    onOpenSettings: (callback) => on('settings:open', callback),
    onContent: (callback) => on('content:ready', callback),
    onError: (callback) => on('action:error', callback),
  },
  actions: {
    capture: () => ipcRenderer.invoke('action:start-capture'),
    grabText: () => ipcRenderer.invoke('action:grab-text'),
    clipboard: () => ipcRenderer.invoke('action:clipboard'),
  },
  capture: {
    getContext: () => ipcRenderer.invoke('capture:get-context'),
    onReset: (callback) => on('capture:reset', callback),
    complete: (rect) => ipcRenderer.invoke('capture:complete', rect),
    cancel: () => ipcRenderer.invoke('capture:cancel'),
  },
  ask: {
    submit: (input) => ipcRenderer.invoke('ask:submit', input),
  },
  answer: {
    get: (id) => ipcRenderer.invoke('answer:get', id),
    stop: (id) => ipcRenderer.invoke('answer:stop', id),
    retry: (id) => ipcRenderer.invoke('answer:retry', id),
    onUpdate: (callback) => on('answer:update', callback),
  },
  clipboard: {
    write: (text) => ipcRenderer.invoke('clipboard:write', text),
  },
  app: {
    showMain: () => ipcRenderer.invoke('app:show-main'),
    openSettings: () => ipcRenderer.invoke('app:open-settings'),
    openExternal: (url) => ipcRenderer.invoke('app:open-external', url),
  },
  window: {
    control: (action) => ipcRenderer.invoke('window:control', action),
  },
  badge: {
    onData: (callback) => on('badge:data', callback),
  },
})
