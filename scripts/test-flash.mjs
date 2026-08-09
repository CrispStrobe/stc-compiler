// test-flash.mjs — drive the flasher against a simulated bootloader.
//
// Flashing is the one step here whose output cannot be checked by compiling
// it. So the other end of the wire is simulated: a bootloader that speaks
// STK500v1, remembers what it was told to write, and is then asked whether
// its flash matches the image that went in.
//
// That catches the mistakes that actually happen in this protocol -- byte
// addresses where word addresses belong, a page boundary off by one, a
// checksum read as data -- none of which a human staring at the code reliably
// sees, and all of which look identical from outside: a board that does
// nothing.
//
//   node scripts/test-flash.mjs
//
import { parseIntelHex, Stk500, flashAvr, STK } from '../docs/flash.js';

let passed = 0, failed = 0;
const ok = (name, cond, detail = '') => {
  if (cond) { passed++; console.log(`  \x1b[32mok \x1b[0m ${name} ${detail}`); }
  else { failed++; console.log(`  \x1b[31mFAIL\x1b[0m ${name} ${detail}`); }
  return !!cond;
};

/** An optiboot that remembers what it was told. */
class FakeBootloader {
  constructor({ pageSize = 128, flashSize = 32768, quirk = null } = {}) {
    this.flash = new Uint8Array(flashSize).fill(0xff);
    this.pageSize = pageSize;
    this.address = 0;            // WORD address, as STK500 defines it
    this.inbox = [];
    this.out = [];
    this.quirk = quirk;
    this.syncs = 0;
    this.pagesWritten = 0;
  }

  feed(bytes) {
    for (const b of bytes) this.inbox.push(b);
    for (;;) {
      const consumed = this.step();
      if (!consumed) break;
    }
  }

  reply(...bytes) { this.out.push(...bytes); }

  step() {
    if (!this.inbox.length) return false;
    const command = this.inbox[0];
    const need = (n) => this.inbox.length >= n;

    if (command === STK.GET_SYNC) {
      if (!need(2)) return false;
      this.inbox.splice(0, 2);
      this.syncs++;
      // A real board often emits noise before it is listening.
      if (this.quirk === 'noisy' && this.syncs === 1) { this.reply(0x00); return true; }
      this.reply(STK.INSYNC, STK.OK);
      return true;
    }
    if (command === STK.ENTER_PROGMODE || command === STK.LEAVE_PROGMODE) {
      if (!need(2)) return false;
      this.inbox.splice(0, 2);
      this.reply(STK.INSYNC, STK.OK);
      return true;
    }
    if (command === STK.READ_SIGN) {
      if (!need(2)) return false;
      this.inbox.splice(0, 2);
      this.reply(STK.INSYNC, 0x1e, 0x95, 0x0f, STK.OK);   // ATmega328P
      return true;
    }
    if (command === STK.LOAD_ADDRESS) {
      if (!need(4)) return false;
      const [, lo, hi] = this.inbox.splice(0, 4);
      this.address = (hi << 8) | lo;
      this.reply(STK.INSYNC, STK.OK);
      return true;
    }
    if (command === STK.PROG_PAGE) {
      if (!need(5)) return false;
      const length = (this.inbox[1] << 8) | this.inbox[2];
      if (!need(5 + length)) return false;
      const frame = this.inbox.splice(0, 5 + length);
      const data = frame.slice(4, 4 + length);
      const byteAddress = this.address * 2;
      for (let i = 0; i < data.length; i++) this.flash[byteAddress + i] = data[i];
      this.pagesWritten++;
      this.reply(STK.INSYNC, STK.OK);
      return true;
    }
    if (command === STK.READ_PAGE) {
      if (!need(5)) return false;
      const frame = this.inbox.splice(0, 5);
      const length = (frame[1] << 8) | frame[2];
      const byteAddress = this.address * 2;
      this.reply(STK.INSYNC,
                 ...this.flash.subarray(byteAddress, byteAddress + length),
                 STK.OK);
      return true;
    }
    // Unknown byte: drop it, the way a bootloader ignores line noise.
    this.inbox.shift();
    return true;
  }
}

/** A Web Serial-shaped port wrapping the fake bootloader. */
function fakePort(boot) {
  let opened = false;
  const signals = [];
  return {
    signals,
    async open() { opened = true; },
    async close() { opened = false; },
    async setSignals(s) { signals.push(s); },
    get writable() {
      return { getWriter: () => ({
        async write(bytes) { boot.feed(bytes); },
        releaseLock() {},
      }) };
    },
    get readable() {
      return { getReader: () => ({
        async read() {
          for (let i = 0; i < 400 && !boot.out.length; i++) {
            await new Promise(r => setTimeout(r, 2));
          }
          if (!boot.out.length) return { done: true };
          const value = Uint8Array.from(boot.out.splice(0, boot.out.length));
          return { value, done: false };
        },
        async cancel() {},
        releaseLock() {},
      }) };
    },
    get isOpen() { return opened; },
  };
}

console.log('flashing, against a simulated bootloader\n');

