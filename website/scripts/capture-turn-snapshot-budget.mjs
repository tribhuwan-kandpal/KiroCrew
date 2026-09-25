/**
 * Screenshots for the file-change card when a turn's snapshot budget dropped a
 * file's content.
 *
 * `_apply_turn_snapshot_budget` keeps the newest real change whole and demotes
 * the rest to path-only. A demoted row carries `content_omitted`, so the card
 * says the turn-budget notice ONCE under its header, tags each demoted row, and
 * the row opens the real file rather than an empty comparison. Three frames per
 * theme:
 *
 *   01-card-mixed-<theme>    one kept diff above three demoted rows, the shape a
 *                            reader actually meets: the notice row sits under the
 *                            header, the kept diff is the first row, and each
 *                            demoted row shows its filename plus a short tag.
 *   02-row-notice-<theme>    the notice row alone, so its wording is legible at
 *                            full scale — it must name the turn's size, never
 *                            one file's, and tell the reader the files open.
 *   03-minimal-chip-<theme>  the minimal (glass pill) style of the same turn: a
 *                            demoted pill stands alone with no card to carry the
 *                            sentence, so the pill carries it and wraps.
 *
 * Runs the REAL built SPA (website/dist) behind the shared dashboard stub: no
 * gateway, no token, no network.
 *
 * Usage: node scripts/capture-turn-snapshot-budget.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/turn-snapshot-budget'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const MAX_EDGE = 2000
const TURN_BUDGET = 400000

mkdirSync(OUT, { recursive: true })

const KEPT_BEFORE = `def _flush_file_changes(slot):
    deduped = {}
    for fc in slot._file_changes:
        deduped[fc["path"]] = fc
    return list(deduped.values())`

const KEPT_AFTER = `def _flush_file_changes(slot):
    deduped = {}
    for write_order, fc in enumerate(slot._file_changes):
        deduped.setdefault(fc["path"], fc)
        deduped[fc["path"]]["_last_write"] = write_order
    fc_list, demoted, dropped = _apply_turn_snapshot_budget(list(deduped.values()))
    return fc_list`

/* The kept row FIRST, exactly as the backend orders them: entries that still
 * carry content sort ahead of the demoted ones. */
const DEMOTED = path => ({
  path,
  before: '',
  after: '',
  truncated: true,
  content_omitted: true,
  turn_budget_chars: TURN_BUDGET,
})
const FILE_CHANGES = [
  { path: 'src/kiro_crew/dashboard/chat_runner.py', before: KEPT_BEFORE, after: KEPT_AFTER },
  DEMOTED('src/kiro_crew/dashboard/chat_utils.py'),
  DEMOTED('website/src/store/chatSlice.ts'),
  DEMOTED('test/test_file_change_snapshots.py'),
]
const DEMOTED_ROW = 'src/kiro_crew/dashboard/chat_utils.py'
const TURN_SENTENCE = /too large to keep every diff/
const OPEN_HINT = /Click a file name to open it/
const ROW_TAG = 'Diff not kept'
const WRONG_NOTICE = /too large to compare/

const t0 = Math.floor(Date.now() / 1000) - 900
const SURFACE = {
  key: 'chat-turn-snapshot-budget',
  title: 'Turn snapshot budget',
  messages: [
    { role: 'user', content: 'Bound what one turn keeps.', ts: String(t0) },
    {
      role: 'assistant',
      ts: String(t0 + 120),
      content: 'Done — the newest change keeps its diff and the older files keep only their path.',
      meta: { file_changes: FILE_CHANGES },
    },
  ],
}

