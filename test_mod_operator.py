"""`mod` is the sb3-creator dialect's spelling of `%`, and three gallery
programs already use it (76-multimeter, arduino-02-state-change,
eater6502-full-build). Until 2026-08-18 this front end only knew `%`, so
those programs were unparseable here — and `font[copy mod 10]` failed
with the misleading "missing ']'" because the unknown word ended the
index expression early (found authoring 78-a2-calculator)."""
import unittest

import stc_pseudocode as sp


def compile_body(expr: str) -> str:
    src = (f"DEVICE STC89C52RC:\n  CLOCK 11059200\n"
           f"  TABLE font = 1, 2, 3, 4\n  PORT segments = P0 OUTPUT\n"
           f"  WHEN started:\n    set copy to 47\n"
           f"    set segments to {expr}\n")
    c, _ = sp.transpile(src)
    return next(l.strip() for l in c.splitlines() if "P0 =" in l)


class TestModOperator(unittest.TestCase):
    def test_mod_is_percent(self):
        self.assertEqual(compile_body("copy mod 10"),
                         compile_body("copy % 10"))

    def test_mod_inside_a_table_index(self):
        line = compile_body("font[copy mod 10]")
        self.assertIn("bw_tab_font[", line)
        self.assertIn("% 10", line)

    def test_mod_precedence_matches_percent(self):
        # binds like * and /: `a + b mod 4` is `a + (b % 4)`
        self.assertEqual(compile_body("copy + copy mod 4"),
                         compile_body("copy + copy % 4"))


if __name__ == "__main__":
    unittest.main()
