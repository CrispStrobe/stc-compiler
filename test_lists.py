"""LIST — the mutable, RAM-resident sibling of TABLE.

The dialect is sb3-creator's, which is Scratch's: `add X to L`,
`delete N of L`, `delete all of L`, `insert X at N of L`,
`replace item N of L with X`, and the reporters `item N of L`,
`length of L`, `L contains X`. Closing a gap where this reference
implementation lagged the JS one (DIVERGENCES.md, first section).

Three things are load-bearing and silent if wrong, so each is pinned:
one-based indices, out-of-range being inert rather than wild, and a
capacity that cannot be exceeded. The last two are checked by RUNNING the
emitted C, not by reading it.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

import stc_pseudocode as sp

HEAD = "DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"


def program(body, decls="  LIST xs = 1, 2, 3\n"):
    return HEAD + decls + "\n  WHEN started:\n" + body


class TestDeclaration:
    def test_initialiser_sizes_the_list(self):
        p = sp.parse(program("    stop\n"))
        assert p.lists == {"xs": (3, [1, 2, 3])}

    def test_explicit_size(self):
        p = sp.parse(program("    stop\n", "  LIST ys SIZE 20\n"))
        assert p.lists == {"ys": (20, [])}

    def test_bare_declaration_gets_the_default(self):
        p = sp.parse(program("    stop\n", "  LIST zs\n"))
        assert p.lists == {"zs": (sp.LIST_DEFAULT_CAP, [])}

    def test_declared_twice_is_refused(self):
        with pytest.raises(sp.PseudocodeError, match="declared twice"):
            sp.parse(program("    stop\n", "  LIST xs = 1\n  LIST xs = 2\n"))

    def test_a_name_cannot_be_both_table_and_list(self):
        """One is constants in flash and the other variables in RAM; a name
        that is both would silently resolve to whichever check ran first."""
        with pytest.raises(sp.PseudocodeError, match="already a TABLE"):
            sp.parse(program("    stop\n", "  TABLE xs = 1, 2\n  LIST xs = 3\n"))

    def test_capacity_has_a_ceiling(self):
        """2 bytes an item against 256 bytes of IRAM. Without this the first
        complaint comes from the linker, much later, about something else."""
        with pytest.raises(sp.PseudocodeError, match="ceiling"):
            sp.parse(program("    stop\n", "  LIST big SIZE 500\n"))

    def test_non_constant_initialiser_is_refused(self):
        with pytest.raises(sp.PseudocodeError, match="not a constant"):
            sp.parse(program("    stop\n", "  LIST xs = 1, n\n"))


class TestVerbs:
    ALL = ("    add 7 to xs\n"
           "    delete 1 of xs\n"
           "    insert 9 at 2 of xs\n"
           "    replace item 1 of xs with 42\n"
           "    set n to item 2 of xs\n"
           "    set m to length of xs\n"
           "    IF xs contains 9 THEN:\n"
           "      delete all of xs\n")

    def test_every_verb_parses(self):
        body = sp.parse(program(self.ALL)).whens[0]
        kinds = [type(s).__name__ for s in body]
        assert kinds == ["ListAdd", "ListDelete", "ListInsert", "ListReplace",
                         "SetVar", "SetVar", "If"]

    def test_delete_all_beats_delete_n(self):
        """`delete <n> of L` would match `delete all of L` with n = "all" and
        fail much later, inside the expression parser, about something else."""
        body = sp.parse(program("    delete all of xs\n")).whens[0]
        assert isinstance(body[0], sp.ListDeleteAll)

    def test_an_undeclared_list_is_not_a_list_verb(self):
        with pytest.raises(sp.PseudocodeError):
            sp.parse(program("    add 1 to nope\n"))

    def test_a_bare_list_name_says_how_to_read_it(self):
        with pytest.raises(sp.PseudocodeError, match="item <n> of xs"):
            sp.parse(program("    set n to xs\n"))

    def test_round_trip_is_a_fixed_point(self):
        source = program(self.ALL, "  LIST xs = 1, 2, 3\n  LIST ys SIZE 8\n  LIST zs\n")
        once = sp.decompile(source)
        assert once == sp.decompile(once)

    def test_round_trip_keeps_every_declaration_form(self):
        """A dropped LIST line does not merely lose the declaration -- the
        re-parse then fails, because the verbs no longer resolve."""
        source = program(self.ALL, "  LIST xs = 1, 2, 3\n  LIST ys SIZE 8\n  LIST zs\n")
        back = sp.decompile(source)
        assert "LIST xs = 1, 2, 3" in back
        assert "LIST ys SIZE 8" in back
        assert "LIST zs" in back

    def test_a_compound_index_keeps_its_parentheses(self):
        """`item <n> of L` parses its index as an atom so the `of` is not
        swallowed, so re-emitting one has to bracket anything compound."""
        source = program("    set n to item (a + 1) of xs\n")
        back = sp.decompile(source)
        assert "item (a + 1) of xs" in back
        assert sp.decompile(back) == back


class TestEmission:
    def test_only_the_helpers_used_are_emitted(self):
        """An unused static function is -Wunused-function, and the AVR
        goldens build under -Werror in CI."""
        c = sp.emit(sp.parse(program("    add 1 to xs\n")))
        assert "bw_list_add" in c
        for absent in ("bw_list_ins", "bw_list_del", "bw_list_has"):
            assert absent not in c

    def test_length_is_emitted_without_a_cast(self):
        """The width of a value belongs to the target, not to expr_c --
        scripts/test-golden.py reads the source and forbids `(int)` there."""
        c = sp.emit(sp.parse(program("    set m to length of xs\n")))
        assert "m = bw_list_xs_len;" in c
        assert "(int)bw_list" not in c

    def test_capacity_reaches_the_growing_calls(self):
        c = sp.emit(sp.parse(program("    add 1 to xs\n    insert 2 at 1 of xs\n")))
        assert "bw_list_add(bw_list_xs, &bw_list_xs_len, 3, 1);" in c
        assert "bw_list_ins(bw_list_xs, &bw_list_xs_len, 3, 1, 2);" in c

    def test_delete_all_needs_no_helper(self):
        c = sp.emit(sp.parse(program("    delete all of xs\n")))
        assert "bw_list_xs_len = 0;" in c

    def test_the_scheduler_path_lowers_them_too(self):
        """Two WHEN blocks switch the emitter from stmts_c to stmts_task. A
        statement handled in one and not the other changes meaning when a
        second script is added."""
        source = (HEAD + "  LIST xs = 1, 2, 3\n\n"
                  "  WHEN started:\n    add 7 to xs\n\n"
                  "  WHEN started:\n    replace item 1 of xs with 5\n")
        c = sp.emit(sp.parse(source))
        assert "bw_list_add(" in c and "bw_list_set(" in c


class TestTargets:
    @pytest.mark.parametrize("device,pin,clock", [
        ("STC12C5A60S2", "P1.0", 11059200),
        ("STC89C52RC", "P1.0", 11059200),
        ("ATMEGA328P", "D13", 16000000),
        ("ARDUINO-UNO", "D13", 16000000),
    ])
    def test_c_targets_emit_lists(self, device, pin, clock):
        source = (f"DEVICE {device}:\n  CLOCK {clock}\n  LIST xs = 1, 2, 3\n"
                  f"  PIN led = {pin} OUTPUT\n\n  WHEN started:\n"
                  f"    add 7 to xs\n    set n to item 1 of xs\n")
        c = sp.emit(sp.parse(source))
        assert "bw_list_xs" in c

    @pytest.mark.parametrize("device", ["MICROBIT", "PICO", "ARCADE"])
    def test_targets_without_lists_refuse_by_name(self, device):
        """MicroPython and TypeScript both have real lists; what is missing is
        the lowering, not the capability. Until it exists the refusal has to
        name the board rather than emit something that does not work."""
        with pytest.raises(sp.PseudocodeError) as caught:
            sp.parse(f"DEVICE {device}\n  LIST xs = 1, 2\n\n  WHEN started:\n    stop\n")
        assert "LIST is not available" in str(caught.value)


# ---- the semantics, executed rather than read -------------------------------

HARNESS = r"""
#include <stdio.h>
#include <string.h>
%s
static int A[8]; static unsigned char N;
static void show(void){
    printf("[");
    for (unsigned char k = 0; k < N; k++) printf("%%s%%d", k ? " " : "", A[k]);
    printf("] %%u\n", N);
}
int main(void){
    memset(A, 0, sizeof A); N = 0;
    bw_list_add(A,&N,8,10); bw_list_add(A,&N,8,20); bw_list_add(A,&N,8,30); show();
    printf("%%d %%d %%d %%d\n", bw_list_get(A,N,1), bw_list_get(A,N,3),
                             bw_list_get(A,N,0), bw_list_get(A,N,4));
    bw_list_set(A,N,2,99); show();
    bw_list_set(A,N,9,7);  show();
    bw_list_ins(A,&N,8,1,5); show();
    bw_list_ins(A,&N,8,5,6); show();
    bw_list_del(A,&N,1); show();
    bw_list_del(A,&N,99); show();
    printf("%%u %%u\n", bw_list_has(A,N,99), bw_list_has(A,N,12345));
    N = 0; for (int i = 0; i < 12; i++) bw_list_add(A,&N,8,i); show();
    return 0;
}
"""


class TestSemanticsByExecution:
    """Reading the C proves it compiles. One-based indexing, an out-of-range
    write being inert, and `add` stopping at capacity are all silent when
    wrong, so the helpers are compiled for the host and RUN."""

    def test_scratch_semantics_hold(self):
        cc = shutil.which("cc") or shutil.which("gcc")
        if not cc:
            pytest.skip("no host C compiler")
        helpers = "\n".join(
            "\n".join(sp.LIST_HELPERS[k]) for k in
            ("get", "set", "add", "del", "ins", "has"))
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "t.c")
            with open(src, "w") as handle:
                handle.write(HARNESS % helpers)
            exe = os.path.join(d, "t")
            build = subprocess.run([cc, "-O1", "-o", exe, src],
                                   capture_output=True, text=True)
            assert build.returncode == 0, build.stderr[:400]
            out = subprocess.run([exe], capture_output=True, text=True).stdout
        lines = out.strip().splitlines()
        assert lines[0] == "[10 20 30] 3"
        # 1-based: item 1 is the first, item 0 and item 4 are out of range.
        assert lines[1] == "10 30 0 0"
        assert lines[2] == "[10 99 30] 3"          # replace item 2
        assert lines[3] == "[10 99 30] 3"          # replace item 9 is dropped
        assert lines[4] == "[5 10 99 30] 4"        # insert at 1 shifts up
        assert lines[5] == "[5 10 99 30 6] 5"      # insert at len+1 appends
        assert lines[6] == "[10 99 30 6] 4"        # delete 1 shifts down
        assert lines[7] == "[10 99 30 6] 4"        # delete 99 is dropped
        assert lines[8] == "1 0"                   # contains
        # Twelve adds into a capacity of eight: stops, never overflows.
        assert lines[9] == "[0 1 2 3 4 5 6 7] 8"
