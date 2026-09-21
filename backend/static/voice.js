// The call engine: audio worklets and the websocket session.
//
// The worklets and the delta-joining are taken from the AssemblyAI starter's
// deployment/browser/app.js unchanged, because they are carefully tuned. What
// is different here is the boundary: this file owns no DOM. It reports what
// happens through VoiceCall.on.*, and the page decides what to draw.
const WIRE_RATE = 24_000

// Scratch buffers are reused: allocating on the audio thread causes glitches.
const CAPTURE_WORKLET = `
  class CaptureProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      this._ratio = sampleRate / ${WIRE_RATE};
      this._pos = 0;
      this._prev = 0;
      this._src = null;
      this._out = null;
    }
    _toPcm(samples, len) {
      const pcm = new Int16Array(len);
      for (let i = 0; i < len; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      return pcm;
    }
    process(inputs) {
      const ch = inputs[0]?.[0];
      if (!ch) return true;
      if (this._ratio === 1) {
        const pcm = this._toPcm(ch, ch.length);
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
        return true;
      }
      const n = ch.length;
      if (!this._src || this._src.length < n + 1) {
        this._src = new Float32Array(n + 1);
        this._out = new Float32Array(Math.ceil((n + 1) / this._ratio) + 2);
      }
      const src = this._src;
      const out = this._out;
      src[0] = this._prev;
      src.set(ch, 1);
      let outLen = 0;
      let pos = this._pos;
      while (pos < n) {
        const i = Math.floor(pos);
        const frac = pos - i;
        out[outLen++] = src[i] + (src[i + 1] - src[i]) * frac;
        pos += this._ratio;
      }
      this._pos = pos - n;
      this._prev = ch[n - 1];
      if (outLen) {
        const pcm = this._toPcm(out, outLen);
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
      }
      return true;
    }
  }
  registerProcessor('capture', CaptureProcessor);
`

// A ring buffer rather than one AudioBufferSource per chunk, which drifts and
// clicks under jitter. Posting 'stop' empties it for barge-in.
const PLAYBACK_WORKLET = `
  class PlaybackProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      this._ring = new Float32Array(sampleRate * 30);
      this._writePos = 0;
      this._readPos = 0;
      this._available = 0;
      this._step = ${WIRE_RATE} / sampleRate;
      this._rsPos = 0;
      this._rsPrev = 0;
      this._drained = false;
      this.port.onmessage = (e) => {
        if (e.data === 'stop') {
          this._writePos = this._readPos = this._available = 0;
          this._rsPos = this._rsPrev = 0;
          return;
        }
        const int16 = new Int16Array(e.data);
        if (!int16.length) return;
        if (this._drained) {
          this._rsPrev = 0;
          this._rsPos = 0;
          this._drained = false;
        }
        if (this._step === 1) {
          for (let i = 0; i < int16.length; i++) this._push(int16[i] / 32768);
          return;
        }
        const n = int16.length;
        let pos = this._rsPos;
        while (pos < n) {
          const i = Math.floor(pos);
          const frac = pos - i;
          const a = i === 0 ? this._rsPrev : int16[i - 1] / 32768;
          const b = int16[i] / 32768;
          this._push(a + (b - a) * frac);
          pos += this._step;
        }
        this._rsPos = pos - n;
        this._rsPrev = int16[n - 1] / 32768;
      };
    }
    _push(v) {
      if (this._available < this._ring.length) {
        this._ring[this._writePos] = v;
        this._writePos = (this._writePos + 1) % this._ring.length;
        this._available++;
      }
    }
    process(inputs, outputs) {
      const output = outputs[0];
      const out = output[0];
      const cap = this._ring.length;
      for (let i = 0; i < out.length; i++) {
        if (this._available > 0) {
          out[i] = this._ring[this._readPos];
          this._readPos = (this._readPos + 1) % cap;
          this._available--;
        } else {
          out[i] = 0;
          this._drained = true;
        }
      }
      for (let ch = 1; ch < output.length; ch++) output[ch].set(out);
      return true;
    }
  }
  registerProcessor('playback', PlaybackProcessor);
`

const blobUrl = (code) =>
  URL.createObjectURL(new Blob([code], { type: 'application/javascript' }))

