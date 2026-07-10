'use strict'

const DEFAULT_TEXT_PROMPT = '请理解以下内容；若其中包含问题，请直接回答，否则给出简洁解释。'
const DEFAULT_VISION_PROMPT = '请分析这张截图；若其中包含问题，请直接解答。'

function normalizeEndpoint(baseUrl) {
  const value = String(baseUrl || '').trim().replace(/\/+$/, '')
  if (!value) throw new Error('Base URL 不能为空')
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    throw new Error('Base URL 格式不正确')
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('Base URL 仅支持 http 或 https')
  }
  if (/\/chat\/completions$/i.test(parsed.pathname)) return parsed.toString().replace(/\/$/, '')
  if (/\/v1$/i.test(parsed.pathname)) return `${parsed.toString().replace(/\/$/, '')}/chat/completions`
  return `${parsed.toString().replace(/\/$/, '')}/v1/chat/completions`
}

function buildChatPayload(input, model) {
  const question = String(input.question || '').trim()
  if (input.kind === 'image') {
    if (!input.imageDataUrl || !String(input.imageDataUrl).startsWith('data:image/')) {
      throw new Error('没有可发送的截图')
    }
    return {
      model,
      stream: true,
      messages: [{
        role: 'user',
        content: [
          { type: 'text', text: question || DEFAULT_VISION_PROMPT },
          { type: 'image_url', image_url: { url: input.imageDataUrl, detail: 'auto' } },
        ],
      }],
    }
  }

  const text = String(input.text || '').trim()
  if (!text) throw new Error('没有可发送的文本')
  const instruction = question || DEFAULT_TEXT_PROMPT
  return {
    model,
    stream: true,
    messages: [{
      role: 'user',
      content: `${instruction}\n\n---\n${text}`,
    }],
  }
}

function createSSEParser(onEvent) {
  let buffer = ''
  return {
    push(chunk) {
      buffer += chunk.replace(/\r\n/g, '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const data = block
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart())
          .join('\n')
        if (data) onEvent(data)
        boundary = buffer.indexOf('\n\n')
      }
    },
    flush() {
      const rest = buffer.trim()
      buffer = ''
      if (!rest) return
      const data = rest
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
        .join('\n')
      if (data) onEvent(data)
    },
  }
}

function extractDelta(payload) {
  const delta = payload?.choices?.[0]?.delta?.content
  if (typeof delta === 'string') return delta
  if (Array.isArray(delta)) {
    return delta.map((item) => typeof item === 'string' ? item : (item?.text || '')).join('')
  }
  return ''
}

function describeHttpError(status, body) {
  const message = body?.error?.message || body?.message || ''
  if (status === 401 || status === 403) return 'API Key 无效或没有访问权限，请检查模型设置。'
  if (status === 404) return '接口地址或模型不存在，请检查 Base URL 与模型名称。'
  if (status === 429) return '请求过于频繁或额度不足，请稍后重试。'
  if (status >= 500) return 'AI 服务暂时不可用，请稍后重试。'
  return message ? `请求失败：${String(message).slice(0, 240)}` : `请求失败（HTTP ${status}）`
}

module.exports = {
  DEFAULT_TEXT_PROMPT,
  DEFAULT_VISION_PROMPT,
  normalizeEndpoint,
  buildChatPayload,
  createSSEParser,
  extractDelta,
  describeHttpError,
}
