/**
 * Screenshot harness for the action strip under a pinned long user prompt.
 *
 * A user prompt taller than the viewport hands over to the pinned-prompt card
 * the moment its row top crosses the fold, and its row is hidden for as long as
 * the card stands in for it. The row also carries the message's action strip
 * (copy, copy link, pin, timestamp), so before the fix that strip was never on
 * screen for a tall prompt: its bottom only comes into view once its top is
 * above the fold, i.e. once the whole row is hidden. This scene drives the REAL
 * transcript (built SPA, `/api/**` fixtures) into exactly that state and reads
 * the strip's fate off the live DOM, not the pixels alone:
 *
 *   - `expected=after`  (fixed build): the strip is visible and hit-testable
 *     beneath the card, the card's bottom sits at or above the strip, and a
 *     click on Copy lands (the button flips to its copied state).
 *   - `expected=before` (unfixed build): the strip is `visibility: hidden` with
 *     the row — the defect, recorded so the pair is evidence of a change.
 *
 * Usage, from website/ after `npm run build`:
 *   node scripts/capture-long-user-bubble-actions.mjs [outDir] [--dist DIR] [--expected before|after]
 *
 * `--dist` points a run at another build of the same page (the before/after
 * pair is two runs, two dists, one script).
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')

const argv = process.argv.slice(2)
const flag = (name, fallback) => {
  const i = argv.indexOf(name)
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback
}
const OUT = argv.find((a, i) => !a.startsWith('--') && (i === 0 || !argv[i - 1].startsWith('--')))
  || '../temp-screenshots/long-user-bubble-actions'
const DIST = flag('--dist', undefined)
const EXPECTED = flag('--expected', 'after')
if (!['before', 'after'].includes(EXPECTED)) {
  console.error(`FAIL: --expected must be before|after, got ${JSON.stringify(EXPECTED)}`)
  process.exit(2)
}
const SLOT = 'chat-longbubble'
// Derived from this script's own location (scripts/ -> website/ -> repo root),
// never hardcoded: this path RENDERS into the captured screenshot, so a personal
// absolute path both leaks a home directory and misrepresents any other checkout.
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
// Far taller than any viewport: 70 lines at ~23px each is ~1600px of bubble.
const LONG_PROMPT = [
  'Please review the deployment plan below before I hand it to the team.',
  ...Array.from({ length: 68 }, (_, i) => `Step ${i + 1}: check the ${['cache', 'queue', 'index', 'replica', 'gateway'][i % 5]} rollout order, confirm the health check window, and record who signs off.`),
  'That is the whole plan. Tell me what is missing.',
].join('\n')
const REPLY = [
  'Read the plan end to end. Two gaps: the queue drain in step 12 has no owner, and the replica',
  'cutover in step 40 lands inside the health check window it is supposed to wait for.',
  '',
  'Everything else is ordered correctly. Steps 1-11 can run in parallel; the rest is strictly serial.',
].join('\n')

const slots = [{
  key: SLOT, title: 'Deployment plan review', running: false,
  last_message: 'Everything else is ordered correctly.', messages: 4, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 3000, content: 'Morning. I have a long one coming.' },
    { role: 'assistant', ts: now - 2950, content: 'Go ahead, paste it in full.' },
    { role: 'user', ts: now - 900, content: LONG_PROMPT },
    { role: 'assistant', ts: now - 30, content: REPLY },
  ],
}

let failures = 0
const assert = (label, ok) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
  if (!ok) failures += 1
}

/** Geometry + visibility of the long prompt's row, bubble, strip and the card. */
async function inspect(page) {
  return page.evaluate(() => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const row = rows.find(r => r.textContent.includes('Step 68:'))
    if (!row) return { error: 'long prompt row not mounted' }
    const bubble = row.querySelector('.message-bubble')
    const copy = row.querySelector('button[title="Copy"]')
    const strip = copy ? copy.parentElement : null
    const card = document.querySelector('[data-testid="pinned-prompt"]')
    const scroller = document.querySelector('.chat-container')
    const r = el => { const b = el.getBoundingClientRect(); return { top: b.top, bottom: b.bottom, left: b.left, right: b.right, height: b.height } }
    const mid = el => { const b = el.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2] }
    let copyHit = null
    if (copy) {
      const [x, y] = mid(copy)
      const hit = document.elementFromPoint(x, y)
      copyHit = hit ? (hit === copy || copy.contains(hit)) : false
    }
    return {
      rowHidden: row.style.visibility === 'hidden',
      rowStandin: row.hasAttribute('data-pinned-standin'),
      row: r(row),
      bubble: bubble ? r(bubble) : null,
      strip: strip ? { ...r(strip), visibility: getComputedStyle(strip).visibility, opacity: getComputedStyle(strip).opacity } : null,
      copy: copy ? { ...r(copy), visibility: getComputedStyle(copy).visibility, hit: copyHit, label: copy.getAttribute('aria-label') } : null,
      card: card ? { ...r(card), text: (card.textContent || '').slice(0, 40) } : null,
      viewport: { w: innerWidth, h: innerHeight },
      scrollTop: scroller ? scroller.scrollTop : null,
    }
  })
}

