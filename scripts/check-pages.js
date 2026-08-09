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
// It also runs against the deployed site, which is the only way to prove the
// page users actually load carries the current transpiler:
//
//   PW_CHANNEL=chrome node scripts/check-pages.js \
//       https://crispstrobe.github.io/stc-compiler
//
// Installing playwright locally needs --prefix, unlike in CI: npm walks UP
// for a package.json, and a clone under a directory that has one will
// otherwise install into THAT tree rather than this one.
//
//   npm install --no-save --prefix "$PWD" playwright@1.60.0
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

  // A board fact, checked through the deployed transpiler rather than the
  // local one. The Nano's A6/A7 reach the pad with no digital buffer, so
  // digitalWrite to one is accepted by every layer and does nothing; the two
  // boards must refuse the same pin name for their own different reasons,
  // because one answer sends the reader to the package and the other to the
  // schematic. Locking this in HERE is the point: the page carries its own
  // copy of the transpiler, and a fix that never reached docs/ would pass
  // every local test and still ship the old rule to users.
  const refusal = async (device, decl) => {
    await page.fill('#source',
      `DEVICE ${device}:\n  PIN x = ${decl}\n  WHEN started:\n    wait 10 ms\n`);
    await page.click('#go');
    await page.waitForTimeout(300);
    return (await page.getAttribute('#status', 'class')) === 'err'
      ? await page.textContent('#out') : null;
  };
  const nanoDigital = await refusal('ARDUINO-NANO', 'A6 OUTPUT');
  ok('the live page refuses a digital write to the Nano\'s A6',
     /analog-IN only/.test(nanoDigital || ''), String(nanoDigital).slice(0, 72));
  const unoAbsent = await refusal('ARDUINO-UNO', 'A6 ANALOG');
  ok('...and refuses the Uno\'s A6 as absent instead',
     /A0-A5/.test(unoAbsent || ''), String(unoAbsent).slice(0, 72));
  ok('...with two different messages, not one shared "no such pin"',
     nanoDigital !== unoAbsent && !!nanoDigital && !!unoAbsent);

  await page.fill('#source',
    'DEVICE ARDUINO-NANO:\n  PIN pot = A6 ANALOG\n  WHEN started:\n'
    + '    FOREVER:\n      print pot\n      wait 10 ms\n');
  await page.click('#go');
  await page.waitForTimeout(300);
  ok('...while the one thing A6 CAN do still transpiles',
     (await page.getAttribute('#status', 'class')) === 'ok'
     && (await page.textContent('#out')).includes('analogRead(A6)'));

  // Canonical pseudocode view.
  await page.selectOption('#example', names[0]);
  await page.waitForTimeout(400);
  await page.selectOption('#view', 'canon');
  await page.waitForTimeout(200);
  ok('canonical pseudocode round-trips in-page',
     (await page.textContent('#out')).includes('NAME blink'));

  // --- flash.js, in the runtime it actually ships to --------------------
  //
  // scripts/test-flash.mjs exercises these protocols in Node. They run in a
  // browser. Those are different runtimes and the module has only ever been
  // IMPORTED here, never executed -- so the pure protocol functions are run
  // in the page and compared against Node's answers for the same inputs.
  // Same module, two engines, one expected result.
  const { pathToFileURL } = require('url');
  const path = require('path');
  const flash = await import(
    pathToFileURL(path.join(__dirname, '..', 'docs', 'flash.js')).href);

  // Computed, not typed. Two drafts of this file carried a hand-written
  // checksum that was wrong -- good for the parser, wasteful for me.
  const record = (addr, data) => {
    const body = [data.length, (addr >> 8) & 0xff, addr & 0xff, 0, ...data];
    const sum = (0x100 - (body.reduce((a, b) => a + b, 0) & 0xff)) & 0xff;
    return ':' + [...body, sum]
      .map(b => b.toString(16).padStart(2, '0').toUpperCase()).join('');
  };
  const HEX = [record(0x0000, [0x0C, 0x94, 0x34, 0x00, 0x0C, 0x94, 0x3E, 0x00,
                               0x0C, 0x94, 0x3E, 0x00, 0x0C, 0x94, 0x3E, 0x00]),
               record(0x0020, [0x12, 0x34, 0x56, 0x78]),
               ':00000001FF'].join('\n');
  const TRICKY = "a'b\\c\nd\x00\x7f";

  const node = {
    image: Array.from(flash.parseIntelHex(HEX).image).join(','),
    packet: Buffer.from(flash.stcPacket([0x50, 0x00, 0x00, 0x36, 0x01, 0xd1, 0x7e]))
                  .toString('hex'),
    baud: JSON.stringify(flash.stcBaud(11059200, 115200)),
    bytes: flash.pythonBytes(new TextEncoder().encode(TRICKY)),
  };

  const inBrowser = await page.evaluate(async ({ hex, tricky, base }) => {
    const m = await import(base + '/flash.js');
    const hexb = (a) => Array.from(a).map(b => b.toString(16).padStart(2, '0')).join('');
    return {
      image: Array.from(m.parseIntelHex(hex).image).join(','),
      packet: hexb(m.stcPacket([0x50, 0x00, 0x00, 0x36, 0x01, 0xd1, 0x7e])),
      baud: JSON.stringify(m.stcBaud(11059200, 115200)),
      bytes: m.pythonBytes(new TextEncoder().encode(tricky)),
    };
  }, { hex: HEX, tricky: TRICKY, base });

  for (const key of Object.keys(node)) {
    ok(`flash.js agrees between Node and the browser: ${key}`,
       node[key] === inBrowser[key],
       node[key] === inBrowser[key] ? ''
         : `node ${JSON.stringify(node[key]).slice(0, 40)} vs browser `
           + `${JSON.stringify(inBrowser[key]).slice(0, 40)}`);
  }

  // A malformed image must throw in the browser too, not silently return junk.
  const threw = await page.evaluate(async ({ base, bad }) => {
    const m = await import(base + '/flash.js');
    try { m.parseIntelHex(bad); return null; }
    catch (e) { return e.message; }
  }, { base, bad: HEX.split('\n')[0].slice(0, -2) + 'FF' });
  ok('a bad checksum still throws in the browser', /checksum/.test(threw || ''),
     String(threw));

  ok('no uncaught page errors', errors.length === 0, errors.slice(0, 2).join(' | '));

  await browser.close();
  console.log(fails ? `\n  ${fails} failed` : '\n  all good');
  process.exit(fails ? 1 : 0);
})();
