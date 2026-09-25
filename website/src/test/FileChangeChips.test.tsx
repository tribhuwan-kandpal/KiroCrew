import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import FileChangeChips, { countLines, headerClickAction, type FileChangeEntry } from '../components/FileChangeChips'

const change = (path: string, before: string, after: string) => ({ path, before, after })
const rows = (c: HTMLElement) => c.querySelectorAll('[data-testid^="fcc-row-"]')

/* The expanded style renders a lightweight header per closed file and mounts
 * Pierre only after disclosure. Two consequences for this suite:
 *
 *  - Closed filenames, counts, diffstat cells and controls are ordinary DOM and
 *    covered by the focused header tests. Pierre's open header still lives in a
 *    shadow root behind a lazy chunk that never resolves in this suite.
 *  - The line counting used by both header forms is a pure exported function,
 *    so it is tested directly instead of through presentation details.
 *
 * Header parity and mount lifecycle are covered in the focused companion tests. */
describe('countLines', () => {
  it('counts pure additions', () => {
    expect(countLines('a', 'a\nb\nc')).toEqual({ added: 2, removed: 0 })
  })

  it('counts pure removals', () => {
    expect(countLines('a\nb\nc', 'a')).toEqual({ added: 0, removed: 2 })
  })

  it('reports a pure move as +N/-N, not 0/0', () => {
    // LCS attributes a moved line to both sides; a multiset count would call
    // this unchanged, which reads as "nothing happened" on a real reorder.
    expect(countLines('a\nb', 'b\na')).toEqual({ added: 1, removed: 1 })
  })

  it('reports nothing when the content is identical', () => {
    expect(countLines('same\ntext', 'same\ntext')).toEqual({ added: 0, removed: 0 })
  })

  it('counts a mixed edit', () => {
    expect(countLines('one\ntwo\nthree', 'one\ntwo-edited\nthree\nfour')).toEqual({ added: 2, removed: 1 })
  })

  it('treats empty content as zero lines, not one phantom line', () => {
    // ''.split('\n') is [''], which would mis-count a new file as +1/-1.
    expect(countLines('a\nb', '')).toEqual({ added: 0, removed: 2 })
    expect(countLines('', 'a')).toEqual({ added: 1, removed: 0 })
  })

  /* Past 1M LCS cells the counter drops to a multiset count to bound cost.
   * That fallback is cheaper and measurably weaker, so these pin what it
   * reports at that size — including the one case where it disagrees with
   * LCS — rather than restating the expectations above. */
  const numbered = (n: number) => Array.from({ length: n }, (_, i) => `line-${i}`)

  it('counts adds and removes past the LCS cell cap', () => {
    const before = numbered(1100)
    const after = [...before.slice(0, 1095), 'new-a', 'new-b', 'new-c']
    expect(before.length * after.length).toBeGreaterThan(1_000_000)
    expect(countLines(before.join('\n'), after.join('\n'))).toEqual({ added: 3, removed: 5 })
  })

  it('under-reports a pure reorder past the cap, unlike the LCS path below it', () => {
    const before = numbered(1200)
    const reordered = [...before].reverse()
    expect(before.length * reordered.length).toBeGreaterThan(1_000_000)
    expect(countLines(before.join('\n'), reordered.join('\n'))).toEqual({ added: 0, removed: 0 })

    // The same reorder under the cap runs through LCS, which does attribute it.
    const small = numbered(40)
    expect(countLines(small.join('\n'), [...small].reverse().join('\n')).added).toBeGreaterThan(0)
  })

  it('nets out duplicate lines past the cap instead of counting every occurrence', () => {
    const before = [...numbered(1100), 'dup', 'dup', 'dup']
    const after = [...numbered(1100), 'dup']
    expect(before.length * after.length).toBeGreaterThan(1_000_000)
    expect(countLines(before.join('\n'), after.join('\n'))).toEqual({ added: 0, removed: 2 })
  })
})

