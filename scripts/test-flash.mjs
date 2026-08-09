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
import { parseIntelHex, Stk500, flashAvr, flashMicroPython, flashStc, stcPacket,
         stcBaud, stcStatus, pythonBytes, STK } from '../docs/flash.js';

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
    // A board listening at another rate sees line noise, not commands.
    if (this.deaf) return;
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

/** A Web Serial-shaped port wrapping the fake bootloader.
 *  `speaksAt` models a board that only answers at one rate, which is the
 *  whole difference between an Uno and an older Nano. */
function fakePort(boot, speaksAt = null) {
  let opened = false;
  const signals = [];
  const opens = [];
  return {
    signals,
    opens,
    async open(options) {
      opened = true;
      opens.push(options && options.baudRate);
      boot.deaf = speaksAt !== null && options && options.baudRate !== speaksAt;
    },
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
// Committed, not built: the stcgal transcript was captured against THIS
// image, so the differential check is only meaningful with the same bytes.
const REAL = 'scripts/fixtures/parity.hex';
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

// --- an older Nano: 57600, and silence at 115200 --------------------------
const nano = new FakeBootloader();
const nanoPort = fakePort(nano, 57600);
const nanoResult = await flashAvr(nanoPort, hexText, { log: () => {} });
ok('falls back to 57600 when nothing answers at 115200',
   nanoResult.baud === 57600, `answered at ${nanoResult.baud}`);
ok('...having actually tried 115200 first',
   nanoPort.opens[0] === 115200 && nanoPort.opens[1] === 57600,
   JSON.stringify(nanoPort.opens));
let nanoMismatch = -1;
for (let i = 0; i < expected.length; i++) {
  if (nano.flash[i] !== expected[i]) { nanoMismatch = i; break; }
}
ok('...and programmed it correctly at that rate', nanoMismatch === -1,
   nanoMismatch === -1 ? `${expected.length} bytes` : `differs at 0x${nanoMismatch.toString(16)}`);

// A board that answers nowhere must say so, naming both rates it tried.
let deafError = null;
try {
  await flashAvr(fakePort(new FakeBootloader(), 9600), hexText, { log: () => {} });
} catch (e) { deafError = e; }
ok('a board that answers at neither rate names both',
   deafError && /115200 or 57600/.test(deafError.message),
   deafError ? deafError.message.slice(0, 58) : 'it succeeded!');

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

// --- micro:bit: MicroPython's raw REPL as a file channel ------------------
console.log('');

/** A MicroPython raw REPL that actually reconstructs the file it is sent. */
class FakeMicroPython {
  constructor({ dropChunk = -1 } = {}) {
    this.out = [];
    this.buffer = '';
    this.raw = false;
    this.files = {};
    this.open = null;
    this.chunks = 0;
    this.dropChunk = dropChunk;
    this.resets = 0;
  }

  say(text) { for (const ch of text) this.out.push(ch.charCodeAt(0)); }

  feed(bytes) {
    for (const byte of bytes) {
      if (byte === 0x03) { this.buffer = ''; this.say('\r\n>>> '); continue; }
      if (byte === 0x01) { this.raw = true; this.buffer = '';
                           this.say('raw REPL; CTRL-B to exit\r\n>'); continue; }
      if (byte === 0x02) { this.raw = false; this.say('\r\n>>> '); continue; }
      if (byte === 0x04) {
        if (!this.raw) { this.resets++; this.say('\r\nMicroPython\r\n>>> '); continue; }
        this.run(this.buffer);
        this.buffer = '';
        continue;
      }
      this.buffer += String.fromCharCode(byte);
    }
  }

  run(code) {
    this.say('OK');
    let stdout = '';
    try {
      const opening = code.match(/^fd = open\("([^"]+)", 'wb'\)/);
      if (opening) { this.open = opening[1]; this.files[this.open] = []; }
      const write = code.match(/^f\((b'.*')\)$/s);
      if (write) {
        this.chunks++;
        if (this.chunks !== this.dropChunk) {
          this.files[this.open].push(...decodeBytes(write[1]));
        }
      }
      const size = code.match(/print\(os\.size\("([^"]+)"\)\)/);
      if (size) stdout = String((this.files[size[1]] || []).length) + '\r\n';
      if (/^fd\.close\(\)$/.test(code)) this.open = null;
    } catch (err) {
      this.say(stdout + '\x04' + 'Error: ' + err.message + '\x04>');
      return;
    }
    this.say(stdout + '\x04' + '\x04>');
  }
}

/** Decode the b'...' literal the flasher builds, so the test is independent. */
function decodeBytes(literal) {
  const body = literal.slice(2, -1);
  const out = [];
  for (let i = 0; i < body.length; i++) {
    if (body[i] !== '\\') { out.push(body.charCodeAt(i)); continue; }
    const next = body[++i];
    if (next === 'n') out.push(10);
    else if (next === 'r') out.push(13);
    else if (next === 't') out.push(9);
    else if (next === 'x') { out.push(parseInt(body.substr(i + 1, 2), 16)); i += 2; }
    else out.push(next.charCodeAt(0));
  }
  return out;
}

function microbitPort(mp) {
  return {
    async open() {}, async close() {},
    get writable() { return { getWriter: () => ({
      async write(b) { mp.feed(b); }, releaseLock() {} }) }; },
    get readable() { return { getReader: () => ({
      async read() {
        for (let i = 0; i < 400 && !mp.out.length; i++) {
          await new Promise(r => setTimeout(r, 2));
        }
        if (!mp.out.length) return { done: true };
        return { value: Uint8Array.from(mp.out.splice(0, mp.out.length)), done: false };
      },
      async cancel() {}, releaseLock() {} }) }; },
  };
}

ok('a bytes literal survives quotes, backslashes and newlines',
   pythonBytes(new TextEncoder().encode("a'b\\c\nd\x00")) === "b'a\\'b\\\\c\\nd\\x00'",
   pythonBytes(new TextEncoder().encode("a'b\\c\nd\x00")));

const SOURCE = 'from microbit import *\n\n' +
  "# quotes ' and a backslash \\ and a tab\there\n" +
  Array.from({ length: 12 }, (_, i) => `display.show(${i})`).join('\n') + '\n';

const mp = new FakeMicroPython();
const written = await flashMicroPython(microbitPort(mp), SOURCE, { log: () => {} });
const landed = new TextDecoder().decode(Uint8Array.from(mp.files['main.py'] || []));
ok('main.py lands byte for byte', landed === SOURCE,
   landed === SOURCE ? `${written.bytes} bytes in ${mp.chunks} chunks`
                     : `got ${JSON.stringify(landed.slice(0, 40))}`);
ok('the board was restarted afterwards', mp.resets === 1, `${mp.resets} reset(s)`);

// A swallowed chunk must not pass, which is the entire reason for reading the
// size back rather than trusting the writes.
const lossy = new FakeMicroPython({ dropChunk: 2 });
let replError = null;
try { await flashMicroPython(microbitPort(lossy), SOURCE, { log: () => {} }); }
catch (e) { replError = e; }
ok('a dropped chunk is caught by the size read-back',
   replError && /but the board has/.test(replError.message),
   replError ? replError.message.slice(0, 56) : 'no error raised!');

// --- STC: differential against a transcript real stcgal produced ---------
console.log('');
const SESSION = JSON.parse(readFileSync('scripts/fixtures/stc12-session.json', 'utf8'));

/** Answers exactly as the simulated STC12 did while stcgal drove it. */
function stcDevice(clockHz, magic, handshakeBaud) {
  const out = [];
  const push = (data) => { for (const b of stcPacket(data)) out.push(b); };
  // The greeting: 8 frequency words, BSL version, then the model magic.
  const counter = Math.round(clockHz * 7 / (handshakeBaud * 12));
  const status = [0x50];
  for (let i = 0; i < 8; i++) status.push((counter >> 8) & 0xff, counter & 0xff);
  status.push(0x72, 0x03, 0x00, (magic >> 8) & 0xff, magic & 0xff);
  for (let i = 0; i < 16; i++) status.push(0);

  let greeted = false;
  return {
    out,
    feed(bytes) {
      if (!greeted) {
        if (bytes.some(b => b === 0x7f)) {
          greeted = true;
          // The MCU packet direction byte is 0x68, not 0x6a.
          const packet = stcPacket(status);
          packet[2] = 0x68;
          const body = [0x68, packet[3], packet[4], ...status];
          const sum = body.reduce((a, b) => a + b, 0) & 0xffff;
          out.push(0x46, 0xb9, ...body, (sum >> 8) & 0xff, sum & 0xff, 0x16);
        }
        return;
      }
      // Host packet: reply per Stc12BaseProtocol's table.
      const data = [...bytes].slice(5, -3);
      const reply = { 0x50: 0x8f, 0x8f: 0x8f, 0x8e: 0x84, 0x84: 0x00,
                      0x00: 0x00, 0x69: 0x8d, 0x82: null }[data[0]];
      if (reply === null || reply === undefined) return;
      const body = data[0] === 0x84
        ? [reply, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17] : [reply, 0x00];
      const packet = stcPacket(body);
      const inner = [0x68, packet[3], packet[4], ...body];
      const sum = inner.reduce((a, b) => a + b, 0) & 0xffff;
      out.push(0x46, 0xb9, ...inner, (sum >> 8) & 0xff, sum & 0xff, 0x16);
    },
  };
}

function stcPort(device) {
  return {
    async open() {}, async close() {},
    get writable() { return { getWriter: () => ({
      async write(b) { device.feed(b); }, releaseLock() {} }) }; },
    get readable() { return { getReader: () => ({
      async read() {
        for (let i = 0; i < 300 && !device.out.length; i++) {
          await new Promise(r => setTimeout(r, 2));
        }
        if (!device.out.length) return { done: true };
        return { value: Uint8Array.from(device.out.splice(0, device.out.length)),
                 done: false };
      },
      async cancel() {}, releaseLock() {} }) }; },
  };
}

ok('the baud maths matches stcgal (11.0592 MHz -> 115200)',
   JSON.stringify(stcBaud(11059200, 115200)) ===
     JSON.stringify({ brt: 250, csum: 12, iap: 0x83, delay: 0x80 }),
   JSON.stringify(stcBaud(11059200, 115200)));

const stcHex = readFileSync('scripts/fixtures/parity.hex', 'utf8');
const device = stcDevice(SESSION.clock_hz, parseInt(SESSION.mcu_magic, 16),
                         SESSION.handshake_baud);
const stcResult = await flashStc(stcPort(device), stcHex, { log: () => {}, sink: true });
const mine = stcResult.sent.map(p => Buffer.from(p).toString('hex'));
// stcgal also rewrites the option bytes (0x8d); this deliberately does not,
// because an option byte is how you disable the ISP pin and lock yourself out.
const theirs = SESSION.host_packets.filter(h => h.slice(10, 12) !== '8d');

ok('sends the same number of packets as stcgal', mine.length === theirs.length,
   `${mine.length} vs ${theirs.length}`);
let differs = -1;
for (let i = 0; i < Math.min(mine.length, theirs.length); i++) {
  if (mine[i] !== theirs[i]) { differs = i; break; }
}
ok('every packet is byte-identical to the reference implementation\'s',
   differs === -1,
   differs === -1 ? `${mine.length} packets`
     : `packet ${differs}:\n        stcgal ${theirs[differs]}\n        ours   ${mine[differs]}`);

// An STC15 must be refused, not spoken to in STC12.
const wrongPart = stcDevice(SESSION.clock_hz, 0xf408, SESSION.handshake_baud);
let ispError = null;
try { await flashStc(stcPort(wrongPart), stcHex, { log: () => {}, sink: true }); }
catch (e) { ispError = e; }
ok('an STC15 is refused by name rather than spoken to in STC12',
   ispError && /stc15 ISP protocol/.test(ispError.message),
   ispError ? ispError.message.slice(0, 62) : 'it proceeded!');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
