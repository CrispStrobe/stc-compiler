"""`not` binds looser than comparisons, tighter than and/or — Python's
precedence. The old atom-level `not` parsed `IF not k = shown` as
`(not k) = shown`, a boolean compared to a number: the A2's blocks
keyshow flashed clean and silently did nothing (2026-08-17)."""
import unittest

import stc_pseudocode as sp


def body_line(cond: str) -> str:
    src = (f"DEVICE STC89C52RC:\n  CLOCK 11059200\n"
           f"  PIN led1 = P1.0 OUTPUT\n  WHEN started:\n"
           f"    IF {cond} THEN:\n      toggle led1\n    wait 10 ms\n")
    c, _ = sp.transpile(src)
    return next(l.strip() for l in c.splitlines() if "if (" in l)


class TestNotPrecedence(unittest.TestCase):
    def test_not_wraps_a_comparison(self):
        self.assertEqual(body_line("not k = shown"),
                         "if (!((k == shown))) {")

    def test_not_binds_tighter_than_and(self):
        self.assertEqual(body_line("not a and b"), "if (!(a) && b) {")
        self.assertEqual(body_line("a and not b"), "if (a && !(b)) {")

    def test_double_not(self):
        self.assertEqual(body_line("not not a"), "if (!(!(a))) {")


if __name__ == "__main__":
    unittest.main()
