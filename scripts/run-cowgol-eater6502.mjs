// Test-only execution of service ROMs on Brickwright's actual ACIA/VIA machine.
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const board = process.env.COWGOL_BW_BOARD;
if (!board) throw new Error('COWGOL_BW_BOARD must name the pinned bw-board checkout');
const {M6502Machine, EATER6502} = await import(pathToFileURL(resolve(board, 'src/m6502-machine.js')).href);
const rom = new Uint8Array(readFileSync(process.argv[2]));
const options = JSON.parse(process.argv[3] || '{}');
const output = [], romWrites = [];
const machine = new M6502Machine(EATER6502, {onSerial: byte => output.push(byte)});
machine.mem.fill(0xa5, 0, 0x4000);
machine.loadRom(rom);
const write = machine.cpu.write;
machine.cpu.write = (address, value) => {
    if (address >= 0x8000) romWrites.push({address, value});
    write(address, value);
};
machine.chips.via1.inA = options.inA ?? 0;
machine.chips.via1.inB = options.inB ?? 0;
const runs = [];
for (let run = 0; run < (options.runs || 1); run++) {
    const before = output.length;
    machine.reset();
    let fed = false;
    for (let steps = 0; steps < 4_000_000 && !machine.cpu.stopped; steps++) {
        machine.step();
        if (!fed && options.input && output.length > before) {
            for (const byte of Buffer.from(options.input)) machine.chips.acia1.rxPush(byte);
            fed = true;
        }
    }
    runs.push({stopped: machine.cpu.stopped, status: machine.cpu.a,
        text: Buffer.from(output.slice(before)).toString('utf8')});
}
const via = machine.chips.via1;
process.stdout.write(JSON.stringify({runs, romWrites,
    via: {ora: via.ora, orb: via.orb, ddra: via.ddra, ddrb: via.ddrb},
    romUnchanged: Buffer.from(machine.mem.subarray(0x8000)).equals(Buffer.from(rom))}));
