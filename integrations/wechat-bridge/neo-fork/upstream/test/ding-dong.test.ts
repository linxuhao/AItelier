import assert from 'node:assert/strict'
import { setImmediate as nextTurn, setTimeout as delay } from 'node:timers/promises'
import { test } from 'node:test'
import { types, type payloads } from 'wechaty-puppet'
import qrTerminal from 'qrcode-terminal'
import PuppetNeo from 'wechaty-puppet-neo'
import type { IncomingText, SelfProfile, Session } from '../src/native.js'

const username = 'fixture-self'
const sender = 'fixture-peer'
const room = 'fixture-room@chatroom'
const member = 'fixture-member'
const direct: IncomingText = { id: 'fixture-message', talker: sender, listener: username, text: 'ding', timestamp: 1700000000 }
const inRoom: IncomingText = { id: 'fixture-room-message', talker: member, room, text: 'ding', timestamp: 1700000001 }
// The native addon's error for a session WeChat no longer accepts (SESSION_ENDED).
const SESSION_ENDED = 'Neo session ended by WeChat'
const profile: SelfProfile = { nickname: 'Fixture Name', alias: 'fixture-alias', avatar: null }

interface Script {
  send?: 'accepted' | 'uncertain'
  profile?: SelfProfile | 'fails'
  /** Thrown by every heartbeat after the first, which then comes due quickly. */
  laterHeartbeat?: Error
  /** Delivered by the first NewSync after login. */
  messages?: IncomingText[]
  /** Thrown by NewSync instead of delivering messages. */
  laterSync?: Error
}

function scriptedSession(trace: unknown[], script: Script = {}): Session {
  let notified = false
  let beats = 0
  return {
    async startLogin() {
      trace.push('start-login')
      return { png: 'fixture-image' }
    },
    async pollLogin() {
      return { status: 'completed', username }
    },
    async heartbeat() {
      beats++
      if (beats > 1 && script.laterHeartbeat) throw script.laterHeartbeat
      return { sync: false, intervalMs: script.laterHeartbeat ? 20 : 5000 }
    },
    async pollIncoming() {
      if (notified) {
        await delay(15)
        return false
      }
      notified = true
      return true
    },
    async sync(scene) {
      trace.push(['sync', scene])
      if (scene === 6) return { messages: [], continuation: false }
      assert.equal(scene, 1)
      if (script.laterSync) throw script.laterSync
      return { messages: script.messages ?? [direct], continuation: false }
    },
    async sendText(destination, text) {
      trace.push(['send', destination, text])
      return { status: script.send ?? 'accepted' }
    },
    async lookupSelf() {
      // Recorded one turn later, so the trace shows whether login waited for it.
      await nextTurn()
      trace.push('lookup-self')
      if (script.profile === 'fails') throw new Error('Neo could not look up the account profile')
      return script.profile ?? profile
    },
    async logout() {
      trace.push('logout')
      return { accepted: true }
    },
    async close() {
      trace.push('close')
    },
  }
}

function waitFor<T>(subscribe: (done: (value: T) => void) => void, label: string, timeoutMs = 5000): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`No ${label} before the timeout`)), timeoutMs)
    subscribe(value => {
      clearTimeout(timer)
      resolve(value)
    })
  })
}

function scriptedPuppet(trace: unknown[], script: () => Script) {
  const puppet = new class extends PuppetNeo {
    protected override createSession() { return scriptedSession(trace, script()) }
    protected override scanCode() { return 'fixture-qr' }
  }()
  const events = {
    logins: [] as payloads.EventLogin[],
    logouts: [] as payloads.EventLogout[],
    errors: [] as string[],
  }
  puppet.on('login', payload => {
    trace.push('login')
    events.logins.push(payload)
  })
  puppet.on('ready', () => { trace.push('ready') })
  puppet.on('logout', payload => { events.logouts.push(payload) })
  puppet.on('error', ({ gerror }) => { events.errors.push(gerror ? JSON.parse(gerror).message : 'unknown') })
  return { puppet, events }
}

