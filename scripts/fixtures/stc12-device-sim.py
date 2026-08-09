"""A simulated STC12C5A60S2 ISP bootloader, on a pty, that logs the session.

Built to be driven by REAL stcgal, so the transcript it records is ground
truth rather than my reading of the protocol.
"""
import os, pty, struct, sys, time, select, json

START, MCU, HOST, END = b"\x46\xb9", 0x68, 0x6a, 0x16
MAGIC = 0xD17E                     # STC12C5A60S2
HANDSHAKE_BAUD = 2400
CLOCK = 11059200


def mcu_packet(payload):
    body = bytes([MCU]) + struct.pack(">H", len(payload) + 6) + payload
    return START + body + struct.pack(">H", sum(body) & 0xFFFF) + bytes([END])


def status_payload():
    counter = round(CLOCK * 7.0 / (HANDSHAKE_BAUD * 12.0))     # ~2688
    out = bytearray([0x50])
    for _ in range(8):
        out += struct.pack(">H", counter)
    out += bytes([0x72, 0x03])          # bootloader version 7.2, stepping
    out += bytes([0x00])
    out += struct.pack(">H", MAGIC)
    out += bytes([0x00] * 16)           # option bytes; stcgal reads several
    return bytes(out)


master, slave = pty.openpty()
open("/tmp/stc-pty-name", "w").write(os.ttyname(slave))
print(f"pty: {os.ttyname(slave)}", flush=True)

log = []
flash = bytearray(b"\xff" * 65536)
blocks = []
buffer = bytearray()
synced = False
deadline = time.time() + 25

def send(data, why):
    log.append({"dir": "mcu", "why": why, "hex": data.hex()})
    os.write(master, data)

while time.time() < deadline:
    r, _, _ = select.select([master], [], [], 0.2)
    if not r:
        continue
    try:
        chunk = os.read(master, 4096)
    except OSError:
        break
    if not chunk:
        break
    buffer += chunk

    if not synced:
        if buffer.count(0x7F) >= 4:
            buffer.clear()
            synced = True
            time.sleep(0.05)
            send(mcu_packet(status_payload()), "status")
        continue

    # Host packets: 46 B9 6A <len:2> ... <csum:2> 16
    while True:
        at = buffer.find(START)
        if at < 0 or len(buffer) < at + 5:
            break
        length = struct.unpack(">H", bytes(buffer[at + 3:at + 5]))[0]
        total = length + 2
        if len(buffer) < at + total:
            break
        packet = bytes(buffer[at:at + total])
        del buffer[:at + total]
        data = packet[5:-3]
        log.append({"dir": "host", "hex": packet.hex(), "cmd": data[0] if data else None})
        command = data[0] if data else None
        # Stc12BaseProtocol's command/response table, read off its source.
        # The reply's FIRST byte is what stcgal checks; it is not an echo.
        if command == 0x50:                       # begin baud handshake
            send(mcu_packet(bytes([0x8F, 0x00])), "handshake begin")
        elif command == 0x8F:                     # test the new settings
            send(mcu_packet(bytes([0x8F, 0x00])), "settings ok")
        elif command == 0x8E:                     # adopt them
            send(mcu_packet(bytes([0x84, 0x00])), "settings set")
        elif command == 0x84:                     # erase
            # >= 8 bytes makes stcgal record a UID, which is worth exercising.
            send(mcu_packet(bytes([0x00]) + bytes(range(0x11, 0x18))), "erased")
        elif command == 0x00:                     # program one block
            addr = struct.unpack(">H", data[3:5])[0]
            size = struct.unpack(">H", data[5:7])[0]
            flash[addr:addr + size] = data[7:7 + size]
            blocks.append((addr, size))
            send(mcu_packet(bytes([0x00, 0x00])), f"wrote {size}@{addr:04X}")
        elif command == 0x69:                     # finish write
            send(mcu_packet(bytes([0x8D, 0x00])), "write finished")
        elif command == 0x8D:                     # program options
            send(mcu_packet(bytes([0x50, 0x00])), "options set")
        elif command == 0x82:                     # reset and go
            log.append({"dir": "note", "why": "disconnect"})
        else:
            send(mcu_packet(bytes([command, 0x00])), f"ack {command:02X}")

written = max((a + n for a, n in blocks), default=0)
open("flash.bin", "wb").write(bytes(flash[:written]))
open("session.json", "w").write(json.dumps(log, indent=1))
print(f"flash written: {written} bytes in {len(blocks)} blocks")
print(f"\nlogged {len(log)} frames; host packets: "
      f"{sum(1 for e in log if e['dir'] == 'host')}")
for entry in log:
    if entry["dir"] == "host":
        print(f"  host cmd {entry['cmd']:02X}  {entry['hex'][:56]}")
