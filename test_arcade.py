"""Tests for the Arcade game-engine target (bw_arcade.py).

Covers: parsing arcade verbs, pseudocode round-trip, TypeScript emission,
and the new expression nodes (randint, controller axis).
"""

import pytest
import stc_pseudocode as sp


# ---- fixtures ---------------------------------------------------------------

DODGE_EXAMPLE = """\
DEVICE ARCADE

WHEN started:
  arcade create player kind Player
  arcade place player x 80 y 100
  arcade set player stay in screen
  set score to 0
  FOREVER:
    arcade move player vx (controller dx) vy 0
    change score by 1
    arcade score add 1
    wait 1 seconds

WHEN started:
  set spawnx to 0
  FOREVER:
    set spawnx to randint(10, 150)
    arcade create enemy kind Enemy
    arcade place enemy x spawnx y 0
    arcade move enemy vx 0 vy 40
    arcade set enemy destroy on wall
    wait 800 ms

WHEN started:
  ARCADE ON OVERLAP Player Enemy:
    arcade game over lose
"""

MINIMAL_SPRITE = """\
DEVICE ARCADE

WHEN started:
  arcade create hero kind Hero
  arcade place hero x 40 y 60
"""

CONDITIONAL_GAME = """\
DEVICE ARCADE

WHEN started:
  set lives to 3
  arcade create ship kind Ship
  FOREVER:
    IF lives = 0 THEN:
      arcade game over lose
    ELSE:
      arcade score add 1
    wait 500 ms
"""

OVERLAP_HANDLER = """\
DEVICE ARCADE

WHEN started:
  arcade create bullet kind Bullet
  ARCADE ON OVERLAP Bullet Enemy:
    arcade score add 10
    arcade game over win
"""


# ---- parse + round-trip -----------------------------------------------------