test('the raw Puppet API delivers text and scans again after stopping', async () => {
  const trace: unknown[] = []
  const scans: payloads.EventScan[] = []
  let send: 'accepted' | 'uncertain' = 'accepted'
  const { puppet, events } = scriptedPuppet(trace, () => ({ send }))
  puppet.on('scan', payload => { scans.push(payload) })
  for (const attempt of [1, 2]) {
    trace.length = 0
    scans.length = 0
    events.logins.length = 0
    events.logouts.length = 0
    const delivery = waitFor<payloads.EventMessage>(done => puppet.once('message', done), 'message')
    await puppet.start()
    try {
      const { messageId } = await delivery
      const message = await puppet.messagePayload(messageId)
      assert.equal(puppet.currentUserId, username)
      assert.equal(message.text, 'ding')
      assert.equal(message.talkerId, sender)
      if (attempt === 1) await puppet.messageSendText(sender, 'dong')
      else await assert.rejects(puppet.messageSendText(sender, 'dong'), /outcome is uncertain/)
      assert.deepEqual(trace, [
        'start-login', 'lookup-self', 'login', ['sync', 6], 'ready', ['sync', 1], ['send', sender, 'dong'],
      ])
      assert(scans.some(scan => scan.status === types.ScanStatus.Waiting && scan.qrcode === 'fixture-qr'))
      assert.equal(puppet.memory.name, undefined)
      assert.equal(await puppet.memory.size, 0)
      assert.deepEqual(events.errors, [])
      assert.deepEqual(events.logins, [{ contactId: username }])
      const self = await puppet.contactPayload(username)
      assert.equal(self.name, 'Fixture Name')
      assert.equal(self.weixin, 'fixture-alias')
      assert.deepEqual(events.logouts, [])
    } finally {
      await puppet.stop()
    }
    assert.deepEqual(events.logouts, [{ contactId: username, data: 'puppet stop()' }])
    assert.equal(trace.at(-1), 'close')
    await assert.rejects(puppet.messageSendText(sender, 'dong'), /offline/)
    send = 'uncertain'
  }
})

test('login falls back to the username when the profile lookup fails', async () => {
  const { puppet, events } = scriptedPuppet([], () => ({ profile: 'fails' }))
  const ready = waitFor(done => puppet.once('ready', done), 'ready')
  await puppet.start()
  try {
    await ready
    assert.deepEqual(events.logins, [{ contactId: username }])
    const self = await puppet.contactPayload(username)
    assert.equal(self.name, username)
    assert.equal(self.weixin, undefined)
    assert.deepEqual(events.errors, [])
  } finally {
    await puppet.stop()
  }
})

test('group text names the group and the member, and the reply goes to the group', async () => {
  const trace: unknown[] = []
  const { puppet, events } = scriptedPuppet(trace, () => ({ messages: [inRoom] }))
  const delivery = waitFor<payloads.EventMessage>(done => puppet.once('message', done), 'message')
  await puppet.start()
  try {
    const message = await puppet.messagePayload((await delivery).messageId)
    assert.equal(message.roomId, room)
    assert.equal(message.talkerId, member)
    assert.equal(message.listenerId, undefined)
    assert.equal(message.text, 'ding')
    assert.equal((await puppet.contactPayload(member)).id, member)
    await puppet.messageSendText(room, 'dong')
    assert.deepEqual(trace.at(-1), ['send', room, 'dong'])
    await assert.rejects(puppet.messageSendText('fixture-other@chatroom', 'dong'), /observed peer or group/)
    await assert.rejects(puppet.messageSendText(room, 'dong', [member]), /observed peer or group/)
    assert.deepEqual(events.errors, [])
  } finally {
    await puppet.stop()
  }
})

test('signing out from the phone during sync emits logout without an error', async () => {
  const trace: unknown[] = []
  const { puppet, events } = scriptedPuppet(trace, () => ({ laterSync: new Error(SESSION_ENDED) }))
  const loggedOut = waitFor(done => puppet.once('logout', done), 'logout')
  await puppet.start()
  try {
    assert.deepEqual(await loggedOut, { contactId: username, data: 'WeChat ended the session' })
    assert.deepEqual(events.errors, [])
    await assert.rejects(puppet.messageSendText(sender, 'dong'), /offline/)
  } finally {
    await puppet.stop()
  }
  assert.equal(events.logouts.length, 1)
  assert(!trace.includes('logout'))
})

