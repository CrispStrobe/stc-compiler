// Execute service output in the exact Brickwright RISC-V machine. Test-only:
// this runner is never shipped as, or called by, a service request handler.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

if (!process.env.COWGOL_BW_BOARD) throw new Error('COWGOL_BW_BOARD must name the pinned bw-board checkout');
const moduleUrl = pathToFileURL(path.resolve(process.env.COWGOL_BW_BOARD, 'src/riscv32-machine.js'));
const {RiscV32Machine} = await import(moduleUrl.href);
const image = JSON.parse(await readFile(process.argv[2], 'utf8'));
const machine = new RiscV32Machine({memSize: 1 << 22, resetPc: image.entry});
for (const segment of image.segments) machine.load(Buffer.from(segment.bytes, 'base64'), segment.addr);
machine.run(50_000_000);
assert.equal(machine.halted, true, 'Cowgol RV32 program must halt within the instruction budget');
assert.equal(machine.exitCode, 0, 'Cowgol RV32 program must exit successfully');
process.stdout.write(machine.output);
