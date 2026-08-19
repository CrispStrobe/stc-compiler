"""
bw_arcade — BrickWright-style pseudocode → MakeCode Arcade TypeScript.

Emits TypeScript that the PXT compiler (microsoft/pxt, MIT) compiles to
the JS fiber format the Arcade simulator's `run` message expects. The
block surface — sprites, tilemaps, controller, game state — maps directly
to the Arcade API in `pxt-common-packages`.

Provenance: all MIT, attribution only. See docs/ARCADE-SCOPING.md §0.
"""

from __future__ import annotations

import stc_pseudocode as sp


# TypeScript operator spellings. Same structure as bw_micropython.TO_PYTHON.
TO_TS = {"or": "||", "and": "&&", "=": "==", "!=": "!=",
         "<": "<", ">": ">", "<=": "<=", ">=": ">=",
         "+": "+", "-": "-", "*": "*", "/": "/", "%": "%"}


class ArcadeTarget(sp.Target):
    """MakeCode Arcade — 160×120 colour TFT game engine."""

    key = "arcade"
    display = "MakeCode Arcade"
    toolchain = "pxt"
    default_clock = 0       # no crystal; the game loop is the clock
    supports = frozenset({"print", "table"})
    source_extension = "ts"
    compile_hint = ("MakeCode Arcade TypeScript is compiled by the PXT "
                    "compiler into JS for the Arcade simulator, or into "
                    "ARM Thumb for physical hardware.")

    def resolve_pin(self, program, name, where, direction, active_low, line):
        raise sp.PseudocodeError(
            line, "MakeCode Arcade has no GPIO pins; use arcade verbs "
                  "instead (arcade create, arcade move, etc.)")

    def resolve_port(self, program, name, where, direction, active_low, line):
        raise sp.PseudocodeError(
            line, "MakeCode Arcade has no GPIO ports")

    # ---- the emitter -------------------------------------------------------
    def emit(self, program) -> str:
        out = [
            "// Generated from BrickWright pseudocode by stc-compiler.",
            "// Hand edits will be lost; change the pseudocode instead.",
            "//",
            "// Target: MakeCode Arcade (MIT). See docs/ARCADE-SCOPING.md.",
            "",
        ]

        # Collect all sprite kinds used by ArcadeCreate nodes.
        kinds = set()
        for node in _walk(program):
            if isinstance(node, sp.ArcadeCreate):
                kinds.add(node.kind)

        if kinds:
            out.append("// Sprite kinds.")
            out.append("namespace SpriteKind {")
            for kind in sorted(kinds):
                out.append(f"    export const {kind} = SpriteKind.create()")
            out.append("}")
            out.append("")

        # Variable declarations.
        # Sprite variables are declared as `let name: Sprite = null`.
        sprite_vars = set()
        for node in _walk(program):
            if isinstance(node, sp.ArcadeCreate):
                sprite_vars.add(node.sprite)

        for name in sprite_vars:
            out.append(f"let {name}: Sprite = null")

        if program.variables:
            for name in program.variables:
                out.append(f"let {name} = 0")

        if sprite_vars or program.variables:
            out.append("")

        # Procedures.
        for procedure in program.procedures.values():
            params = ", ".join(f"{p}: number" for p in procedure.params)
            out.append(f"function {procedure.c_name}({params}) {{")
            out += self._stmts(procedure.body, 1, program)
            out.append("}")
            out.append("")

        # WHEN blocks → top-level statements.
        for number, block in enumerate(program.whens):
            hat = program.when_hats[number] if number < len(program.when_hats) else None
            if hat is None:
                out.append(f"// WHEN started: (script {number + 1})")
            else:
                out.append(f"// WHEN {hat[0]} {hat[1]}:")
            out += self._stmts(block, 0, program)
            out.append("")

        return "\n".join(out)

    # ---- statement lowering ------------------------------------------------
    def _stmts(self, body, depth, program) -> list[str]:
        pad = "    " * depth
        out: list[str] = []

        for node in body:
            if isinstance(node, sp.ArcadeCreate):
                out.append(f"{pad}{node.sprite} = sprites.create("
                           f"img`.`, SpriteKind.{node.kind})")
            elif isinstance(node, sp.ArcadePlace):
                out.append(f"{pad}{node.sprite}.setPosition("
                           f"{self._expr(node.x)}, {self._expr(node.y)})")
            elif isinstance(node, sp.ArcadeMove):
                out.append(f"{pad}{node.sprite}.setVelocity("
                           f"{self._expr(node.vx)}, {self._expr(node.vy)})")
            elif isinstance(node, sp.ArcadeSetFlag):
                if node.flag == "stayinscreen":
                    out.append(f"{pad}{node.sprite}.setStayInScreen(true)")
                else:
                    out.append(f"{pad}{node.sprite}.setFlag("
                               f"SpriteFlag.DestroyOnWall, true)")
            elif isinstance(node, sp.ArcadeScore):
                out.append(f"{pad}info.changeScoreBy({self._expr(node.delta)})")
            elif isinstance(node, sp.ArcadeGameOver):
                out.append(f"{pad}game.over({'true' if node.win else 'false'})")
            elif isinstance(node, sp.ArcadeOnOverlap):
                out.append(
                    f"{pad}sprites.onOverlap(SpriteKind.{node.kind_a}, "
                    f"SpriteKind.{node.kind_b}, function (sprite: Sprite, "
                    f"otherSprite: Sprite) {{")
                out += self._stmts(node.body, depth + 1, program)
                out.append(f"{pad}}})")
            elif isinstance(node, sp.ArcadeTilemap):
                out.append(f"{pad}tiles.setTilemap(tiles.createTilemap(")
                out.append(f"{pad}    hex``, // {node.name}: "
                           f"{self._expr(node.cols)}x{self._expr(node.rows)} "
                           f"grid, tile size {self._expr(node.tile_size)}")
                out.append(f"{pad}    img``,")
                out.append(f"{pad}    [myTiles.transparency16],")
                out.append(f"{pad}    TileScale.Sixteen")
                out.append(f"{pad}))")
            elif isinstance(node, sp.ArcadeSetTile):
                out.append(f"{pad}tiles.setTileAt(tiles.getTileLocation("
                           f"{self._expr(node.col)}, {self._expr(node.row)}), "
                           f"sprites.castle.tileGrass1) "
                           f"// tile index {self._expr(node.tile_index)}")
            elif isinstance(node, sp.ArcadeTileWall):
                out.append(f"{pad}scene.setTileIsWall("
                           f"{self._expr(node.tile_index)}, true) "
                           f"// tilemap {node.tilemap}")
            elif isinstance(node, sp.ArcadeSetFrame):
                out.append(f"{pad}{node.sprite}.setImage("
                           f"spritesheet_{node.sprite}[{self._expr(node.frame)}])")
            elif isinstance(node, sp.SetVar):
                out.append(f"{pad}{node.name} = {self._expr(node.value)}")
            elif isinstance(node, sp.ChangeVar):
                out.append(f"{pad}{node.name} += {self._expr(node.delta)}")
            elif isinstance(node, sp.Print):
                if node.value is None:
                    out.append(f"{pad}game.splash({node.text!r})")
                else:
                    out.append(f"{pad}game.splash(\"\" + {self._expr(node.value)})")
            elif isinstance(node, sp.Wait):
                ms = self._ms(node)
                out.append(f"{pad}pause({ms})")
            elif isinstance(node, sp.WaitUntil):
                out.append(f"{pad}while (!({self._expr(node.cond)})) {{")
                out.append(f"{pad}    pause(100)")
                out.append(f"{pad}}}")
            elif isinstance(node, sp.Forever):
                out.append(f"{pad}game.onUpdate(function () {{")
                out += self._stmts(node.body, depth + 1, program)
                out.append(f"{pad}}})")
            elif isinstance(node, sp.Repeat):
                out.append(f"{pad}for (let _i = 0; _i < {self._expr(node.count)}; _i++) {{")
                out += self._stmts(node.body, depth + 1, program)
                out.append(f"{pad}}}")
            elif isinstance(node, sp.Loop):
                test = self._expr(node.cond)
                if node.until:
                    out.append(f"{pad}while (!({test})) {{")
                else:
                    out.append(f"{pad}while ({test}) {{")
                out += self._stmts(node.body, depth + 1, program)
                out.append(f"{pad}}}")
            elif isinstance(node, sp.If):
                out.append(f"{pad}if ({self._expr(node.cond)}) {{")
                out += self._stmts(node.body, depth + 1, program)
                if node.orelse:
                    out.append(f"{pad}}} else {{")
                    out += self._stmts(node.orelse, depth + 1, program)
                out.append(f"{pad}}}")
            elif isinstance(node, sp.Call):
                args = ", ".join(self._expr(a) for a in node.args)
                out.append(f"{pad}{program.procedures[node.name.lower()].c_name}"
                           f"({args})")
            elif isinstance(node, sp.Stop):
                out.append(f"{pad}game.over(false)")
            elif isinstance(node, sp.SetPin):
                out.append(f"{pad}// pin {node.pin}: not available on Arcade")
            elif isinstance(node, sp.Toggle):
                out.append(f"{pad}// toggle {node.pin}: not available on Arcade")
            elif isinstance(node, sp.SetTone):
                hz = self._expr(node.hz)
                out.append(f"{pad}music.playTone({hz}, music.beat(BeatFraction.Whole))")
            else:
                out.append(f"{pad}// unsupported: {type(node).__name__}")
        return out

    # ---- expression lowering -----------------------------------------------
    def _expr(self, node, parent_level: int = -1) -> str:
        if isinstance(node, sp.Num):
            return str(int(node.value))
        if isinstance(node, sp.Var):
            return node.name
        if isinstance(node, sp.Randint):
            return f"randint({self._expr(node.low)}, {self._expr(node.high)})"
        if isinstance(node, sp.ControllerAxis):
            return f"controller.{'dx' if node.axis == 'dx' else 'dy'}()"
        if isinstance(node, sp.Index):
            return f"{node.table}[{self._expr(node.where)}]"
        if isinstance(node, sp.Unary):
            inner = self._expr(node.operand, sp.UNARY_LEVEL)
            return f"!({inner})" if node.op == "not" else f"-({inner})"
        if isinstance(node, sp.Binary):
            level = sp.LEVEL[node.op]
            text = (f"{self._expr(node.left, level)} "
                    f"{TO_TS[node.op]} "
                    f"{self._expr(node.right, level + 1)}")
            return f"({text})" if level < parent_level else text
        if isinstance(node, sp.PinRef):
            return f"0 /* pin {node.name}: not available on Arcade */"
        if isinstance(node, sp.PortRef):
            return f"0 /* port {node.port}: not available on Arcade */"
        raise TypeError(f"unsupported expression node: {type(node).__name__}")

    def _ms(self, node) -> str:
        if isinstance(node.amount, sp.Num):
            value = node.amount.value
            return str(int(round(value * 1000 if node.unit == "seconds"
                                 else value)))
        inner = self._expr(node.amount, sp.UNARY_LEVEL)
        return inner if node.unit == "ms" else f"({inner}) * 1000"


def _walk(program):
    """Yield every AST node in the program, for pre-pass collection."""
    for block in program.whens:
        yield from _walk_body(block)
    for proc in program.procedures.values():
        yield from _walk_body(proc.body)


def _walk_body(body):
    for node in body:
        yield node
        for field in ("body", "orelse"):
            inner = getattr(node, field, None)
            if inner:
                yield from _walk_body(inner)
