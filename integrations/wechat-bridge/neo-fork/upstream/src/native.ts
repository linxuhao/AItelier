import { createRequire } from 'node:module'
import platforms from './native-platforms.json' with { type: 'json' }

/** Error message for a session that WeChat no longer accepts. */
export const SESSION_ENDED = 'Neo session ended by WeChat'

export interface SelfProfile {
  nickname: string | null
  alias: string | null
  avatar: string | null
}

export interface IncomingText {
  id: string
  /** Who wrote the message; in a group, the member who posted it. */
  talker: string
  /** The account that received a direct message. */
  listener?: string
  /** The group the message was posted in. */
  room?: string
  text: string
  timestamp: number
}

export interface Session {
  startLogin(): Promise<{ png: string }>
  pollLogin(): Promise<
    | { status: 'pending' | 'scanned' | 'retry' | 'expired' | 'cancelled' | 'rejected' }
    | { status: 'completed', username: string }
  >
  heartbeat(): Promise<{ sync: boolean, intervalMs: number }>
  pollIncoming(): Promise<boolean>
  sync(scene: 1 | 6 | 7): Promise<{ messages: IncomingText[], continuation: boolean }>
  sendText(destination: string, text: string): Promise<{ status: 'accepted' | 'rejected' | 'uncertain' }>
  lookupSelf(): Promise<SelfProfile>
  logout(): Promise<{ accepted: boolean }>
  close(): Promise<void>
}

type Addon = { [K in keyof Session]: (...args: Parameters<Session[K]>) => Promise<string> }

export function openNativeSession(): Session {
  const platform = `${process.platform}-${process.arch}`
  if (platform === 'win32-arm64') {
    throw new Error('Run Neo with x64 Node.js on Windows 11 on ARM')
  }
  if (!Object.hasOwn(platforms, platform)) {
    throw new Error(`Neo has no native build for ${platform}`)
  }
  const report = process.report.getReport() as { header: { glibcVersionRuntime?: string } }
  if (process.platform === 'linux' && !report.header.glibcVersionRuntime) {
    throw new Error('Neo requires glibc on Linux')
  }
  const require = createRequire(import.meta.url)
  const addonPath = `../native/${platform}/neo.node`
  try {
    require.resolve(addonPath)
  } catch (error) {
    throw new Error(`Neo native addon missing for ${platform}. Install a package for this platform or run npm run build:native from a source checkout.`, { cause: error })
  }
  const { NeoSession } = require(addonPath) as { NeoSession: new () => Addon }
  const addon = new NeoSession()

  return {
    startLogin: async () => JSON.parse(await addon.startLogin()),
    pollLogin: async () => JSON.parse(await addon.pollLogin()),
    heartbeat: async () => JSON.parse(await addon.heartbeat()),
    pollIncoming: async () => JSON.parse(await addon.pollIncoming()),
    sync: async (scene) => JSON.parse(await addon.sync(scene)),
    sendText: async (destination, text) => JSON.parse(await addon.sendText(destination, text)),
    lookupSelf: async () => JSON.parse(await addon.lookupSelf()),
    logout: async () => JSON.parse(await addon.logout()),
    close: async () => { await addon.close() },
  }
}
