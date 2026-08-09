// flash.js — Intel HEX to an AVR over Web Serial, speaking STK500v1.
//
// This is the last gap between the page and a board doing something. An
// ATmega with the Arduino bootloader (optiboot) is entered by pulsing DTR,
// which resets the chip; the bootloader then answers STK500v1 for a second or
// so before handing over to the application.
//
// The transport is a parameter rather than `navigator.serial` directly, so
// the whole protocol can be driven against a simulated bootloader in a test.
// Flashing is one of the few things here that cannot be checked by compiling
// the output, and "it looked right" is not good enough for something that
// writes to a chip.
//
// Deliberately NOT here: the STC12's ISP. That protocol only answers after a
// COLD power-on -- a reset pulse will not do -- so it needs a different
// interaction from the user, not just different bytes. See flashStc below.

export const STK = {
  OK: 0x10, INSYNC: 0x14, CRC_EOP: 0x20,
  GET_SYNC: 0x30, ENTER_PROGMODE: 0x50, LEAVE_PROGMODE: 0x51,
  LOAD_ADDRESS: 0x55, PROG_PAGE: 0x64, READ_PAGE: 0x74,
  READ_SIGN: 0x75,
};

/** Parse Intel HEX into a flat image. Throws on a bad checksum. */
export function parseIntelHex(text) {
  const bytes = new Map();
  let base = 0, highest = -1, lowest = Infinity;
  let line = 0;
  for (const raw of text.split(/\r?\n/)) {
    line++;
    const record = raw.trim();
    if (!record) continue;
    if (record[0] !== ':') throw new Error(`line ${line}: no start code`);
    const octets = [];
    for (let i = 1; i + 1 < record.length; i += 2) {
      octets.push(parseInt(record.substr(i, 2), 16));
    }
    if (octets.some(Number.isNaN)) throw new Error(`line ${line}: not hex`);
    const sum = octets.reduce((a, b) => (a + b) & 0xff, 0);
    if (sum !== 0) throw new Error(`line ${line}: checksum mismatch`);
    const count = octets[0];
    const address = (octets[1] << 8) | octets[2];
    const type = octets[3];
    if (type === 0x00) {
      for (let i = 0; i < count; i++) {
        const at = base + address + i;
        bytes.set(at, octets[4 + i]);
        if (at > highest) highest = at;
        if (at < lowest) lowest = at;
      }
    } else if (type === 0x01) {
      break;                                    // end of file
    } else if (type === 0x02) {
      base = ((octets[4] << 8) | octets[5]) << 4;
    } else if (type === 0x04) {
      base = ((octets[4] << 8) | octets[5]) << 16;
    }
    // 0x03 and 0x05 are start-address records: meaningless for a bootloader
    // that always begins at reset, and skipping them is correct rather than
    // lazy.
  }
  if (highest < 0) throw new Error('no data records');
  // Bootloaders write from 0; a gap inside the image is padded with 0xFF,
  // which is what erased flash reads as.
  const image = new Uint8Array(highest + 1).fill(0xff);
  for (const [at, value] of bytes) image[at] = value;
  return { image, lowest, highest };
}

/** A framed STK500v1 conversation over a byte transport. */
export class Stk500 {
  constructor(transport, { pageSize = 128, log = () => {} } = {}) {
    this.io = transport;
    this.pageSize = pageSize;
    this.log = log;
  }

  async command(body, expected = 0) {
    await this.io.write(Uint8Array.from([...body, STK.CRC_EOP]));
    const head = await this.io.read(1);
    if (head[0] !== STK.INSYNC) {
      throw new Error(`out of sync (got 0x${head[0].toString(16)})`);
    }
    const payload = expected ? await this.io.read(expected) : new Uint8Array(0);
    const tail = await this.io.read(1);
    if (tail[0] !== STK.OK) {
      throw new Error(`command not acknowledged (0x${tail[0].toString(16)})`);
    }
    return payload;
  }

  /** optiboot answers within a couple of attempts; the first can be noise. */
  async sync(attempts = 5) {
    let last;
    for (let i = 0; i < attempts; i++) {
      try {
        await this.command([STK.GET_SYNC]);
        return true;
      } catch (err) {
        last = err;
        await this.io.drain?.();
      }
    }
    throw new Error(`no bootloader answered: ${last && last.message}`);
  }

  async signature() {
    return this.command([STK.READ_SIGN], 3);
  }

  async program(image) {
    await this.command([STK.ENTER_PROGMODE]);
    for (let offset = 0; offset < image.length; offset += this.pageSize) {
      const page = image.subarray(offset, Math.min(offset + this.pageSize,
                                                   image.length));
      // STK500 addresses flash in WORDS, not bytes. Getting this wrong writes
      // the image to twice its address and the board does nothing at all.
      const word = offset >> 1;
      await this.command([STK.LOAD_ADDRESS, word & 0xff, (word >> 8) & 0xff]);
      await this.command([STK.PROG_PAGE, (page.length >> 8) & 0xff,
                          page.length & 0xff, 0x46, ...page]);
      this.log(`  wrote ${page.length} bytes at 0x${offset.toString(16).padStart(4, '0')}`);
    }
    await this.command([STK.LEAVE_PROGMODE]);
  }

