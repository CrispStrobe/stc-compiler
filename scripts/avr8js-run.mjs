// avr8js-run.mjs — execute an AVR Intel HEX image for a fixed simulated time
// and report what came out of it: every byte USART0 transmitted, and how many
// times each PORTB/PORTD pin changed level.
//
//   node scripts/avr8js-run.mjs <image.hex> <mcu> <milliseconds> [clockHz]
//
// Prints one JSON object. Used by scripts/test-arduino-run.py, which builds
// sketches through the service and asserts on this output; avr8js (MIT) is
// installed by that CI step, not vendored.
import {createRequire} from 'module';
import fs from 'fs';

const require = createRequire(import.meta.url);
const avr = require('avr8js');

const [hexPath, mcu, msArg, clockArg] = process.argv.slice(2);
const ms = Number(msArg);
const clockHz = Number(clockArg || 16e6);
// Flash in 16-bit words: 32 KB (ATmega328P/168P) or 256 KB (ATmega2560).
const words = mcu === 'atmega2560' ? 0x20000 : 0x4000;
const program = new Uint16Array(words);
const bytes = new Uint8Array(program.buffer);

let base = 0;
for (const line of fs.readFileSync(hexPath, 'utf8').split(/\r?\n/)) {
    if (line[0] !== ':') continue;
    const n = parseInt(line.slice(1, 3), 16);
    const addr = parseInt(line.slice(3, 7), 16);
    const type = parseInt(line.slice(7, 9), 16);
    if (type === 0) {
        for (let i = 0; i < n; i++) bytes[base + addr + i] = parseInt(line.slice(9 + 2 * i, 11 + 2 * i), 16);
    } else if (type === 2) {
        base = parseInt(line.slice(9, 13), 16) << 4;
    } else if (type === 4) {
        base = parseInt(line.slice(9, 13), 16) << 16;
    }
}

// SRAM beyond the 0x100 register/IO space. The ATmega2560's SRAM starts at
// 0x200 (it has 0x100-0x1FF of extended I/O) and ends at 0x21FF, so its stack
// lives 0x100 bytes above where an 8 KB default would stop.
const sramBytes = {atmega328p: 0x800, atmega168p: 0x400, atmega2560: 0x2100}[mcu] || 0x800;
const eepromBytes = {atmega328p: 1024, atmega168p: 512, atmega2560: 4096}[mcu] || 1024;
// avr8js's stock peripheral configs carry the ATmega328P's interrupt vectors
// (it has 26; the 2560 has 57). Registers sit at the same addresses on the
// 2560, the vectors do not -- with the 328P's, the first USART interrupt
// lands on __bad_interrupt and the chip resets after one character.
// Word addresses are vector number x 2 (iom2560.h's *_vect_num).
const mega = mcu === 'atmega2560';
const timer0 = mega ? {...avr.timer0Config, compAInterrupt: 42, compBInterrupt: 44, ovfInterrupt: 46}
    : avr.timer0Config;
const usart0 = mega ? {...avr.usart0Config, rxCompleteInterrupt: 50,
    dataRegisterEmptyInterrupt: 52, txCompleteInterrupt: 54} : avr.usart0Config;
const eeprom = mega ? {...avr.eepromConfig, eepromReadyInterrupt: 60} : avr.eepromConfig;

const cpu = new avr.CPU(program, sramBytes);
new avr.AVRTimer(cpu, timer0);
new avr.AVREEPROM(cpu, new avr.EEPROMMemoryBackend(eepromBytes), eeprom);
const usart = new avr.AVRUSART(cpu, usart0, clockHz);
let serial = '';
usart.onByteTransmit = (b) => { serial += String.fromCharCode(b); };

const toggles = {};
for (const [name, config] of [['B', avr.portBConfig], ['D', avr.portDConfig]]) {
    const port = new avr.AVRIOPort(cpu, config);
    let last = null;
    port.addListener((value) => {
        if (last !== null) {
            for (let bit = 0; bit < 8; bit++) {
                if (((value ^ last) >> bit) & 1) {
                    const key = `P${name}${bit}`;
                    toggles[key] = (toggles[key] || 0) + 1;
                }
            }
        }
        last = value;
    });
}

const end = clockHz / 1000 * ms;
while (cpu.cycles < end) {
    avr.avrInstruction(cpu);
    cpu.tick();
}
process.stdout.write(JSON.stringify({serial, toggles, cycles: cpu.cycles}) + '\n');
