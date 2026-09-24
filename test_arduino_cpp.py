"""test_arduino_cpp — the `arduino` language route compiles real Arduino C++.

Two halves. The first is the .ino preprocessing, which needs no toolchain:
which functions get prototypes, which must not, and that every diagnostic
still names the line the user wrote. The second builds sketches that use
exactly what the C-only route could not -- Serial, String, classes, templates,
the bundled libraries -- for every board the route accepts.

Whether the images RUN, and print what they should, is a separate check:
scripts/test-arduino-run.py drives them in avr8js.
"""
import asyncio
import base64

import pytest

import app
import arduino_build as ab


def compile_sketch(code, target="atmega328p", **kw):
    return asyncio.run(app.compile_source(
        app.CompileReq(code=code, language="arduino", target=target, **kw)))


# ------------------------------------------------------------ preprocessing

def protos(code):
    return ab.prepare_sketch(code)[1]


def test_forward_called_function_gets_a_prototype():
    code = "void setup() { helper(3); }\nvoid loop() {}\nint helper(int x) { return x; }\n"
    assert protos(code) == ["void setup();", "void loop();", "int helper(int x);"]


def test_prototypes_go_before_the_first_function_after_the_globals():
    code = "#define N 3\nint table[N] = {1, 2, 3};\nvoid setup() {}\nvoid loop() {}\n"
    src, _ = ab.prepare_sketch(code)
    assert src.index("int table[N]") < src.index("void setup();") < src.index("void setup() {}")


def test_arduino_h_comes_first_and_line_numbers_are_the_sketchs():
    code = "int g;\n\nvoid setup() {}\nvoid loop() {}\n"
    src, _ = ab.prepare_sketch(code, "blink.ino")
    assert src.startswith('#include <Arduino.h>\n#line 1 "blink.ino"\n')
    # After the inserted prototypes the numbering resumes at the sketch's
    # own line 3, where setup() is.
    assert '#line 3 "blink.ino"\nvoid setup() {}' in src


def test_methods_and_class_bodies_are_not_prototyped():
    code = ("class Led {\n public:\n  void on() { x = 1; }\n  int x;\n};\n"
            "void Led2::off() {}\nvoid setup() {}\nvoid loop() {}\n")
    assert protos(code) == ["void setup();", "void loop();"]


def test_control_flow_and_initialisers_are_not_functions():
    code = "int a = max(1, 2);\nvoid setup() { if (a) { a++; } }\nvoid loop() {}\n"
    assert protos(code) == ["void setup();", "void loop();"]


def test_braces_in_strings_and_comments_do_not_confuse_the_scan():
    code = ('const char *s = "{ ( not code";\n// void fake() {\n/* } */\n'
            "void setup() { Serial.print(\"}\"); }\nvoid loop() {}\n")
    assert protos(code) == ["void setup();", "void loop();"]


def test_default_arguments_are_left_to_the_user():
    # Repeating a default in a prototype makes the definition an error.
    code = "void setup() {}\nvoid loop() {}\nvoid blink(int n = 3) {}\n"
    assert protos(code) == ["void setup();", "void loop();"]


def test_templates_are_left_to_the_user():
    code = "template <typename T> T twice(T x) { return x + x; }\nvoid setup() {}\nvoid loop() {}\n"
    assert protos(code) == ["void setup();", "void loop();"]


def test_a_type_defined_after_the_insertion_point_is_not_prototyped():
    code = ("void setup() {}\nvoid loop() {}\n"
            "struct Point { int x; };\nvoid show(Point p) {}\n")
    assert protos(code) == ["void setup();", "void loop();"]


def test_pointer_and_qualified_return_types():
    code = ("void setup() {}\nvoid loop() {}\n"
            "const char *name(unsigned long id) { return \"x\"; }\n"
            "static inline uint8_t low(uint16_t v) { return v; }\n")
    assert protos(code)[2:] == ["const char *name(unsigned long id);",
                                "static inline uint8_t low(uint16_t v);"]


def test_includes_are_found_for_library_resolution():
    assert ab.included_headers('#include <Wire.h>\n  # include "EEPROM.h"\n') == ["Wire.h", "EEPROM.h"]


# ------------------------------------------------------------------ builds

CPP_SKETCH = """
class Counter {
 public:
  explicit Counter(int start) : n(start) {}
  int next() { return n++; }
 private:
  int n;
};

template <typename T> T twice(T x) { return x + x; }

Counter counter(40);
String label = "count";

void setup() {
  Serial.begin(9600);
  Serial.println(F("hello from C++"));
  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  report(twice(counter.next()));
  digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
  delay(10);
}

void report(int v) {
  String s = label + "=" + String(v) + " pi=" + String(3.14159, 3);
  Serial.println(s);
}
"""


@pytest.mark.parametrize("target", sorted(ab.BOARDS))
def test_cpp_sketch_builds_on_every_board(target):
    r = compile_sketch(CPP_SKETCH, target)
    assert r["success"], r.get("error")
    spec = ab.BOARDS[target]
    assert r["mcu"] == spec["mcu"] and r["variant"] == spec["variant"]
    assert r["toolchain"] == ("avr-gcc+ATTinyCore" if spec["core"] == "tiny"
                              else "avr-gcc+ArduinoCore-avr")
    assert "void report(int v);" in r["prototypes"]
    text = base64.b64decode(r["base64"]).decode()
    assert text.startswith(":") and text.rstrip().endswith(":00000001FF")
    assert r["f_cpu"] == spec["default_clock"]


@pytest.mark.parametrize("target,inputs", [("arduino-uno", 6), ("arduino-nano", 8),
                                           ("arduino-mega", 16)])
