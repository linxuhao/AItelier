import { setTimeout as delay } from 'node:timers/promises'
import { Puppet, types, type payloads, type PuppetOptions } from 'wechaty-puppet'
import jsQR from 'jsqr'
import { PNG } from 'pngjs'
import { openNativeSession, SESSION_ENDED, type SelfProfile, type Session } from './native.js'

const CACHE_LIMIT = 2048

function putRecent<T>(cache: Map<string, T>, id: string, value: T) {
  cache.delete(id)
  cache.set(id, value)
  if (cache.size > CACHE_LIMIT) {
    const oldest = cache.keys().next().value
    if (oldest !== undefined) cache.delete(oldest)
  }
}

function decodeQr(encoded: string) {
  const bytes = Buffer.from(encoded, 'base64')
  if (bytes.length < 24 || bytes.length > 1024 * 1024 ||
      bytes.readUInt32BE(16) > 1024 || bytes.readUInt32BE(20) > 1024) {
    throw new Error('Invalid Neo login QR image')
  }
  const png = PNG.sync.read(bytes)
  const pixels = new Uint8ClampedArray(png.data.buffer, png.data.byteOffset, png.data.byteLength)
  const qr = jsQR(pixels, png.width, png.height)
  if (!qr?.data) throw new Error('Could not decode the Neo login QR')
  return qr.data
}

export class PuppetNeo extends Puppet {
  static override readonly VERSION = '0.1.1'

  private run?: Promise<void>
  private abort?: AbortController
  private session?: Session
  private online = false
  private selfProfile?: SelfProfile
  private pendingSends = new Set<Promise<unknown>>()
  private readonly contacts = new Map<string, payloads.Contact>()
  private readonly rooms = new Map<string, true>()
  private readonly messages = new Map<string, payloads.Message>()

  constructor(options: PuppetOptions = {}) {
    super(options)
  }

  protected createSession(): Session {
    return openNativeSession()
  }

  protected scanCode(png: string): string {
    return decodeQr(png)
  }

  override async onStart(): Promise<void> {
    const abort = new AbortController()
    const session = this.createSession()
    this.abort = abort
    this.session = session
    this.run = this.serve(session, abort.signal).catch(async (error: unknown) => {
      this.online = false
      if (abort.signal.aborted) return
      if (error instanceof Error && error.message === SESSION_ENDED) {
        await super.logout('WeChat ended the session')
        return
      }
      this.emit('error', { data: error instanceof Error ? error.message : 'Neo session failed' })
      await super.logout('Neo session failed')
    }).finally(() => {
      this.online = false
    })
  }

  override async onStop(): Promise<void> {
    try {
      await this.stopServing()
      await this.session?.close()
    } finally {
      this.abort = undefined
      this.session = undefined
      this.run = undefined
      this.selfProfile = undefined
      this.contacts.clear()
      this.rooms.clear()
      this.messages.clear()
      if (this.isLoggedIn) await super.logout('puppet stop()')
    }
  }

  /**
   * Sign this device out of WeChat, then emit `logout`. The puppet stays started;
   * stop and start it again to log in with a new QR code.
   */
  override async logout(reason = 'logout()'): Promise<void> {
    if (!this.isLoggedIn) return
    const session = this.session
    const online = this.online
    await this.stopServing()
    if (session && online) {
      try {
        const { accepted } = await session.logout()
        if (!accepted) this.emit('error', { data: 'WeChat did not accept the logout' })
      } catch (error) {
        this.emit('error', { data: error instanceof Error ? error.message : 'Neo could not log out' })
      }
    }
    await super.logout(reason)
  }

  override ding(data?: string): void {
    this.emit('dong', { data })
  }

  override async messageRawPayload(id: string): Promise<payloads.Message> {
    const message = this.messages.get(id)
    if (!message) throw new Error('Neo message payload is no longer cached')
    return message
  }

  override async messageRawPayloadParser(payload: payloads.Message): Promise<payloads.Message> {
    return payload
  }

  override async contactRawPayload(id: string): Promise<payloads.Contact> {
    if (id === this.currentUserId) return this.selfContact(id)
    const contact = this.contacts.get(id)
    if (!contact) throw new Error('Neo has no payload for this contact')
    return contact
  }

  override async contactRawPayloadParser(payload: payloads.Contact): Promise<payloads.Contact> {
    return payload
  }

