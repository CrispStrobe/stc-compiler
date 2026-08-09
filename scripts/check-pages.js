// check-pages.js — drive the GitHub Pages app in a real browser.
//
// scripts/test-pages.py checks the page is WIRED correctly -- that the copies
// match, that Pyodide is pinned, that compiling is delegated. None of that
// proves CPython actually starts in a browser and transpiles anything, and
// that is the whole claim the page makes.
//
//   (cd docs && python3 -m http.server 8765 &)
//   PW_CHANNEL=chrome node scripts/check-pages.js http://localhost:8765
//
const { chromium } = require('playwright');

(async () => {
  const base = process.argv[2];
    // PW_CHANNEL=chrome uses an already-installed Chrome, which is how this
  // runs locally without downloading a second browser. CI leaves it unset and
  // gets Playwright's own chromium.
  const channel = process.env.PW_CHANNEL;
  const browser = await chromium.launch(channel ? { channel } : {});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.goto(base + '/index.html', { waitUntil: 'load' });

  // Pyodide downloads a few MB of wasm; give it room.
  await page.waitForFunction(
    () => document.getElementById('status').className === 'ok'
       || document.getElementById('status').className === 'err',
    null, { timeout: 180000 });

  const status = await page.textContent('#status');
  const cls = await page.getAttribute('#status', 'class');
  console.log(`  status[${cls}] ${status}`);
  if (cls !== 'ok') { console.log('  FAILED to start'); process.exit(1); }

  let fails = 0;
  const ok = (name, cond, detail = '') => {
    console.log(`  ${cond ? '\x1b[32mok \x1b[0m' : '\x1b[31mFAIL\x1b[0m'} ${name} ${detail}`);
    if (!cond) fails++;
  };

  // Every example, transpiled in the browser by the real transpiler.
  const names = await page.$$eval('#example option', os => os.map(o => o.value));
  for (const name of names) {
    await page.selectOption('#example', name);
    await page.waitForTimeout(400);
    const out = await page.textContent('#out');
    const label = await page.textContent('#outlabel');
    const cls2 = await page.getAttribute('#status', 'class');
    const st = await page.textContent('#status');
    ok(`"${name}"`, cls2 === 'ok' && out.length > 100, `-> ${label}  ${st.slice(0, 64)}`);
  }

  // The micro:bit example must come out as MicroPython, not C.
  await page.selectOption('#example', names.find(n => n.includes('micro:bit')));
  await page.waitForTimeout(400);
  const mb = await page.textContent('#out');
  ok('micro:bit emits MicroPython', mb.includes('from microbit import *') && !mb.includes('#include'));
  ok('...with generators, not a switch', mb.includes('yield'));
  ok('...named from the NAME line', (await page.textContent('#outlabel')) === 'chirp.py');

  // A parse error must be reported, not thrown.
  await page.fill('#source', 'DEVICE MICROBIT:\n  PIN x = P99 OUTPUT\n  WHEN started:\n    wait 1 ms\n');
  await page.click('#go');
  await page.waitForTimeout(300);
  ok('a bad pin is an error message, not a crash',
     (await page.getAttribute('#status', 'class')) === 'err'
     && (await page.textContent('#out')).includes('P0-P20'));

  // Canonical pseudocode view.
  await page.selectOption('#example', names[0]);
  await page.waitForTimeout(400);
  await page.selectOption('#view', 'canon');
  await page.waitForTimeout(200);
  ok('canonical pseudocode round-trips in-page',
     (await page.textContent('#out')).includes('NAME blink'));

  ok('no uncaught page errors', errors.length === 0, errors.slice(0, 2).join(' | '));

  await browser.close();
  console.log(fails ? `\n  ${fails} failed` : '\n  all good');
  process.exit(fails ? 1 : 0);
})();