test('WeChat ending the session emits logout without an error', async () => {
  const trace: unknown[] = []
  const { puppet, events } = scriptedPuppet(trace, () => ({ laterHeartbeat: new Error(SESSION_ENDED) }))
  const loggedOut = waitFor(done => puppet.once('logout', done), 'logout')
  await puppet.start()
  try {
    assert.deepEqual(await loggedOut, { contactId: username, data: 'WeChat ended the session' })
    // The base Puppet clears the user one turn after emitting, for logout listeners.
    await nextTurn()
    assert.equal(puppet.isLoggedIn, false)
    assert.deepEqual(events.errors, [])
    await assert.rejects(puppet.messageSendText(sender, 'dong'), /offline/)
  } finally {
    await puppet.stop()
  }
  assert.equal(events.logouts.length, 1)
  assert(!trace.includes('logout'))
  assert.equal(trace.at(-1), 'close')
})

test('an unexpected session failure emits error, then logout', async () => {
  const { puppet, events } = scriptedPuppet([], () => ({
    laterHeartbeat: new Error('Neo could not check session heartbeat'),
  }))
  const loggedOut = waitFor(done => puppet.once('logout', done), 'logout')
  await puppet.start()
  try {
    assert.deepEqual(await loggedOut, { contactId: username, data: 'Neo session failed' })
    assert.deepEqual(events.errors, ['Neo could not check session heartbeat'])
    await nextTurn()
    assert.equal(puppet.isLoggedIn, false)
  } finally {
    await puppet.stop()
  }
  assert.equal(events.logouts.length, 1)
})

test('logout() signs the device out once and stop() adds no second logout', async () => {
  const trace: unknown[] = []
  const { puppet, events } = scriptedPuppet(trace, () => ({}))
  const ready = waitFor(done => puppet.once('ready', done), 'ready')
  await puppet.start()
  try {
    await ready
    await puppet.logout('fixture reason')
    assert.deepEqual(events.logouts, [{ contactId: username, data: 'fixture reason' }])
    assert.equal(puppet.isLoggedIn, false)
    assert.deepEqual(trace.filter(step => step === 'logout'), ['logout'])
    await assert.rejects(puppet.messageSendText(sender, 'dong'), /offline/)
    await puppet.logout()
  } finally {
    await puppet.stop()
  }
  assert.equal(events.logouts.length, 1)
  assert.deepEqual(trace.filter(step => step === 'logout'), ['logout'])
  assert.deepEqual(events.errors, [])
})

test('the TypeScript example replies through PuppetNeo directly', async t => {
  const trace: unknown[] = []
  const originalCreateSession = Reflect.get(PuppetNeo.prototype, 'createSession')
  const originalScanCode = Reflect.get(PuppetNeo.prototype, 'scanCode')
  let puppet: PuppetNeo | undefined
  Reflect.set(PuppetNeo.prototype, 'createSession', function (this: PuppetNeo) {
    puppet = this
    return scriptedSession(trace, { messages: [direct, inRoom] })
  })
  Reflect.set(PuppetNeo.prototype, 'scanCode', () => 'fixture-qr')
  const qr = t.mock.method(qrTerminal, 'generate', () => {})
  const output = t.mock.method(console, 'info', () => {})
  const errors = t.mock.method(console, 'error', () => {})
  t.after(async () => {
    await puppet?.stop()
    Reflect.set(PuppetNeo.prototype, 'createSession', originalCreateSession)
    Reflect.set(PuppetNeo.prototype, 'scanCode', originalScanCode)
  })

  await import('../examples/ding-dong-bot.ts')
  assert(puppet)
  const deadline = Date.now() + 5000
  while (!output.mock.calls.some(call => call.arguments[0] === 'Sent in the group: dong')) {
    assert(Date.now() < deadline, 'Example did not finish its reply')
    await delay(15)
  }
  assert.deepEqual(qr.mock.calls[0]?.arguments, ['fixture-qr', { small: true }])
  assert(output.mock.calls.some(call => call.arguments[0] === 'Logged in as Fixture Name.'))
  assert(output.mock.calls.some(call => call.arguments[0] === 'Received in a group: ding'))
  assert(output.mock.calls.some(call => call.arguments[0] === 'Sent: dong'))
  assert.deepEqual(trace, [
    'start-login', 'lookup-self', ['sync', 6], ['sync', 1], ['send', sender, 'dong'], ['send', room, 'dong'],
  ])
  assert.equal(errors.mock.callCount(), 0)
  assert(output.mock.calls.every(call => [sender, username, room, member].every(id => !String(call.arguments[0]).includes(id))))
})