def test_each_board_gets_its_own_variant(target, inputs):
    # The Nano's eightanaloginputs variant is the Uno's plus ADC6/ADC7; a
    # route that built every ATmega328P as an Uno would get this wrong.
    code = (f"static_assert(NUM_ANALOG_INPUTS == {inputs}, \"variant\");\n"
            "void setup() {}\nvoid loop() {}\n")
    r = compile_sketch(code, target)
    assert r["success"], r.get("error")


def test_the_clock_is_the_boards_unless_named():
    code = "void setup() {}\nvoid loop() {}\n"
    assert compile_sketch(code, "atmega328p")["f_cpu"] == 16000000
    assert compile_sketch(code, "attiny85")["f_cpu"] == 8000000
    assert compile_sketch(code, "attiny85", fosc=1000000)["f_cpu"] == 1000000


@pytest.mark.parametrize("target,header,use", [
    ("atmega328p", "Wire.h", "Wire.begin(); Wire.beginTransmission(0x3C); Wire.write(0); Wire.endTransmission();"),
    ("atmega328p", "SPI.h", "SPI.begin(); SPI.transfer(0x55);"),
    ("atmega328p", "EEPROM.h", "EEPROM.write(0, 7); Serial.println(EEPROM.read(0));"),
    ("atmega328p", "SoftwareSerial.h", "static SoftwareSerial s(10, 11); s.begin(9600); s.print(1);"),
    ("atmega2560", "Wire.h", "Wire.begin();"),
    ("attiny85", "Wire.h", "Wire.begin(); Wire.beginTransmission(0x3C); Wire.endTransmission();"),
    ("attiny85", "EEPROM.h", "EEPROM.write(0, 7);"),
    ("attiny88", "SPI.h", "SPI.begin(); SPI.transfer(1);"),
])
def test_bundled_libraries(target, header, use):
    r = compile_sketch(f"#include <{header}>\nvoid setup() {{ {use} }}\nvoid loop() {{}}\n", target)
    assert r["success"], r.get("error")
    assert header[:-2] in r["libraries"]


def test_errors_name_the_sketch_line():
    code = "int x;\n\nvoid setup() {\n  undefinedThing();\n}\nvoid loop() {}\n"
    r = compile_sketch(code)
    assert not r["success"]
    assert "main.ino:4" in r["error"], r["error"]
    assert "/tmp" not in r["error"]


def test_an_unbundled_library_says_so():
    r = compile_sketch("#include <Servo.h>\nvoid setup() {}\nvoid loop() {}\n")
    assert not r["success"]
    assert "Servo.h" in r["error"] and "not available" in r["error"]
    assert "Wire" in r["error"]  # names what IS there


def test_a_missing_loop_is_explained():
    r = compile_sketch("void setup() {}\n")
    assert not r["success"] and r["stage"] == "link"
    assert "setup() and loop()" in r["error"]


def test_a_sketch_may_bring_its_own_main():
    r = compile_sketch("#include <avr/io.h>\nint main() { DDRB = 1; for (;;); }\n")
    assert r["success"], r.get("error")


def test_an_image_bigger_than_the_part_is_refused():
    code = ("const uint8_t big[9000] PROGMEM = {1};\n"
            "void setup() { Serial.begin(9600); Serial.println(pgm_read_byte(&big[8999])); }\n"
            "void loop() {}\n")
    assert compile_sketch(code, "atmega328p")["success"]
    r = compile_sketch(code, "attiny85")
    assert not r["success"] and "8192 bytes of flash" in r["error"], r["error"]
    assert "/tmp" not in r["error"]


def test_bin_format_and_disassembly():
    r = compile_sketch("void setup() {}\nvoid loop() {}\n", format="bin", disassemble=True)
    assert r["success"] and r["filename"] == "main.bin"
    raw = base64.b64decode(r["base64"])
    assert raw[:2] == b"\x0c\x94"          # jmp __vectors' reset: a real AVR image
    assert r["disassembly"] and "setup" in r["disassembly"]


def test_user_defines_reach_the_core():
    # SERIAL_TX_BUFFER_SIZE is read by HardwareSerial.h, i.e. by the CORE's
    # objects; a define that reached only the sketch would link a core built
    # with the default and a sketch that believes otherwise.
    code = ("void setup() { static_assert(SERIAL_TX_BUFFER_SIZE == 16, \"x\"); }\n"
            "void loop() {}\n")
    r = compile_sketch(code, defines={"SERIAL_TX_BUFFER_SIZE": "16"})
    assert r["success"], r.get("error")


def test_bad_defines_are_refused():
    r = compile_sketch("void setup() {}\nvoid loop() {}\n", defines={"X Y": "1"})
    assert not r["success"] and "bad define" in r["error"]
    r = compile_sketch("void setup() {}\nvoid loop() {}\n", defines={"X": "1 -o/etc/x"})
    assert not r["success"] and "bad define" in r["error"]


def test_unknown_board_lists_the_known_ones():
    r = compile_sketch("void setup() {}\nvoid loop() {}\n", "esp32")
    assert not r["success"] and "arduino-uno" in r["error"]


@pytest.mark.parametrize("device", ["ARDUINO-UNO", "ARDUINO-NANO", "ARDUINO-MEGA"])
def test_pseudocode_for_an_arduino_board_now_builds(device):
    src = (f"DEVICE {device}:\n  NAME blink\n  PIN led = D13 OUTPUT\n"
           "  WHEN started:\n    FOREVER:\n      toggle led\n      wait 100 ms\n")
    r = asyncio.run(app.compile_source(app.CompileReq(code=src, language="pseudocode")))
    assert r["success"], r.get("error")
    assert r["filename"] == "blink.hex"
    assert "#include <Arduino.h>" in r["c"]      # the generated sketch comes back
    assert r["variant"] == ab.BOARDS[device.lower()]["variant"]