  override async messageSendText(destination: string, text: string, mentions?: string[]): Promise<void> {
    if (!this.session || !this.online || this.abort?.signal.aborted) throw new Error('Neo session is offline')
    const observed = destination.endsWith('@chatroom') ? this.rooms.has(destination) : this.contacts.has(destination)
    if (mentions?.length || !observed) {
      throw new Error('Neo supports text to an observed peer or group')
    }
    const send = this.session.sendText(destination, text)
    this.pendingSends.add(send)
    try {
      const result = await send
      if (result.status === 'uncertain') throw new Error('WeChat text send outcome is uncertain')
      if (result.status === 'rejected') throw new Error('WeChat rejected the text send')
    } finally {
      this.pendingSends.delete(send)
    }
  }

  private async stopServing(): Promise<void> {
    this.abort?.abort()
    await this.run
    await Promise.allSettled(this.pendingSends)
  }

  /** The logged-in account, named by its nickname, WeChat ID or username. */
  private selfContact(id: string): payloads.Contact {
    const profile = this.selfProfile
    return {
      id,
      name: profile?.nickname ?? profile?.alias ?? id,
      ...(profile?.alias ? { weixin: profile.alias } : {}),
      avatar: profile?.avatar ?? '',
      phone: [],
      type: types.Contact.Individual,
      gender: types.ContactGender.Unknown,
    }
  }

  private rememberContact(id: string): void {
    putRecent(this.contacts, id, {
      id, name: id, avatar: '', phone: [], type: types.Contact.Unknown, gender: types.ContactGender.Unknown,
    })
  }

  private async loginWithQr(session: Session, signal: AbortSignal): Promise<string> {
    const qr = this.scanCode((await session.startLogin()).png)
    signal.throwIfAborted()
    this.emit('scan', { qrcode: qr, status: types.ScanStatus.Waiting })
    let scanned = false
    for (let attempt = 0; attempt < 120; attempt++) {
      await delay(1000, undefined, { signal })
      const result = await session.pollLogin()
      signal.throwIfAborted()
      if (result.status === 'completed') {
        this.emit('scan', { status: types.ScanStatus.Confirmed })
        return result.username
      }
      if (result.status === 'scanned' && !scanned) {
        scanned = true
        this.emit('scan', { status: types.ScanStatus.Scanned })
      }
      if (result.status === 'expired' || result.status === 'cancelled' || result.status === 'rejected') {
        this.emit('scan', { status: result.status === 'expired' ? types.ScanStatus.Timeout : types.ScanStatus.Cancel })
        throw new Error(`Neo QR login ended: ${result.status}`)
      }
    }
    this.emit('scan', { status: types.ScanStatus.Timeout })
    throw new Error('Neo QR login timed out')
  }

  private async serve(session: Session, signal: AbortSignal): Promise<void> {
    await delay(0, undefined, { signal })
    const username = await this.loginWithQr(session, signal)
    signal.throwIfAborted()
    // `login` comes with the account's profile. A failed lookup falls back to the username.
    this.selfProfile = await session.lookupSelf().catch(() => undefined)
    signal.throwIfAborted()
    this.online = true
    this.login(username)
    const associated = await session.heartbeat()
    signal.throwIfAborted()
    let nextHeartbeat = performance.now() + associated.intervalMs
    await this.drain(session, 6, signal)
    this.emit('ready', {})
    while (!signal.aborted) {
      if (performance.now() >= nextHeartbeat) {
        const beat = await session.heartbeat()
        signal.throwIfAborted()
        nextHeartbeat = performance.now() + beat.intervalMs
        if (beat.sync) await this.drain(session, 7, signal)
      }
      if (await session.pollIncoming()) await this.drain(session, 1, signal)
    }
  }

  private async drain(session: Session, scene: 1 | 6 | 7, signal: AbortSignal): Promise<void> {
    let more: boolean
    do {
      signal.throwIfAborted()
      const page = await session.sync(scene)
      signal.throwIfAborted()
      for (const item of page.messages) {
        if (this.messages.has(item.id)) continue
        this.rememberContact(item.talker)
        const text = { id: item.id, talkerId: item.talker, text: item.text, timestamp: item.timestamp, type: types.Message.Text }
        if (item.room) {
          putRecent(this.rooms, item.room, true)
          putRecent(this.messages, item.id, { ...text, roomId: item.room })
        } else if (item.listener) {
          this.rememberContact(item.listener)
          putRecent(this.messages, item.id, { ...text, listenerId: item.listener })
        } else {
          continue
        }
        this.emit('message', { messageId: item.id })
      }
      more = page.continuation
    } while (more)
  }
}