class TestArcadeParse:
    def test_device_arcade_recognized(self):
        program = sp.parse("DEVICE ARCADE\nWHEN started:\n  set x to 1")
        assert program.part == "arcade"

    def test_dodge_parses(self):
        program = sp.parse(DODGE_EXAMPLE)
        assert program.part == "arcade"
        assert len(program.whens) == 3
        assert "score" in program.variables
        assert "spawnx" in program.variables

    def test_arcade_create(self):
        program = sp.parse(MINIMAL_SPRITE)
        stmt = program.whens[0][0]
        assert isinstance(stmt, sp.ArcadeCreate)
        assert stmt.sprite == "hero"
        assert stmt.kind == "Hero"

    def test_arcade_place(self):
        program = sp.parse(MINIMAL_SPRITE)
        stmt = program.whens[0][1]
        assert isinstance(stmt, sp.ArcadePlace)
        assert stmt.sprite == "hero"
        assert isinstance(stmt.x, sp.Num) and stmt.x.value == 40
        assert isinstance(stmt.y, sp.Num) and stmt.y.value == 60

    def test_arcade_move(self):
        program = sp.parse(DODGE_EXAMPLE)
        # Script 1: create, place, flag, set score, FOREVER
        forever = program.whens[0][4]
        assert isinstance(forever, sp.Forever)
        move = forever.body[0]
        assert isinstance(move, sp.ArcadeMove)
        assert move.sprite == "player"
        assert isinstance(move.vx, sp.ControllerAxis)
        assert move.vx.axis == "dx"

    def test_arcade_set_flag_stay_in_screen(self):
        program = sp.parse(MINIMAL_SPRITE)
        # After create and place, we'd need a flag stmt. Use DODGE_EXAMPLE.
        program = sp.parse(DODGE_EXAMPLE)
        stmt = program.whens[0][2]
        assert isinstance(stmt, sp.ArcadeSetFlag)
        assert stmt.flag == "stayinscreen"

    def test_arcade_set_flag_destroy_on_wall(self):
        program = sp.parse(DODGE_EXAMPLE)
        forever = program.whens[1][1]  # second WHEN, first is set spawnx
        body = forever.body
        # Find the destroy on wall flag
        flags = [s for s in body if isinstance(s, sp.ArcadeSetFlag)]
        assert len(flags) == 1
        assert flags[0].flag == "destroyonwall"

    def test_arcade_score(self):
        program = sp.parse(DODGE_EXAMPLE)
        forever = program.whens[0][4]
        scores = [s for s in forever.body if isinstance(s, sp.ArcadeScore)]
        assert len(scores) == 1
        assert isinstance(scores[0].delta, sp.Num)
        assert scores[0].delta.value == 1

    def test_arcade_game_over(self):
        program = sp.parse(DODGE_EXAMPLE)
        # Script 3 → ARCADE ON OVERLAP → body[0] is game over
        overlap = program.whens[2][0]
        assert isinstance(overlap, sp.ArcadeOnOverlap)
        game_over = overlap.body[0]
        assert isinstance(game_over, sp.ArcadeGameOver)
        assert game_over.win is False

    def test_arcade_game_over_win(self):
        program = sp.parse(OVERLAP_HANDLER)
        overlap = program.whens[0][1]
        game_over = overlap.body[1]
        assert isinstance(game_over, sp.ArcadeGameOver)
        assert game_over.win is True

    def test_arcade_on_overlap(self):
        program = sp.parse(DODGE_EXAMPLE)
        overlap = program.whens[2][0]
        assert isinstance(overlap, sp.ArcadeOnOverlap)
        assert overlap.kind_a == "Player"
        assert overlap.kind_b == "Enemy"
        assert len(overlap.body) == 1

    def test_randint_expression(self):
        program = sp.parse(DODGE_EXAMPLE)
        forever = program.whens[1][1]
        set_stmt = forever.body[0]
        assert isinstance(set_stmt, sp.SetVar)
        assert isinstance(set_stmt.value, sp.Randint)
        assert isinstance(set_stmt.value.low, sp.Num)
        assert set_stmt.value.low.value == 10
        assert set_stmt.value.high.value == 150

    def test_controller_axis_expression(self):
        program = sp.parse(DODGE_EXAMPLE)
        forever = program.whens[0][4]
        move = forever.body[0]
        assert isinstance(move.vx, sp.ControllerAxis)
        assert move.vx.axis == "dx"


# ---- pseudocode round-trip --------------------------------------------------

class TestArcadeRoundTrip:
    def test_dodge_round_trip(self):
        program = sp.parse(DODGE_EXAMPLE)
        pseudo1 = sp.emit_pseudocode(program)
        program2 = sp.parse(pseudo1)
        pseudo2 = sp.emit_pseudocode(program2)
        assert pseudo1 == pseudo2, "pseudocode is not a fixed point"

    def test_minimal_round_trip(self):
        program = sp.parse(MINIMAL_SPRITE)
        pseudo1 = sp.emit_pseudocode(program)
        program2 = sp.parse(pseudo1)
        pseudo2 = sp.emit_pseudocode(program2)
        assert pseudo1 == pseudo2

    def test_conditional_round_trip(self):
        program = sp.parse(CONDITIONAL_GAME)
        pseudo1 = sp.emit_pseudocode(program)
        program2 = sp.parse(pseudo1)
        pseudo2 = sp.emit_pseudocode(program2)
        assert pseudo1 == pseudo2

    def test_overlap_round_trip(self):
        program = sp.parse(OVERLAP_HANDLER)
        pseudo1 = sp.emit_pseudocode(program)
        program2 = sp.parse(pseudo1)
        pseudo2 = sp.emit_pseudocode(program2)
        assert pseudo1 == pseudo2


# ---- TypeScript emission -----------------------------------------------------