describe('FileChangeChips', () => {
  it('renders nothing when fileChanges is empty', () => {
    const { container } = render(<FileChangeChips fileChanges={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing when fileChanges is undefined', () => {
    // Component guards against undefined too — keeps consumers from needing
    // their own falsy guard.
    const { container } = render(<FileChangeChips fileChanges={undefined as unknown as FileChangeEntry[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders one row per file change', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'a\nb'), change('/b.py', 'x', 'y')]} />,
    )
    expect(rows(container)).toHaveLength(2)
    expect(container.querySelector('[data-testid="fcc-row-/a.ts"]')).toBeInTheDocument()
    expect(container.querySelector('[data-testid="fcc-row-/b.py"]')).toBeInTheDocument()
  })

  it('keys rows by path (no duplicate-key warnings)', () => {
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { container } = render(
      <FileChangeChips fileChanges={[change('/x.ts', 'a', 'b'), change('/y.ts', 'a', 'b')]} />,
    )
    expect(rows(container)).toHaveLength(2)
    expect(warn).not.toHaveBeenCalledWith(expect.stringContaining('same key'))
    warn.mockRestore()
  })

  it('spells out the file count and aggregate totals in the card header', () => {
    render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'a\nb'), change('/b.ts', 'a\nb', 'a')]} />)
    expect(screen.getByText('2 files changed')).toBeInTheDocument()
    // Singular forms, and no second ±pair on the right of the same row.
    expect(screen.getByText('1 addition')).toBeInTheDocument()
    expect(screen.getByText('1 removal')).toBeInTheDocument()
  })

  it('pluralises the header totals and omits a side that is zero', () => {
    render(<FileChangeChips fileChanges={[change('/new.ts', '', 'a\nb\nc')]} />)
    expect(screen.getByText('3 additions')).toBeInTheDocument()
    expect(screen.queryByText(/removal/)).not.toBeInTheDocument()
  })

  it('shows an unavailable state instead of counts or an inline diff for truncated snapshots', () => {
    const file = { ...change('/large.ts', 'same', 'same'), truncated: true, snapshot_limit_chars: 200_000 }
    render(<FileChangeChips fileChanges={[file]} />)
    expect(screen.getByText('Diff unavailable: file is too large to compare (over 200,000 characters).')).toBeInTheDocument()
    expect(screen.queryByText('no changes')).not.toBeInTheDocument()
    expect(screen.queryByText(/addition|removal/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Show or hide the diff for /large.ts')).not.toBeInTheDocument()
  })

  const TURN_SENTENCE = "This turn's changes were too large to keep every diff (over 400,000 characters total)."
  const OPEN_HINT = 'Click a file name to open it.'
  const demoted = (path: string) => ({ ...change(path, '', ''), truncated: true, content_omitted: true, turn_budget_chars: 400_000 })

  it('names the turn budget, not this file\'s size, when a turn drops a file\'s content', () => {
    // A small file demoted to path-only must not be described as too large:
    // the size that ran out belongs to the turn, not to this file.
    render(<FileChangeChips fileChanges={[demoted('/small.ts')]} />)
    expect(screen.getByText(TURN_SENTENCE)).toBeInTheDocument()
    expect(screen.queryByText(/too large to compare/)).not.toBeInTheDocument()
  })

  it('says the turn-budget sentence once per card and tags each demoted row', () => {
    // The sentence describes the turn. Three demoted rows carry three short
    // tags under ONE notice row, so each row keeps its width for the filename.
    const { container } = render(
      <FileChangeChips fileChanges={[change('/kept.ts', 'a', 'a\nb'), demoted('/a.ts'), demoted('/b.ts'), demoted('/c.ts')]} />,
    )
    expect(screen.getAllByText(TURN_SENTENCE)).toHaveLength(1)
    expect(container.querySelector('[data-fcc-turn-notice]')).toBeInTheDocument()
    expect(screen.getAllByText('Diff not kept')).toHaveLength(3)
    for (const path of ['/a.ts', '/b.ts', '/c.ts']) {
      expect(screen.getByTestId(`fcc-row-${path}`).querySelector('[data-fcc-demoted-tag]')).toBeInTheDocument()
    }
    expect(screen.getByTestId('fcc-row-/kept.ts').querySelector('[data-fcc-demoted-tag]')).toBeNull()
  })

  it('renders no notice row and no tag when nothing was demoted', () => {
    const { container } = render(<FileChangeChips fileChanges={[change('/kept.ts', 'a', 'a\nb')]} />)
    expect(container.querySelector('[data-fcc-turn-notice]')).toBeNull()
    expect(screen.queryByText('Diff not kept')).not.toBeInTheDocument()
  })

  it('keeps the per-file notice on its own row: that one really is about one file', () => {
    const large = { ...change('/large.ts', 'same', 'same'), truncated: true, snapshot_limit_chars: 200_000 }
    render(<FileChangeChips fileChanges={[large, demoted('/small.ts')]} />)
    expect(screen.getByTestId('fcc-row-/large.ts')).toHaveTextContent('Diff unavailable: file is too large to compare (over 200,000 characters).')
    expect(screen.getByTestId('fcc-row-/large.ts').querySelector('[data-fcc-demoted-tag]')).toBeNull()
    expect(screen.getByTestId('fcc-row-/small.ts')).toHaveTextContent('Diff not kept')
    expect(screen.getByTestId('fcc-row-/small.ts')).not.toHaveTextContent(/too large/)
  })

  it('tells the reader the files still open only when opening is wired', () => {
    // A demoted row's filename is a button exactly when onFileOpen exists;
    // the hint must not promise a click that does nothing.
    const onFileOpen = vi.fn()
    const { unmount } = render(<FileChangeChips fileChanges={[demoted('/small.ts')]} onFileOpen={onFileOpen} />)
    expect(screen.getByText(new RegExp(OPEN_HINT.replace('.', '\\.')))).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open /small.ts in side panel' }))
    expect(onFileOpen).toHaveBeenCalledWith('/small.ts')
    unmount()
    render(<FileChangeChips fileChanges={[demoted('/small.ts')]} />)
    expect(screen.queryByText(/Click a file name/)).not.toBeInTheDocument()
  })

  it('minimal style keeps the whole turn-budget sentence in the pill, never the row tag', () => {
    // A pill stands alone with no card to carry the sentence for it.
    render(<FileChangeChips fileChanges={[demoted('/small.ts')]} style="minimal" />)
    expect(screen.getByLabelText('/small.ts')).toHaveTextContent(TURN_SENTENCE)
    expect(screen.queryByText('Diff not kept')).not.toBeInTheDocument()
    expect(screen.queryByText(/too large to compare/)).not.toBeInTheDocument()
  })

  it('opens a path-only minimal chip as the file, not an empty diff', () => {
    const onOpenDiff = vi.fn()
    const onFileOpen = vi.fn()
    render(
      <FileChangeChips
        fileChanges={[demoted('/small.ts')]}
        style="minimal"
        onOpenDiff={onOpenDiff}
        onFileOpen={onFileOpen}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '/small.ts' }))
    expect(onFileOpen).toHaveBeenCalledWith('/small.ts')
    expect(onOpenDiff).not.toHaveBeenCalled()
  })

  it('suppresses aggregate totals when any snapshot in the batch is truncated', () => {
    const truncated = { ...change('/large.ts', 'same', 'same'), truncated: true, snapshot_limit_chars: 200_000 }
    render(<FileChangeChips fileChanges={[truncated, change('/complete.ts', 'a', 'a\nb')]} />)
    expect(screen.getByText('2 files changed')).toBeInTheDocument()
    expect(screen.queryByText(/addition|removal/)).not.toBeInTheDocument()
  })

  it('opens a truncated minimal chip as the file, not an unavailable diff', () => {
    const onOpenDiff = vi.fn()
    const onFileOpen = vi.fn()
    const file = { ...change('/large.ts', 'before', 'after'), truncated: true, snapshot_limit_chars: 200_000 }
    render(
      <FileChangeChips
        fileChanges={[file]}
        style="minimal"
        onOpenDiff={onOpenDiff}
        onFileOpen={onFileOpen}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '/large.ts' }))
    expect(onFileOpen).toHaveBeenCalledWith('/large.ts')
    expect(onOpenDiff).not.toHaveBeenCalled()
  })

  it('lets a truncated minimal chip wrap within a narrow pane', () => {
    const file = { ...change('/large.ts', 'before', 'after'), truncated: true, snapshot_limit_chars: 200_000 }
    render(<FileChangeChips fileChanges={[file]} style="minimal" />)
    const chip = screen.getByLabelText('/large.ts')
    expect(chip).toHaveClass('max-w-full', 'min-w-0', 'min-h-[22px]', 'h-auto', 'whitespace-normal', 'py-1')
    expect(chip).not.toHaveClass('h-[22px]')
  })

  it('falls back to expanded for an unknown style value', () => {
    // Defensive default in the renderer map covers stale localStorage values
    // (e.g. legacy "tooltip"/"compact"/"full") until the migration runs.
    const { container } = render(
      <FileChangeChips
        fileChanges={[change('/legacy.ts', 'a', 'a\nb')]}
        // @ts-expect-error — intentional invalid style for the fallback path
        style="tooltip"
      />,
    )
    expect(container.querySelector('[data-testid="fcc-row-/legacy.ts"]')).toBeInTheDocument()
  })

  it('caps long lists at 8 rows behind a "Show N more" toggle', () => {
    const files = Array.from({ length: 11 }, (_, i) => change(`/f${i}.ts`, 'a', 'a\nb'))
    const { container } = render(<FileChangeChips fileChanges={files} />)
    expect(rows(container)).toHaveLength(8)
    // Header still reports the TRUE total.
    expect(screen.getByText('11 files changed')).toBeInTheDocument()
    // Expand reveals the remainder…
    fireEvent.click(screen.getByText('Show 3 more'))
    expect(rows(container)).toHaveLength(11)
    // …and collapses again.
    fireEvent.click(screen.getByText('Show less'))
    expect(rows(container)).toHaveLength(8)
  })

  it('does not cap lists at or below the threshold', () => {
    const files = Array.from({ length: 8 }, (_, i) => change(`/s${i}.ts`, 'a', 'b'))
    const { container } = render(<FileChangeChips fileChanges={files} />)
    expect(screen.queryByText(/Show \d+ more/)).not.toBeInTheDocument()
    expect(rows(container)).toHaveLength(8)
  })

  /* ── Header click routing. Pierre owns the header's inner nodes and its lazy
   *   chunk never resolves under vitest, so the real `[data-title]` cannot be
   *   clicked here — and a hand-stubbed `composedPath` does not survive React's
   *   dispatch, which resolves the target from that same path. The rule is
   *   therefore a pure function of the path and is tested as one; that the
   *   'toggle' verdict visibly collapses the row is Playwright's job. */
  const pathOf = (...sels: string[]) => sels.map(s => {
    const el = document.createElement('div')
    el.setAttribute(s, '')
    return el
  })

  it('routes a click on Pierre’s filename node to opening the file', () => {
    expect(headerClickAction(pathOf('data-title', 'data-diffs-header'))).toBe('open')
  })

  it('routes header whitespace to toggling, so the row has no dead zone', () => {
    expect(headerClickAction(pathOf('data-diffs-header'))).toBe('toggle')
  })

  it('routes lightweight header whitespace to toggling', () => {
    expect(headerClickAction(pathOf('data-fcc-header'))).toBe('toggle')
  })

  it('routes a lightweight filename to opening the file', () => {
    expect(headerClickAction(pathOf('data-fcc-filename', 'data-fcc-header'))).toBe('open')
  })

  it('ignores clicks below the header, so selecting code never collapses it', () => {
    expect(headerClickAction(pathOf('data-code'))).toBe('ignore')
    expect(headerClickAction([])).toBe('ignore')
  })

  it('ignores a filename node that is not inside a header', () => {
    // Order in the path is irrelevant, presence is what decides — but a title
    // without a header is not a header click at all.
    expect(headerClickAction(pathOf('data-title'))).toBe('ignore')
  })

  it('carries the full path as a tooltip so duplicate basenames stay distinguishable', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/src/a/index.ts', 'a', 'b'), change('/src/b/index.ts', 'a', 'b')]} />,
    )
    const titles = [...container.querySelectorAll('[data-testid^="fcc-row-"]')].map(r => r.getAttribute('title'))
    expect(titles).toEqual(['/src/a/index.ts', '/src/b/index.ts'])
  })

  /* ── Minimal style: hand-rolled pills, no Pierre, so still fully assertable
   *   here — and the one style that still routes clicks to `onOpenDiff`. */
  it('minimal style hides the filename in the pill but exposes a hover label', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/minimal.ts', 'a', 'a\nb')]} style="minimal" />,
    )
    const button = container.querySelector('button')
    expect(button?.textContent).not.toContain('minimal.ts')
    expect(container.textContent).toContain('minimal.ts')
    expect(button?.textContent).toContain('+1')
  })

  it('minimal style shows a no-changes caption when before === after', () => {
    render(<FileChangeChips fileChanges={[change('/same.ts', 'x', 'x')]} style="minimal" />)
    expect(screen.getByText('no changes')).toBeInTheDocument()
  })

  it('minimal style click triggers onOpenDiff with (path, after, before)', () => {
    const onOpenDiff = vi.fn()
    const { container } = render(
      <FileChangeChips
        fileChanges={[change('/min-click.ts', 'a', 'b')]}
        style="minimal"
        onOpenDiff={onOpenDiff}
      />,
    )
    fireEvent.click(container.querySelector('button')!)
    expect(onOpenDiff).toHaveBeenCalledWith('/min-click.ts', 'b', 'a')
  })

  it('does not throw on a minimal click when onOpenDiff is missing', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/no-handler.ts', 'a', 'b')]} style="minimal" />,
    )
    expect(() => fireEvent.click(container.querySelector('button')!)).not.toThrow()
  })
})