  async verify(image) {
    await this.command([STK.ENTER_PROGMODE]);
    for (let offset = 0; offset < image.length; offset += this.pageSize) {
      const length = Math.min(this.pageSize, image.length - offset);
      const word = offset >> 1;
      await this.command([STK.LOAD_ADDRESS, word & 0xff, (word >> 8) & 0xff]);
      const back = await this.command(
        [STK.READ_PAGE, (length >> 8) & 0xff, length & 0xff, 0x46], length);
      for (let i = 0; i < length; i++) {
        if (back[i] !== image[offset + i]) {
          await this.command([STK.LEAVE_PROGMODE]);
          throw new Error(
            `verify failed at 0x${(offset + i).toString(16)}: ` +
            `wrote 0x${image[offset + i].toString(16)}, read 0x${back[i].toString(16)}`);
        }
      }
    }
    await this.command([STK.LEAVE_PROGMODE]);
    return true;
  }
}

/** Web Serial as a byte transport, with a read buffer and a deadline. */
export function serialTransport(port, { timeout = 3000 } = {}) {
  const writer = port.writable.getWriter();
  const reader = port.readable.getReader();
  let buffer = new Uint8Array(0);
  let closed = false;

  const pump = (async () => {
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        if (value && value.length) {
          const merged = new Uint8Array(buffer.length + value.length);
          merged.set(buffer); merged.set(value, buffer.length);
          buffer = merged;
        }
      }
    } catch { /* the port went away; read() below will time out and say so */ }
    closed = true;
  })();

  return {
    async write(bytes) { await writer.write(bytes); },
    async read(count) {
      const deadline = Date.now() + timeout;
      while (buffer.length < count) {
        if (Date.now() > deadline) {
          throw new Error(`timed out waiting for ${count} byte(s) from the board`);
        }
        if (closed && buffer.length < count) throw new Error('port closed');
        await new Promise(r => setTimeout(r, 5));
      }
      const out = buffer.subarray(0, count);
      buffer = buffer.subarray(count);
      return out;
    },
    async drain() { buffer = new Uint8Array(0); },
    async close() {
      try { reader.cancel(); } catch {}
      try { writer.releaseLock(); } catch {}
      await pump.catch(() => {});
    },
  };
}

/** Pulse DTR/RTS to reset the board into its bootloader. */
export async function pulseReset(port) {
  await port.setSignals({ dataTerminalReady: false, requestToSend: false });
  await new Promise(r => setTimeout(r, 250));
  await port.setSignals({ dataTerminalReady: true, requestToSend: true });
  await new Promise(r => setTimeout(r, 50));
}

/**
 * The whole flow: reset, sync, program, verify.
 * `open` lets a test supply a port without touching navigator.serial.
 */
export async function flashAvr(port, hexText, {
  baud = 115200, pageSize = 128, log = () => {}, verify = true,
} = {}) {
  const { image, highest } = parseIntelHex(hexText);
  log(`image: ${highest + 1} bytes`);
  await port.open({ baudRate: baud });
  const io = serialTransport(port);
  try {
    if (port.setSignals) {
      log('resetting the board (DTR)…');
      await pulseReset(port);
      await io.drain();
    }
    log('waiting for the bootloader…');
    await new Stk500(io, { pageSize, log }).sync();
    const stk = new Stk500(io, { pageSize, log });
    log('programming…');
    await stk.program(image);
    if (verify) {
      log('verifying…');
      await stk.verify(image);
    }
    log(`done: ${highest + 1} bytes written${verify ? ' and verified' : ''}`);
    return { bytes: highest + 1 };
  } finally {
    await io.close();
    try { await port.close(); } catch {}
  }
}

/**
 * The STC path, which is NOT this one and is why it is a stub rather than a
 * guess. The STC12/STC15 ISP bootloader answers only after a COLD power-on --
 * a reset pulse is not enough, which is documented in the lab's README and is
 * the single fact most STC tutorials get wrong. So the interaction is: open
 * the port, begin sending the handshake, ask the user to remove and reapply
 * power, and catch the greeting when it arrives. That is a different UI, not
 * just different bytes, and it is the reason this is not folded into
 * flashAvr.
 */
export async function flashStc() {
  throw new Error(
    'STC ISP flashing is not implemented in the browser yet: the bootloader ' +
    'answers only after a cold power-on, so it needs its own prompt-and-wait ' +
    'flow. Use `stcgal -P stc12 -p /dev/cu.usbserial-XXXX main.hex` for now.');
}

