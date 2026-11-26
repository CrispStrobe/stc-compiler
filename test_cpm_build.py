"""Exercise the CP/M linker contract with the real SDCC toolchain."""
import base64

from app import CompileReq, build


def compile_cpm(code, **options):
    return build(CompileReq(code=code, target="z80-cpm", format="bin", **options))


def test_cpm_com_has_startup_and_bdos_call():
    result = compile_cpm("""
        volatile unsigned char value = 'A';
        void main(void) {
            __asm
                ld c, #2
                ld e, #65
                call #5
            __endasm;
        }
    """)
    assert result["success"], result
    image = base64.b64decode(result["base64"])
    assert (result["origin"], result["bytes"], result["filename"]) == (0x100, len(image), "main.com")
    assert image[0] == 0x01  # ld bc, l__DATA begins the CP/M startup
    assert b"\xcd\x05\x00" in image  # call BDOS
    assert b"\xc3\x00\x00" in image  # return to CP/M warm boot


def test_cpm_retains_source_diagnostics():
    result = compile_cpm("void main(void) { syntax error; }")
    assert not result["success"]
    assert "main.c:1" in result["error"]


def test_cpm_rejects_custom_options_and_inline_file_directive():
    assert not compile_cpm("void main(void) {}", options=["-O"])["success"]
    result = compile_cpm("""
        void main(void) {
            __asm
                .include "local.asm"
            __endasm;
        }
    """)
    assert not result["success"]
    assert "inline assembly cannot read files" in result["error"]
