const CLOSING_ASCII_PUNCTUATION = /^[,.;:!?)}\]]/u

/** Preserve authored whitespace and keep unspaced scripts continuous, while
 * preventing independently recognized Latin words from being glued together. */
export function dictationSeparator(before: string, after: string): string {
  if (!before || !after || /\s$/u.test(before) || /^\s/u.test(after)) return ''
  // A dictated insertion before an existing comma, sentence ending or closing
  // bracket must keep that punctuation attached to the inserted words.
  if (CLOSING_ASCII_PUNCTUATION.test(after)) return ''
  if (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]$/u.test(before) ||
      /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}，。！？、；：]/u.test(after)) return ''
  return ' '
}

export function joinTranscript(parts: string[]): string {
  return parts.map(part => part.trim()).filter(Boolean)
    .reduce((all, part) => all + dictationSeparator(all, part) + part, '')
}

/** A bounded recent caption, preserving unspaced scripts at the cut. Only a
 * partial word in a spaced script is advanced to the next transcript boundary. */
export function transcriptTail(text: string, maxChars: number): string {
  if (maxChars <= 0) return ''
  let start = Math.max(0, text.length - maxChars)
  // The character budget counts UTF-16 units; never leave half a Han character.
  if (start > 0 && /[\uDC00-\uDFFF]/u.test(text[start])) start++
  const tail = text.slice(start)
  if (!start || (!CLOSING_ASCII_PUNCTUATION.test(tail) &&
      !dictationSeparator(text.slice(0, start), tail))) return tail.trimStart()
  const characters = Array.from(tail)
  for (let i = 1; i < characters.length; i++) {
    // A comma belongs to the word being dropped, not the start of the caption.
    if (!CLOSING_ASCII_PUNCTUATION.test(characters[i]) &&
        !dictationSeparator(characters[i - 1], characters[i])) {
      return characters.slice(i).join('').trimStart()
    }
  }
  return tail
}

/** The one derivation of a dictation splice: the text on each side, the
 *  separators the splice adds, and the selected characters the caret displaces.
 *  The written value and the span a discard has to verify are both built from
 *  this, so the two can never describe different writes. */
function dictationSpliceParts(
  base: string,
  text: string,
  caret: { start: number; end: number } | null,
): { before: string; lead: string; trail: string; after: string; replaced: string } {
  if (!caret) return { before: base, lead: dictationSeparator(base, text), trail: '', after: '', replaced: '' }
  const start = Math.min(caret.start, base.length)
  const end = Math.min(caret.end, base.length)
  const before = base.slice(0, start)
  const after = base.slice(end)
  return {
    before,
    lead: dictationSeparator(before, text),
    trail: dictationSeparator(text, after),
    after,
    replaced: base.slice(start, end),
  }
}

export function spliceDictationText(
  base: string,
  text: string,
  caret: { start: number; end: number } | null,
): { value: string; caret: number } {
  // A silent hypothesis must not delete a selected portion of the draft.
  if (!text) return { value: base, caret: caret ? Math.min(caret.start, base.length) : base.length }
  const { before, lead, trail, after } = dictationSpliceParts(base, text, caret)
  const insert = lead + text
  return { value: before + insert + trail + after, caret: before.length + insert.length }
}

/** Which characters of the written value are the machine's, and what the write
 *  consumed to put them there.
 *
 *  A discard needs both halves. The span is what makes the dictated run
 *  identifiable as a POSITION rather than as a string, so an identical phrase the
 *  user typed elsewhere is never mistaken for it. `replaced` is the selection the
 *  splice deleted: dictating over selected words removes them, so dropping the
 *  span alone would give back a draft the user never wrote.
 *
 *  Derived from the same parts as the write rather than recorded when it happens,
 *  so it cannot go stale against a base that moved on. */
export function dictationSpliceSpan(
  base: string,
  text: string,
  caret: { start: number; end: number } | null,
): { start: number; end: number; text: string; replaced: string } | null {
  if (!text) return null
  const { before, lead, trail, replaced } = dictationSpliceParts(base, text, caret)
  const written = lead + text + trail
  return { start: before.length, end: before.length + written.length, text: written, replaced }
}

/** Where a written span sits in a value the user has edited since, or -1 when
 *  that cannot be established.
 *
 *  Everything between the common prefix and the common suffix of the written
 *  value and the current one is the user's edit. When that edit lies wholly to
 *  one side of the span, the span itself survived and its offset follows by
 *  arithmetic: an edit before it shifts it by the length delta, an edit after it
 *  leaves it where it was. An edit that reaches into the span means those
 *  characters are no longer only the machine's, and there is nothing a discard
 *  may safely take.
 *
 *  The offset is then checked against the span's own text before it is returned,
 *  the same way the polish path checks its offsets: a mismatch answers -1, never
 *  "remove anyway". */
export function locateDictationSpan(
  written: string,
  cur: string,
  span: { start: number; end: number; text: string },
): number {
  let head = 0
  while (head < written.length && head < cur.length && written[head] === cur[head]) head++
  let tail = 0
  while (
    tail < written.length - head
    && tail < cur.length - head
    && written[written.length - 1 - tail] === cur[cur.length - 1 - tail]
  ) tail++
  let at = -1
  if (written.length - tail <= span.start) at = span.start + (cur.length - written.length)
  else if (head >= span.end) at = span.start
  return at >= 0 && cur.slice(at, at + span.text.length) === span.text ? at : -1
}