// ---------------------------------------------------------------- micro:bit
//
// A micro:bit has no serial bootloader to talk STK500 to. What it has, once
// MicroPython is on it, is a REPL over the DAPLink CDC port -- and MicroPython's
// "raw REPL" is a perfectly good file-transfer channel. That is what microfs
// and the official editors use, and it is far less machinery than splicing a
// script into a 1.8 MB runtime hex and asking the user to drag it to a drive.
//
// The trade is explicit: this writes main.py to a board that ALREADY has
// MicroPython. It does not install MicroPython. Flash that once from
// python.microbit.org and this works from then on.

const CTRL_A = 0x01, CTRL_B = 0x02, CTRL_C = 0x03, CTRL_D = 0x04;

/** A Python bytes literal that survives the REPL: printable ASCII, escaped. */
export function pythonBytes(chunk) {
  let out = "b'";
  for (const byte of chunk) {
    if (byte === 0x5c) out += '\\\\';
    else if (byte === 0x27) out += "\\'";
    else if (byte === 0x0a) out += '\\n';
    else if (byte === 0x0d) out += '\\r';
    else if (byte === 0x09) out += '\\t';
    else if (byte >= 0x20 && byte < 0x7f) out += String.fromCharCode(byte);
    else out += '\\x' + byte.toString(16).padStart(2, '0');
  }
  return out + "'";
}

/** Read until `marker` appears, or time out saying what was seen instead. */
async function readUntil(io, marker, { timeout = 5000 } = {}) {
  const deadline = Date.now() + timeout;
  let seen = '';
  const decoder = new TextDecoder();
  while (!seen.includes(marker)) {
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${JSON.stringify(marker)}; ` +
                      `saw ${JSON.stringify(seen.slice(-60))}`);
    }
    try {
      seen += decoder.decode(await io.read(1));
    } catch {
      throw new Error(`the board stopped responding; saw ` +
                      `${JSON.stringify(seen.slice(-60))}`);
    }
  }
  return seen;
}

export class RawRepl {
  constructor(io) { this.io = io; this.encoder = new TextEncoder(); }

  async send(text) { await this.io.write(this.encoder.encode(text)); }
  async control(byte) { await this.io.write(Uint8Array.from([byte])); }

  async enter() {
    // Two interrupts: the first stops a running program, the second lands in
    // the REPL even if the first arrived while it was already idle.
    await this.control(CTRL_C);
    await this.control(CTRL_C);
    await this.io.drain?.();
    await this.control(CTRL_A);
    await readUntil(this.io, 'raw REPL');
    await readUntil(this.io, '>');
  }

  /** Run one block and return its stdout. Raises on a Python traceback. */
  async exec(code) {
    await this.send(code);
    await this.control(CTRL_D);
    await readUntil(this.io, 'OK');
    const body = await readUntil(this.io, '\x04');
    const error = await readUntil(this.io, '\x04');
    const failure = error.replace(/\x04/g, '').trim();
    if (failure) throw new Error(failure.split('\n').pop());
    await readUntil(this.io, '>');
    return body.replace(/\x04/g, '');
  }

  async exit() { await this.control(CTRL_B); }
}

/**
 * Write `source` to main.py on an attached micro:bit and restart it.
 * `chunk` stays small because each write becomes one REPL line.
 */
export async function flashMicrobit(port, source, {
  baud = 115200, chunk = 96, log = () => {}, name = 'main.py',
} = {}) {
  const bytes = new TextEncoder().encode(source);
  await port.open({ baudRate: baud });
  const io = serialTransport(port, { timeout: 6000 });
  try {
    log('interrupting whatever is running…');
    const repl = new RawRepl(io);
    await repl.enter();

    log(`writing ${name} (${bytes.length} bytes)…`);
    await repl.exec(`fd = open(${JSON.stringify(name)}, 'wb')\nf = fd.write`);
    for (let at = 0; at < bytes.length; at += chunk) {
      await repl.exec(`f(${pythonBytes(bytes.subarray(at, at + chunk))})`);
    }
    await repl.exec('fd.close()');

    // Read it back. A REPL that swallowed a chunk looks exactly like one that
    // did not, right up until the board runs the wrong program.
    log('verifying…');
    const size = (await repl.exec(
      `import os\nprint(os.size(${JSON.stringify(name)}))`)).trim();
    if (parseInt(size, 10) !== bytes.length) {
      throw new Error(`wrote ${bytes.length} bytes but the board has ${size}`);
    }

    await repl.exit();
    log('restarting…');
    await repl.control(CTRL_D);
    log(`done: ${name}, ${bytes.length} bytes`);
    return { bytes: bytes.length };
  } finally {
    await io.close();
    try { await port.close(); } catch {}
  }
}