const slots = [{
  key: SURFACE.key,
  title: SURFACE.title,
  running: false,
  last_message: SURFACE.title,
  messages: SURFACE.messages.length,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detailByKey = {
  [SURFACE.key]: {
    running: false,
    has_more: false,
    total: SURFACE.messages.length,
    queue: [],
    messages: SURFACE.messages,
  },
}

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const wrote = []

  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({
      viewport: { width: 1180, height: 900 },
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()

    const extra = async (path, route) => {
      if (path === '/api/chat/slots') return json(route, slots), true
      const m = /^\/api\/chat\/slots\/([^/]+)/.exec(path)
      if (m) {
        const d = detailByKey[decodeURIComponent(m[1])]
        if (d) return json(route, d), true
      }
      if (path === '/api/file-read') return route.fulfill({ status: 200, body: '' }), true
      if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
      return false
    }
    // The boot route decides the palette, so the theme has to reach the STUB;
    // localStorage alone is overwritten by what /api/theme/boot answers.
    await stubDashboardApi(page, { slots, extra, theme })
    logPageProblems(page)

    async function shot(locator, name) {
      const file = `${OUT}/${name}.png`
      await locator.screenshot({ path: file })
      const { w, h } = pngSize(file)
      const flag = w > MAX_EDGE || h > MAX_EDGE ? '  ⚠️ OVER 2000px' : ''
      console.log(`wrote ${file}  ${w}x${h}${flag}`)
      wrote.push({ file, w, h, over: !!flag })
    }

    async function open(fileChipStyle) {
      await page.addInitScript(([key, t, style]) => {
        localStorage.clear()
        localStorage.setItem('mc-theme', t)
        localStorage.setItem('mc-onboarded', '1')
        localStorage.setItem('mc-active-slot-chat', key)
        localStorage.setItem('mc-chat-config', JSON.stringify({
          pinLastPrompt: false,
          fileChipStyle: style,
          streamMode: 'immediate',
        }))
      }, [SURFACE.key, theme, fileChipStyle])

      await page.goto(base + '/?sid=' + encodeURIComponent(SURFACE.key), { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(2600)
      await page.keyboard.press('Escape')
      const close = page.locator('[aria-label="Close"]')
      if (await close.count()) await close.first().click().catch(() => {})
      await page.waitForTimeout(400)
    }

    /* ── expanded card ─────────────────────────────────────────────────── */
    await open('expanded')
    const card = page.locator('div.ft-block-reveal:has([data-testid^="fcc-row-"])').first()
    await card.waitFor({ state: 'visible', timeout: 15000 })

    /* The assertions ARE the capture: a frame is only worth reading if the
     * notice it is meant to show is the one on screen. */
    const rows = page.locator('[data-testid^="fcc-row-"]')
    const rowCount = await rows.count()
    if (rowCount !== FILE_CHANGES.length) {
      throw new Error(`expected ${FILE_CHANGES.length} rows, got ${rowCount}`)
    }
    const firstRow = await rows.first().getAttribute('data-testid')
    if (firstRow !== `fcc-row-${FILE_CHANGES[0].path}`) {
      throw new Error(`expected the kept diff first, got ${firstRow}`)
    }
    const noticeRow = card.locator('[data-fcc-turn-notice]')
    if ((await noticeRow.count()) !== 1) {
      throw new Error(`expected ONE turn-budget notice row per card, got ${await noticeRow.count()}`)
    }
    const sentences = await card.getByText(TURN_SENTENCE).count()
    if (sentences !== 1) {
      throw new Error(`expected the turn-budget sentence once per card, got ${sentences}`)
    }
    if (!(await noticeRow.getByText(OPEN_HINT).count())) {
      throw new Error('expected the notice row to say the files still open')
    }
    const demotedCount = FILE_CHANGES.filter(fc => fc.content_omitted).length
    const tags = await card.locator('[data-fcc-demoted-tag]').count()
    if (tags !== demotedCount) {
      throw new Error(`expected ${demotedCount} demoted-row tags, got ${tags}`)
    }
    for (const fc of FILE_CHANGES.filter(fc => fc.content_omitted)) {
      const row = page.locator(`[data-testid="fcc-row-${fc.path}"]`)
      if (!(await row.getByText(ROW_TAG).count())) {
        throw new Error(`expected the "${ROW_TAG}" tag on ${fc.path}`)
      }
      if (await row.getByText(TURN_SENTENCE).count()) {
        throw new Error(`the turn-budget sentence must not repeat on ${fc.path}`)
      }
    }
    if (await card.getByText(WRONG_NOTICE).count()) {
      throw new Error('a demoted row must not claim the FILE was too large to compare')
    }
    console.log(`${theme}: ${rowCount} rows, kept diff first, one notice row, ${tags} tags`)

    await shot(card, `01-card-mixed-${theme}`)
    await shot(noticeRow, `02-row-notice-${theme}`)

    /* ── minimal pills ─────────────────────────────────────────────────── */
    await open('minimal')
    const pill = page.locator(`button.file-chip[aria-label="${DEMOTED_ROW}"]`)
    await pill.waitFor({ state: 'visible', timeout: 15000 })
    const pills = page.locator('button.file-chip')
    const pillCount = await pills.count()
    if (pillCount !== FILE_CHANGES.length) {
      throw new Error(`expected ${FILE_CHANGES.length} pills, got ${pillCount}`)
    }
    if (!(await pill.getByText(TURN_SENTENCE).count())) {
      throw new Error('expected a demoted pill to carry the turn-budget sentence')
    }
    if (await pill.getByText(WRONG_NOTICE).count()) {
      throw new Error('a demoted pill must not claim the FILE was too large to compare')
    }
    if (await page.getByText(ROW_TAG).count()) {
      throw new Error('the row tag belongs to the expanded card, not to a pill')
    }
    const pillRow = page.locator('div.ft-block-reveal:has(button.file-chip)').first()
    console.log(`${theme}: ${pillCount} pills, demoted pill carries the sentence`)
    await shot(pillRow, `03-minimal-chip-${theme}`)

    await context.close()
  }

  await browser.close()
  srv.close()

  const over = wrote.filter(x => x.over)
  if (over.length) throw new Error('frames over the 2000px budget: ' + over.map(x => x.file).join(', '))
  console.log(`done — ${wrote.length} frames in ${OUT}`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