/** Scroll so the long prompt's bubble bottom sits at `frac` of the viewport. */
async function placeBubbleBottom(page, frac) {
  await page.evaluate((f) => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const row = rows.find(r => r.textContent.includes('Step 68:'))
    const bubble = row.querySelector('.message-bubble')
    const scroller = document.querySelector('.chat-container')
    const target = scroller.getBoundingClientRect().top + scroller.clientHeight * f
    scroller.scrollTop += bubble.getBoundingClientRect().bottom - target
  }, frac)
  await page.waitForTimeout(500)
}

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    viewport: { width: 1280, height: 860 },
    dist: DIST,
  })
  const shot = async (name, clip) => {
    const path = `${OUT}/${name}.png`
    await h.page.screenshot({ path, ...(clip ? { clip } : {}) })
    console.log('wrote', path)
  }

  for (const theme of ['dark', 'light']) {
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1200 })
    // The transcript boots at its bottom (the reply). Bring the long prompt's
    // bottom to mid-viewport: its top is then far above the fold, so the row is
    // the pinned stand-in and its strip sits under the folding card.
    await placeBubbleBottom(h.page, 0.5)
    await placeBubbleBottom(h.page, 0.5) // second pass: the fold re-measured heights after the first
    const g = await inspect(h.page)
    if (g.error) { assert(g.error, false); break }
    console.log(`${theme}: ${JSON.stringify(g)}`)
    assert(`${theme}: long prompt row is the pinned stand-in (hidden by visibility)`, g.rowHidden)
    assert(`${theme}: pinned card is mounted`, !!g.card)
    assert(`${theme}: bubble bottom is on screen (${Math.round(g.bubble?.bottom ?? -1)}px of ${g.viewport.h})`,
      !!g.bubble && g.bubble.bottom > 0 && g.bubble.bottom < g.viewport.h)
    assert(`${theme}: the action strip renders inside the row`, !!g.strip && !!g.copy)
    if (EXPECTED === 'after') {
      assert(`${theme}: row carries data-pinned-standin`, g.rowStandin)
      assert(`${theme}: strip is visible (computed ${g.strip?.visibility}, opacity ${g.strip?.opacity})`,
        g.strip?.visibility === 'visible' && g.strip?.opacity === '1')
      assert(`${theme}: card bottom (${Math.round(g.card?.bottom ?? 0)}) does not cover the strip top (${Math.round(g.strip?.top ?? 0)})`,
        !!g.card && !!g.strip && g.card.bottom <= g.strip.top + 0.5)
      assert(`${theme}: Copy is the element under its own centre (hit-testable)`, g.copy?.hit === true)
    } else {
      assert(`${theme}: [defect] strip is hidden with the row (computed ${g.strip?.visibility})`, g.strip?.visibility === 'hidden')
      assert(`${theme}: [defect] Copy is not hit-testable`, g.copy?.hit === false)
    }
    await shot(`${EXPECTED}-01-pinned-long-prompt-${theme}`)
    // Zoom on the seam: the card's bottom edge and the strip beneath it.
    const seamTop = Math.max(0, Math.round((g.bubble?.bottom ?? 300) - 220))
    await shot(`${EXPECTED}-02-strip-under-card-${theme}`, { x: 0, y: seamTop, width: g.viewport.w, height: 320 })
    if (EXPECTED === 'after' && theme === 'dark') {
      // The click must land on the row's button, not on the card overlay. Either
      // outcome label proves the click reached it; the clipboard grant is what
      // lets the successful one show.
      await h.page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: h.base })
      await h.page.locator('[data-display-index]').filter({ hasText: 'Step 68:' }).locator('button[title="Copy"]').click({ timeout: 5000 })
      await h.page.waitForTimeout(250)
      const after = await inspect(h.page)
      assert(`dark: Copy click landed (label now "${after.copy?.label}")`, /copied|copy failed/i.test(after.copy?.label || ''))
      await shot(`${EXPECTED}-03-copy-clicked-${theme}`, { x: 0, y: seamTop, width: g.viewport.w, height: 320 })
    }
  }

  await h.close()
  console.log(failures === 0 ? 'ALL ASSERTIONS PASSED' : `${failures} ASSERTION(S) FAILED`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch(err => { console.error(err); process.exit(1) })