// Deltas arrive with a leading space sometimes and without it other times, so
// add one only when neither side has one and the delta is not punctuation.
const ATTACHES_LEFT = /^[.,!?;:%°)\]}…'"’”]/
const NO_SPACE_AFTER = /[([{$\-\/'"‘“]$/

function appendDelta(text, delta) {
  if (!delta) return text
  if (!text) return delta
  if (/^\s/.test(delta) || /\s$/.test(text)) return text + delta
  if (ATTACHES_LEFT.test(delta) || NO_SPACE_AFTER.test(text)) return text + delta
  return text + ' ' + delta
}

const noop = () => {}

export const VoiceCall = {
  agent: null,
  sessionId: null,
  on: {
    status: noop,     // (state, detail)
    partial: noop,    // (who, text)      a line still being spoken
    line: noop,       // (who, text)      a line that is final
    tool: noop,       // (name, argsObject)
    ready: noop,      // (sessionId)
    tick: noop,       // (seconds)
    ended: noop,
  },

  async start(deviceId) {
    await start(deviceId)
  },
  stop,
  listMics,
}

let ws, captureCtx, playbackCtx, playback, mic, callStart, timer
// The full reply arrives once its audio has been sent, which beats the audio
// playing out, so deltas keep coming after the line is printed. printedReply
// stops them rebuilding the same sentence underneath it.
let liveReply = null
let printedReply = null
let agentPartial = ''

async function listMics() {
  if (!navigator.mediaDevices?.enumerateDevices) return []
  const devices = await navigator.mediaDevices.enumerateDevices()
  return devices
    .filter((d) => d.kind === 'audioinput')
    // Chrome's synthetic entries alias a real device and duplicate it.
    .filter((d) => d.deviceId !== 'default' && d.deviceId !== 'communications')
}

async function addWorklet(ctx, code, name) {
  const url = blobUrl(code)
  try {
    await ctx.audioWorklet.addModule(url)
  } finally {
    URL.revokeObjectURL(url)
  }
  return new AudioWorkletNode(ctx, name)
}

async function start(deviceId) {
  VoiceCall.on.status('connecting')
  liveReply = printedReply = null
  agentPartial = ''

  try {
    // The API key never reaches the page; this token expires in 60 seconds.
    const res = await fetch('/api/token')
    if (!res.ok) {
      VoiceCall.on.status('error', 'could not mint a token, check the API key')
      return
    }
    const { token } = await res.json()

    // Two contexts, created in the click handler so Safari starts them.
    captureCtx = new AudioContext({ sampleRate: WIRE_RATE })
    playbackCtx = new AudioContext({ sampleRate: WIRE_RATE })
    await Promise.all([captureCtx.resume(), playbackCtx.resume()])

    playback = await addWorklet(playbackCtx, PLAYBACK_WORKLET, 'playback')
    playback.connect(playbackCtx.destination)

    mic = await navigator.mediaDevices.getUserMedia({
      audio: {
        // A preference, not `exact`: an unplugged device falls back.
        ...(deviceId ? { deviceId } : {}),
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: false,
        autoGainControl: false,
      },
    })
    const capture = await addWorklet(captureCtx, CAPTURE_WORKLET, 'capture')
    captureCtx.createMediaStreamSource(mic).connect(capture)

    const url = new URL('wss://agents.assemblyai.com/v1/ws')
    url.searchParams.set('token', token)
    ws = new WebSocket(url)
    let ready = false

    // The API takes base64 inside JSON, not binary frames.
    capture.port.onmessage = ({ data }) => {
      if (!ready || ws.readyState !== 1) return
      const bytes = new Uint8Array(data)
      let binary = ''
      for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000))
      }
      ws.send(JSON.stringify({ type: 'input.audio', audio: btoa(binary) }))
    }

    // Everything about the agent lives server-side; the session just names it.
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'session.update', session: { agent_id: VoiceCall.agent.id } }))
    }

    ws.onmessage = ({ data }) => {
      const msg = JSON.parse(data)
      switch (msg.type) {
        case 'session.ready':
          ready = true
          callStart = Date.now()
          VoiceCall.sessionId = msg.session_id
          timer = setInterval(tick, 1000)
          tick()
          VoiceCall.on.ready(msg.session_id)
          VoiceCall.on.status('listening')
          break

        case 'input.speech.started':
          // Barge-in: empty the ring buffer so the agent stops mid-word.
          playback?.port.postMessage('stop')
          VoiceCall.on.status('listening')
          break

        case 'reply.started':
          VoiceCall.on.status('speaking')
          break

        case 'reply.audio': {
          const raw = atob(msg.data)
          const bytes = new Uint8Array(raw.length)
          for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
          playback?.port.postMessage(bytes.buffer, [bytes.buffer])
          break
        }

        case 'reply.done':
          VoiceCall.on.status('listening')
          if (msg.status === 'interrupted') playback?.port.postMessage('stop')
          break

        // text is the full transcript so far, so it replaces.
        case 'transcript.user.delta':
          VoiceCall.on.partial('caller', msg.text)
          break

        // delta is the next word only, so it appends.
        case 'transcript.agent.delta':
          if (msg.reply_id && msg.reply_id === printedReply) break
          if (msg.reply_id !== liveReply) {
            liveReply = msg.reply_id
            agentPartial = ''
          }
          agentPartial = appendDelta(agentPartial, msg.delta)
          VoiceCall.on.partial('agent', agentPartial)
          break

        case 'transcript.user':
          VoiceCall.on.line('caller', msg.text)
          break

        case 'transcript.agent':
          printedReply = msg.reply_id ?? printedReply
          agentPartial = ''
          VoiceCall.on.line('agent', msg.text)
          break

        case 'tool.call':
          // http tools run on AssemblyAI's side; no result comes back here.
          VoiceCall.on.tool(msg.name, msg.arguments ?? {})
          break

        case 'session.ended':
          ws.close()
          break

        case 'session.error':
          VoiceCall.on.status('error', msg.message)
          break
      }
    }

    ws.onclose = () => { VoiceCall.on.status('idle'); finish() }
    ws.onerror = () => { VoiceCall.on.status('error', 'connection failed'); finish() }
  } catch (error) {
    VoiceCall.on.status('error', error.message)
    finish()
  }
}

function stop() {
  // Close cleanly so the session record ends, falling back to the socket.
  if (ws?.readyState === 1) {
    ws.send(JSON.stringify({ type: 'session.end' }))
    const socket = ws
    setTimeout(() => { if (socket.readyState === 1) socket.close() }, 3000)
  } else {
    ws?.close()
  }
  teardown()
  VoiceCall.on.status('idle')
}

function teardown() {
  playback?.port.postMessage('stop')
  mic?.getTracks().forEach((track) => track.stop())
  captureCtx?.close()
  playbackCtx?.close()
  captureCtx = playbackCtx = playback = mic = null
}

function finish() {
  clearInterval(timer)
  teardown()
  VoiceCall.on.ended()
}

function tick() {
  VoiceCall.on.tick(Math.floor((Date.now() - callStart) / 1000))
}