class TestArcadeEmit:
    def test_source_language(self):
        program = sp.parse(MINIMAL_SPRITE)
        # ArcadeTarget has a custom emit, so source_language won't say "c"
        assert sp.source_language(program) != "c"

    def test_emit_header(self):
        program = sp.parse(MINIMAL_SPRITE)
        ts = sp.emit(program)
        assert "// Generated from BrickWright pseudocode" in ts
        assert "MakeCode Arcade" in ts

    def test_emit_sprite_kinds(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "namespace SpriteKind {" in ts
        assert "export const Player = SpriteKind.create()" in ts
        assert "export const Enemy = SpriteKind.create()" in ts

    def test_emit_sprite_create(self):
        program = sp.parse(MINIMAL_SPRITE)
        ts = sp.emit(program)
        assert "hero = sprites.create(img`.`, SpriteKind.Hero)" in ts

    def test_emit_set_position(self):
        program = sp.parse(MINIMAL_SPRITE)
        ts = sp.emit(program)
        assert "hero.setPosition(40, 60)" in ts

    def test_emit_set_velocity(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "player.setVelocity(controller.dx(), 0)" in ts

    def test_emit_stay_in_screen(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "player.setStayInScreen(true)" in ts

    def test_emit_destroy_on_wall(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "enemy.setFlag(SpriteFlag.DestroyOnWall, true)" in ts

    def test_emit_change_score(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "info.changeScoreBy(1)" in ts

    def test_emit_game_over(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "game.over(false)" in ts

    def test_emit_game_over_win(self):
        program = sp.parse(OVERLAP_HANDLER)
        ts = sp.emit(program)
        assert "game.over(true)" in ts

    def test_emit_on_overlap(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "sprites.onOverlap(SpriteKind.Player, SpriteKind.Enemy" in ts

    def test_emit_randint(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "randint(10, 150)" in ts

    def test_emit_controller_dx(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "controller.dx()" in ts

    def test_emit_forever_as_game_update(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "game.onUpdate(function ()" in ts

    def test_emit_pause(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "pause(1000)" in ts
        assert "pause(800)" in ts

    def test_emit_if_else(self):
        program = sp.parse(CONDITIONAL_GAME)
        ts = sp.emit(program)
        assert "if (lives == 0) {" in ts
        assert "} else {" in ts

    def test_emit_variable_declarations(self):
        program = sp.parse(DODGE_EXAMPLE)
        ts = sp.emit(program)
        assert "let player: Sprite = null" in ts
        assert "let enemy: Sprite = null" in ts
        assert "let score = 0" in ts

    def test_emit_controller_dy(self):
        source = """\
DEVICE ARCADE
WHEN started:
  arcade create ship kind Ship
  FOREVER:
    arcade move ship vx (controller dx) vy (controller dy)
"""
        ts = sp.emit(sp.parse(source))
        assert "controller.dx()" in ts
        assert "controller.dy()" in ts

    def test_emit_repeat(self):
        source = """\
DEVICE ARCADE
WHEN started:
  REPEAT 5:
    arcade score add 1
"""
        ts = sp.emit(sp.parse(source))
        assert "for (let _i = 0; _i < 5; _i++) {" in ts

    def test_dodge_example_file(self):
        """The committed example file parses and emits without error."""
        source = open("docs/arcade-example-dodge.bw").read()
        program = sp.parse(source)
        ts = sp.emit(program)
        assert "SpriteKind.Player" in ts
        assert "SpriteKind.Enemy" in ts
        assert "game.over(false)" in ts


# ---- error handling ---------------------------------------------------------

class TestArcadeErrors:
    def test_pin_refused(self):
        with pytest.raises(sp.PseudocodeError, match="no GPIO pins"):
            sp.parse("DEVICE ARCADE\nPIN led = P1.0 OUTPUT")

    def test_unknown_device(self):
        with pytest.raises(sp.PseudocodeError, match="unknown device"):
            sp.parse("DEVICE FOOBAR\nWHEN started:\n  set x to 1")
