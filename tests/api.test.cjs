'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const {
  normalizeEndpoint,
  buildChatPayload,
  createSSEParser,
  extractDelta,
  describeHttpError,
} = require('../electron/lib/api.cjs')

test('normalizes OpenAI-compatible Base URLs', () => {
  assert.equal(normalizeEndpoint('https://api.openai.com/v1'), 'https://api.openai.com/v1/chat/completions')
  assert.equal(normalizeEndpoint('https://example.com'), 'https://example.com/v1/chat/completions')
  assert.equal(normalizeEndpoint('http://127.0.0.1:11434/v1/'), 'http://127.0.0.1:11434/v1/chat/completions')
  assert.equal(normalizeEndpoint('https://example.com/api/chat/completions'), 'https://example.com/api/chat/completions')
  assert.throws(() => normalizeEndpoint('file:///tmp/model'), /http/)
})

test('keeps text and vision payloads structurally separate', () => {
  const text = buildChatPayload({ kind: 'text', question: '翻译', text: 'hello' }, 'text-sentinel')
  const vision = buildChatPayload({ kind: 'image', question: '解释', imageDataUrl: 'data:image/png;base64,AA==' }, 'vision-sentinel')

  assert.equal(text.model, 'text-sentinel')
  assert.equal(typeof text.messages[0].content, 'string')
  assert.match(text.messages[0].content, /hello/)
  assert.equal(vision.model, 'vision-sentinel')
  assert.ok(Array.isArray(vision.messages[0].content))
  assert.equal(vision.messages[0].content[1].type, 'image_url')
  assert.equal(vision.messages[0].content[1].image_url.url, 'data:image/png;base64,AA==')
})

test('uses safe default prompts and rejects empty source content', () => {
  const text = buildChatPayload({ kind: 'text', text: '内容' }, 'm')
  const image = buildChatPayload({ kind: 'image', imageDataUrl: 'data:image/jpeg;base64,AA==' }, 'm')
  assert.match(text.messages[0].content, /请理解以下内容/)
  assert.match(image.messages[0].content[0].text, /请分析这张截图/)
  assert.throws(() => buildChatPayload({ kind: 'text', text: '   ' }, 'm'), /没有可发送的文本/)
})

test('parses fragmented SSE frames without dropping content', () => {
  const events = []
  const parser = createSSEParser((value) => events.push(value))
  parser.push('data: {"choices":[{"delta":{"content":"你"}}]}\r\n')
  parser.push('\r\ndata: {"choices":[{"delta":{"content":"好"}}]}\n\n')
  parser.push('data: [DONE]\n\n')
  parser.flush()
  assert.equal(events.length, 3)
  assert.equal(extractDelta(JSON.parse(events[0])), '你')
  assert.equal(extractDelta(JSON.parse(events[1])), '好')
  assert.equal(events[2], '[DONE]')
})

test('maps common HTTP failures to actionable messages', () => {
  assert.match(describeHttpError(401, {}), /API Key/)
  assert.match(describeHttpError(404, {}), /模型不存在/)
  assert.match(describeHttpError(429, {}), /频繁/)
  assert.match(describeHttpError(503, {}), /暂时不可用/)
})
