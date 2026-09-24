/**
 * Notification Center sheet — its crash fallback hands the crash to the agent.
 *
 * The sheet renders inside its own `ErrorBoundary` with a custom fallback (a
 * small panel over the bell, not the boundary's default card). The panel used
 * to offer only "Open the full inbox": a dead end, because the inbox page is not
 * where a render crash gets fixed. The fallback now mounts `AskAgentButton`
 * with the CAUGHT ERROR's message, which is the key the button uses to recover
 * the journaled report at click time — so the agent receives the crash
 * (message, `notifications-bell` scope, component stack), not the panel's
 * headline. The hand-off is `hard` (a full page load), the boundary's own
 * crash-fallback shape: the hook-free button cannot ask through the leave gate
 * the inbox link uses, and a full load lets a draft-holding page beneath the
 * sheet defend itself with its `beforeunload` guard.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { sendErrorToChat } from '../utils/errorReport'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))

// The throwing child: the feed is the sheet's body, and it is rendered only once
// the bell opens the sheet, so the boundary engages on the click and nowhere
// else in the shell. Unconditional, not a counter — React re-invokes a throwing
// render to rebuild the component stack, and a "throw once" child would
// succeed on the retry. The error itself is chosen per test.
const crash = vi.hoisted(() => ({ make: (): Error => new Error('zzq-feed-broke') }))
vi.mock('../components/notifications/NotificationFeed', async importOriginal => {
  const mod = await importOriginal<typeof import('../components/notifications/NotificationFeed')>()
  return { ...mod, default: () => { throw crash.make() } }
})

// The hand-off's last step would `location.assign` in jsdom; observe it instead.
vi.mock('../utils/errorReport', async importOriginal => {
  const mod = await importOriginal<typeof import('../utils/errorReport')>()
  return { ...mod, sendErrorToChat: vi.fn(() => true) }
})

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import App from '../App'

async function openCrashedSheet() {
  const store = createTestStore({ notifications: { items: [], clearSeq: 0, ackSeq: 0, ackSeqByTs: {} } })
  renderWithProviders(<App />, { route: '/chat', store })
  fireEvent.click(await screen.findByLabelText('Notifications'))
  const headline = await screen.findByText('Notifications failed to load')
  // The fallback panel: the material card the headline sits in.
  return headline.closest('[data-nc-material]') as HTMLElement
}

describe('Notification Center sheet — the crash fallback hands off to the agent', () => {
  let consoleError: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    vi.mocked(sendErrorToChat).mockClear()
    // The boundary logs the caught throw on purpose; keep the run readable.
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    consoleError.mockRestore()
    crash.make = () => new Error('zzq-feed-broke')
  })

  it('offers "Ask the agent" above the inbox link, and the hand-off carries the caught crash', async () => {
    const panel = await openCrashedSheet()
    expect(panel, 'the throw must land in the sheet\'s own boundary, not the root one').not.toBeNull()

    // The inbox link the panel always had is still there.
    expect(within(panel).getByRole('button', { name: 'Open the full inbox' })).toBeInTheDocument()

    const ask = within(panel).getByRole('button', { name: 'Ask the agent' })
    // The line under the button says what the press does, for a reader who
    // has never met the hand-off: it leaves this page, and the chat it opens
    // has the report pre-filled but NOT sent — the user still presses send.
    expect(within(panel).getByText(
      "Leaves this page and opens a new chat with this error's report already filled in, ready for you to send",
    )).toBeInTheDocument()
    fireEvent.click(ask)

    expect(sendErrorToChat).toHaveBeenCalledTimes(1)
    const [prompt, opts] = vi.mocked(sendErrorToChat).mock.calls[0]
    // The boundary's OWN error reached the agent — resolved from the journal
    // (the scope rides as the report's code), not the panel's headline.
    expect(prompt).toContain('- Message: zzq-feed-broke')
    expect(prompt).toContain('- Code: notifications-bell')
    expect(prompt).not.toContain('Notifications failed to load')
    // A crash fallback hands off with a full page load.
    expect(opts).toEqual({ hard: true })
  })

  it('still hands off when the thrown error has no message: the name stands in for it', async () => {
    // `AskAgentButton` renders nothing with neither report nor message, so a
    // bare `throw new TypeError('')` would leave the panel with no hand-off at
    // all. The boundary journals `message || name` for the same reason; the
    // fallback must key its button on the same value or the lookup misses.
    crash.make = () => new TypeError('')
    const panel = await openCrashedSheet()

    fireEvent.click(within(panel).getByRole('button', { name: 'Ask the agent' }))

    expect(sendErrorToChat).toHaveBeenCalledTimes(1)
    const [prompt] = vi.mocked(sendErrorToChat).mock.calls[0]
    expect(prompt).toContain('- Message: TypeError')
    expect(prompt).toContain('- Code: notifications-bell')
  })
})
