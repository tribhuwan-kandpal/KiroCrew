/**
 * Row resizes the reader caused in place: a disclosure they opened or closed
 * inside a transcript row, below the top of the viewport.
 *
 * The virtualizer holds the reader still when a row straddling the top of the
 * viewport changes height, because a reprice of such a row usually moves its
 * visible part. A disclosure the reader toggles on screen is the opposite
 * case: everything above it stays where it is, and only what is below it
 * moves. Compensating that change scrolls the page by the disclosure's
 * height at the moment it opens or closes, which reads as the page bouncing.
 *
 * A note records WHERE the change happens (an element that stays mounted and
 * the viewport y of the change). The virtualizer's resize handler asks whether
 * a resized row holds a note at or below the fold, and treats such a change
 * like growth appended at the row's bottom: it moves nothing above the fold.
 * A note lasts two frames, which covers the layout and the resize observer
 * delivery that follow a one-step change, or `durationMs` for a change that
 * moves over several frames.
 */

interface Note {
  anchor: Element
  top: number
}

const notes = new Set<Note>()

/** Record that the reader is about to change a row's height in place at `top`
 *  (viewport y), inside `anchor`, over `durationMs` (0 for a single step). */
export function noteInPlaceResize(anchor: Element, top: number, durationMs = 0): void {
  const note = { anchor, top }
  notes.add(note)
  const drop = () => { notes.delete(note) }
  if (durationMs > 0) {
    setTimeout(drop, durationMs + 100)
  } else if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => requestAnimationFrame(drop))
  } else {
    setTimeout(drop, 50)
  }
}

/** True when `row` holds an in-place change at or below `foldTop`. */
export function resizedInPlaceBelow(row: Element, foldTop: number): boolean {
  for (const note of notes) {
    if (note.top >= foldTop && row.contains(note.anchor)) return true
  }
  return false
}