// --- Intel HEX ------------------------------------------------------------
// Built rather than pasted: a hand-typed checksum is a hand-typed bug, and
// the first draft of this file had one (which the parser duly rejected).
function record(address, type, data) {
  const body = [data.length, (address >> 8) & 0xff, address & 0xff, type, ...data];
  const sum = (0x100 - (body.reduce((a, b) => a + b, 0) & 0xff)) & 0xff;
  return ':' + [...body, sum].map(b => b.toString(16).padStart(2, '0')
                                        .toUpperCase()).join('');
}
const SAMPLE = [
  record(0x0000, 0, [0x0C, 0x94, 0x34, 0x00, 0x0C, 0x94, 0x3E, 0x00,
                     0x0C, 0x94, 0x3E, 0x00, 0x0C, 0x94, 0x3E, 0x00]),
  record(0x0010, 0, [0x0C, 0x94, 0x3E, 0x00, 0x0C, 0x94, 0x3E, 0x00,
                     0x0C, 0x94, 0x3E, 0x00, 0x0C, 0x94, 0x3E, 0x00]),
  record(0x0020, 0, [0x12, 0x34, 0x56, 0x78]),
  ':00000001FF',
].join('\n');

const parsed = parseIntelHex(SAMPLE);
ok('parses records and pads gaps with 0xFF', parsed.image.length === 0x24
   && parsed.image[0] === 0x0c && parsed.image[0x20] === 0x12);

let threw = null;
try { parseIntelHex(SAMPLE.split('\n')[0].slice(0, -2) + 'FF'); }
catch (e) { threw = e; }
ok('a bad checksum is refused', threw && /checksum/.test(threw.message),
   threw ? threw.message : 'accepted!');

threw = null;
try { parseIntelHex('nonsense'); } catch (e) { threw = e; }
ok('a non-record is refused', threw && /start code/.test(threw.message));

// --- a real image, end to end --------------------------------------------
import { readFileSync, existsSync } from 'node:fs';
// A real avr-gcc image when one has been built locally; otherwise a
// synthetic one big enough to cross several page boundaries, because a
// 36-byte image would never exercise the addressing that actually goes wrong.
const REAL = 'build-avr/parity.hex';
function syntheticHex(length) {
  const lines = [];
  for (let at = 0; at < length; at += 16) {
    const chunk = [];
    for (let i = 0; i < Math.min(16, length - at); i++) chunk.push((at + i * 7) & 0xff);
    lines.push(record(at, 0, chunk));
  }
  lines.push(':00000001FF');
  return lines.join('\n');
}
const hexText = existsSync(REAL) ? readFileSync(REAL, 'utf8') : syntheticHex(800);
const expected = parseIntelHex(hexText).image;

const boot = new FakeBootloader();
const port = fakePort(boot);
const result = await flashAvr(port, hexText, { log: () => {} });

ok('reports the image size', result.bytes === expected.length,
   `${result.bytes} vs ${expected.length}`);
ok('pulsed DTR to reset the board',
   port.signals.length >= 2 && port.signals[0].dataTerminalReady === false
   && port.signals[1].dataTerminalReady === true);
ok('wrote every page', boot.pagesWritten === Math.ceil(expected.length / 128),
   `${boot.pagesWritten} pages for ${expected.length} bytes`);

let mismatch = -1;
for (let i = 0; i < expected.length; i++) {
  if (boot.flash[i] !== expected[i]) { mismatch = i; break; }
}
ok('the bootloader\'s flash matches the image, byte for byte', mismatch === -1,
   mismatch === -1 ? `${expected.length} bytes`
                   : `first difference at 0x${mismatch.toString(16)}`);

// --- word vs byte addressing, the classic way to brick a flash ------------
// If LOAD_ADDRESS were sent as a BYTE address, everything past the first page
// lands at twice its offset. Assert the addresses we sent were word addresses
// by checking the image is contiguous from 0 with nothing written above it.
const tail = boot.flash.subarray(expected.length);
ok('nothing written past the end of the image (word addressing)',
   tail.every(b => b === 0xff),
   tail.every(b => b === 0xff) ? ''
     : 'something landed above the image -- byte addresses sent as word ones?');

// --- a board that answers late -------------------------------------------
const noisy = new FakeBootloader({ quirk: 'noisy' });
const noisyResult = await flashAvr(fakePort(noisy), hexText, { log: () => {} });
ok('retries through a board that answers with noise first',
   noisyResult.bytes === expected.length, `${noisy.syncs} sync attempts`);

// --- verification has to be able to fail ---------------------------------
const corrupt = new FakeBootloader();
const corruptPort = fakePort(corrupt);
const realFeed = corrupt.feed.bind(corrupt);
let flipped = false;
corrupt.feed = (bytes) => {
  realFeed(bytes);
  if (!flipped && corrupt.pagesWritten > 0) { corrupt.flash[4] ^= 0xff; flipped = true; }
};
let verifyError = null;
try { await flashAvr(corruptPort, hexText, { log: () => {} }); }
catch (e) { verifyError = e; }
ok('verify catches a board that did not store what it was sent',
   verifyError && /verify failed/.test(verifyError.message),
   verifyError ? verifyError.message.slice(0, 58) : 'no error raised!');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
