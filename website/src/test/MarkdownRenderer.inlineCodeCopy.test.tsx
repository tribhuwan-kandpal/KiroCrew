import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { OPEN_DELAY_MS } from '../components/InstantTip'
import { copyToClipboard } from '../utils/clipboard'

// Resolves TRUE: the chip gates its confirmation on the helper's boolean, so a
// mock that resolved `undefined` would look like a refused clipboard write.
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => true) }))

beforeEach(() => { vi.mocked(copyToClipboard).mockClear(); vi.mocked(copyToClipboard).mockResolvedValue(true) })
afterEach(() => { vi.useRealTimers() })

describe('InlineCode click-to-copy (non-path chips)', () => {
  it('copies the inline code text on click', async () => {
    render(<MarkdownRenderer content={'Run `npm test` please.'} />)
    const code = await screen.findByText('npm test')
    expect(code.tagName).toBe('CODE')
    expect(code).toHaveAttribute('role', 'button')

    fireEvent.click(code)
    expect(copyToClipboard).toHaveBeenCalledWith('npm test')
  })

  it('copies on Enter keydown', async () => {
    render(<MarkdownRenderer content={'Try `curl -s https://example.com`.'} />)
    const code = await screen.findByText('curl -s https://example.com')
    fireEvent.keyDown(code, { key: 'Enter' })
    expect(copyToClipboard).toHaveBeenCalledWith('curl -s https://example.com')
  })

  it('copies on Space keydown', async () => {
    render(<MarkdownRenderer content={'Set `NODE_ENV=production`.'} />)
    const code = await screen.findByText('NODE_ENV=production')
    fireEvent.keyDown(code, { key: ' ' })
    expect(copyToClipboard).toHaveBeenCalledWith('NODE_ENV=production')
  })

  it('names the action in its tooltip, on hover and on keyboard focus alike', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    // No native title: the shared instant tooltip replaces it, so a keyboard
    // user gets the cue too (a `title` never shows on focus).
    expect(code).not.toHaveAttribute('title')

    fireEvent.mouseEnter(code)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to copy')
    fireEvent.mouseLeave(code)
    expect(screen.queryByRole('tooltip')).toBeNull()

    fireEvent.focus(code)
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to copy')
    expect(code).toHaveAttribute('aria-describedby', screen.getByRole('tooltip').id)
  })

  it('confirms the copy in the tooltip and to assistive tech, then clears both after 1500ms', async () => {
    vi.useFakeTimers()
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('')

    fireEvent.focus(code)
    await act(async () => { fireEvent.click(code) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copied!')
    expect(status).toHaveTextContent('Copied!')

    act(() => { vi.advanceTimersByTime(1500) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to copy')
    expect(status).toHaveTextContent('')
  })

  it('reports a refused clipboard write beside the chip instead of claiming a copy', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    const code = screen.getByText('--verbose')
    fireEvent.focus(code)
    await act(async () => { fireEvent.click(code) })
    expect(copyToClipboard).toHaveBeenCalledWith('--verbose')
    // No confirmation anywhere...
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to copy')
    expect(screen.getByRole('status')).toHaveTextContent('')
    // ...but the failure is rendered, through ErrorNotice, next to the chip.
    const notice = screen.getByTestId('md-chip-copy-error')
    expect(notice).toHaveTextContent('Copy failed')
    expect(code.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    // A later successful copy clears it and confirms.
    vi.mocked(copyToClipboard).mockResolvedValue(true)
    await act(async () => { fireEvent.click(code) })
    expect(screen.queryByTestId('md-chip-copy-error')).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent('Copied!')
  })

  it('lets the user dismiss the refused-write notice', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    render(<MarkdownRenderer content={'Use `--verbose` flag.'} />)
    await act(async () => { fireEvent.click(screen.getByText('--verbose')) })
    expect(screen.getByTestId('md-chip-copy-error')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('md-chip-copy-error')).toBeNull()
  })

  it('is focusable via tabIndex', async () => {
    render(<MarkdownRenderer content={'Check `FOO_BAR` env var.'} />)
    const code = await screen.findByText('FOO_BAR')
    expect(code).toHaveAttribute('tabindex', '0')
  })

  it('wears code styling, not link styling: copy cursor, no accent colour, no underline', async () => {
    render(<MarkdownRenderer content={'Run `ls -la`.'} />)
    const code = await screen.findByText('ls -la')
    expect(code.className).toContain('font-mono')
    expect(code.className).toContain('bg-bg-elevated')
    expect(code.className).toContain('cursor-copy')
    expect(code.className).not.toContain('cursor-pointer')
    expect(code.className).not.toContain('text-accent')
    expect(code.className).not.toMatch(/underline/)
  })
})
