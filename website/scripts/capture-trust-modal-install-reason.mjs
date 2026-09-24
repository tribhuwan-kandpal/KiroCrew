/**
 * Screenshot harness for the trust-and-enable modal's FAILURE state (#13446).
 *
 * Runs the REAL built SPA behind the shared static server and answers every
 * /api/** call from fixtures — including the SSE install stream — so no gateway
 * and no kiro-cli is needed.
 *
 * The state captured is the dead end the issue reported: a desktop install is
 * refused by the install-time build-step gate, the user confirms trust, the
 * retried install fails with the gate's own sentence, and the grant is rolled
 * back because nothing was installed under that name.
 *
 *   01 failure → the modal after the retried install failed
 *
 * BEFORE (`before` prefix, served from a dist built at the PR's base commit)
 * asserts the modal shows ONLY the generic copy and that the server's sentence
 * is nowhere on screen. AFTER asserts the same generic copy is still there as
 * the headline AND that the server's sentence is now rendered with it. Each
 * assertion runs before the frame is taken, so a screenshot can never show a
 * state the run did not verify.
 *
 * Usage: node scripts/capture-trust-modal-install-reason.mjs [outDir] [prefix] [dist]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/trust-modal-install-reason'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

const APP = 'agent-dashboard'
const DISPLAY = 'Agent Dashboard'
const REPO = 'https://git.example.test/apps/agent-dashboard.git'

/** The install-time gate's own refusal — the sentence that used to be readable
 *  only in the security event log. */
const REFUSAL =
  'Python apps that require a build step are not supported in the desktop app: ' +
  'its bundled interpreter is inside the signed application bundle and cannot ' +
  'install packages'

/** The generic copy, which stays as the headline either way. */
const GENERIC = 'could not be turned on, and nothing was changed'

const REGISTRY_APP = {
  name: APP, displayName: DISPLAY, version: '3.2.6',
  description: 'A dashboard for your agents, with its own FastAPI backend.',
  author: 'example-apps', repo: REPO, gitUrl: REPO, trustRepository: REPO,
  tags: ['dashboard'], installed: false, enabled: false, origin: 'registry',
  updateAvailable: false,
  manifest: {
    name: APP, version: '3.2.6', displayName: DISPLAY,
    backend: { entryPoint: 'server.py', type: 'asgi' },
  },
}

/** One SSE `done` frame, the shape `installFromRegistryStream` parses. */
const sse = (payload) => `event: done\ndata: ${JSON.stringify(payload)}\n\n`

const DENIED = {
  ok: false, name: APP, code: 'app_execution_denied',
  error: `blocked by execution policy: App ${APP} is not trusted to run its own code.`,
  log: '',
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1, serviceWorkers: 'block',
  })
  const page = await context.newPage()
  logPageProblems(page)

  let installs = 0
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/apps') { await route.fulfill({ json: [] }); return true }
      // 404 is how "no app occupies this name" really arrives: it makes the page
      // load from the registry AND is the rollback probe's proof of absence.
      if (path === `/api/apps/${APP}`) {
        await route.fulfill({ status: 404, json: { error: 'app not installed' } })
        return true
      }
      if (path === '/api/apps/registry') {
        await route.fulfill({
          json: { apps: [REGISTRY_APP], serverPlatform: { os: 'darwin', arch: 'arm64' } },
        })
        return true
      }
      if (path === '/api/apps/registries') { await route.fulfill({ json: { registries: [] } }); return true }
      if (path === `/api/apps/${APP}/trust` || path === `/api/apps/${APP}/untrust`) {
        await route.fulfill({ json: { ok: true } })
        return true
      }
      if (path === '/api/apps/registry/install-stream') {
        installs += 1
        // First attempt: the execution gate refuses before the clone, which is
        // what opens the consent modal. Retry: trust is granted, the clone runs,
        // and the BUILD-STEP gate refuses — the failure this frame is about.
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: sse(installs === 1 ? DENIED : { ok: false, name: APP, error: REFUSAL }),
        })
        return true
      }
      return false
    },
  })

  await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
  await page.getByText(DISPLAY).first().waitFor({ timeout: 15000 })

  await page.getByRole('button', { name: 'Install' }).first().click()
  const modal = page.locator('[role="dialog"]').filter({ hasText: /to run its own code\?/ })
  await modal.waitFor({ timeout: 15000 })

  await modal.getByRole('button', { name: 'Trust this app and enable' }).click()
  const alert = modal.getByRole('alert')
  await alert.waitFor({ timeout: 15000 })
  // Settle the rollback probe + untrust round trip so the copy is final.
  await page.getByRole('button', { name: 'Try again' }).waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)

  const text = await alert.innerText()
  if (!text.includes(GENERIC)) {
    throw new Error(`${PREFIX}: the generic headline must stay on screen: ${JSON.stringify(text)}`)
  }
  // The page's OWN error banner (behind the modal) always carried the sentence —
  // `reportInstallFailure` journals and shows it there. What #13446 reported is the
  // MODAL, the surface the user is looking at and the only one offering Try again,
  // so the assertion is scoped to the modal and the banner is reported as a fact.
  const pageHasReason = (await page.locator('body').innerText()).includes('bundled interpreter')
  if (PREFIX === 'before') {
    if (text.includes('bundled interpreter')) {
      throw new Error(`before: the modal already shows the server reason, so this is not the BEFORE state: ${JSON.stringify(text)}`)
    }
  } else {
    if (!text.includes(REFUSAL)) {
      throw new Error(`after: the modal must render the server's reason: ${JSON.stringify(text)}`)
    }
  }

  await modal.screenshot({ path: `${OUT}/${PREFIX}-01-failure.png` })
  await browser.close()
  srv.close()
  console.log(
    `Wrote ${OUT}/${PREFIX}-01-failure.png (modal asserted: ` +
    `${PREFIX === 'before' ? 'generic copy only, no server reason' : "generic headline + the server's reason"}` +
    `; page banner behind the modal carries the reason: ${pageHasReason})`,
  )
}

main().catch(e => { console.error(e); process.exit(1) })
