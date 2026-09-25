"""test_service_hardening — the compile endpoint's abuse guard and the health
report's RISC-V lines (menu items 4 and 5).

The rate limit is best-effort and per-instance (Vercel is stateless across
instances), so this exercises the in-process mechanism directly: a burst from
one IP is throttled, a second IP is not, and a direct in-process call (no
Request) is trusted and skips the guard.
"""
import asyncio
import unittest

import app


class FakeReq:
    """Minimal stand-in for a Starlette Request: an IP + headers."""
    def __init__(self, ip, xff=None):
        self._ip = ip
        self.headers = {"x-forwarded-for": xff} if xff else {}

        class _C:
            host = ip
        self.client = _C()
    # app._client_ip reads .headers.get and .client.host
    # (dict.get already satisfies the header lookup)


SRC = app.CompileReq(code="int main(){return 0;}", language="c", target="riscv32-gcc")


class RateLimit(unittest.TestCase):
    def setUp(self):
        # isolate each test: clear the shared per-instance counters
        with app._rl_lock:
            app._rl_hits.clear()
            app._rl_inflight = 0

    def test_a_burst_from_one_ip_is_eventually_throttled(self):
        ip = "203.0.113.7"
        allowed = 0
        throttled = 0
        for _ in range(app._RL_MAX_PER_IP + 5):
            if app._rate_ok(FakeReq(ip)):
                allowed += 1
            else:
                throttled += 1
        self.assertEqual(allowed, app._RL_MAX_PER_IP, "exactly the budget is allowed")
        self.assertEqual(throttled, 5, "the rest are refused")

    def test_a_different_ip_has_its_own_budget(self):
        for _ in range(app._RL_MAX_PER_IP):
            app._rate_ok(FakeReq("198.51.100.1"))
        # a fresh IP is unaffected by the first one's exhaustion
        self.assertTrue(app._rate_ok(FakeReq("198.51.100.2")))

    def test_x_forwarded_for_is_used_over_the_socket_peer(self):
        # Vercel puts the real client in X-Forwarded-For; the socket peer is a proxy.
        self.assertEqual(app._client_ip(FakeReq("10.0.0.1", xff="9.9.9.9, 10.0.0.1")), "9.9.9.9")

    def test_the_guarded_endpoint_returns_429_when_the_ip_is_over_budget(self):
        ip = "203.0.113.9"
        for _ in range(app._RL_MAX_PER_IP):
            app._rate_ok(FakeReq(ip))       # exhaust the budget
        resp = app._guarded_build(SRC, FakeReq(ip))
        self.assertEqual(getattr(resp, "status_code", 200), 429)

    def test_a_direct_call_with_no_request_is_trusted_and_skips_the_guard(self):
        ip = "203.0.113.11"
        for _ in range(app._RL_MAX_PER_IP + 3):
            app._rate_ok(FakeReq(ip))       # would 429 an HTTP caller
        # a direct in-process call passes no Request and must still compile
        out = asyncio.run(app.compile_source(SRC, None))
        # (skips if the riscv-gcc bundle is absent — then it's a compile refusal,
        # not a rate-limit one; either way it is NOT throttled)
        self.assertNotEqual(out.get("stage") if isinstance(out, dict) else None, "rate-limit")


class HealthRiscv(unittest.TestCase):
    def test_health_reports_the_riscv_targets_and_toolchains(self):
        h = asyncio.run(app.health())
        self.assertTrue(h.get("ok"))
        self.assertIn("riscv_targets", h)
        self.assertIn("riscv32", h["riscv_targets"])
        self.assertIn("riscv32-gcc", h["riscv_targets"])
        # both toolchain keys are present (value may be None where a toolchain
        # is absent on this host — the key itself is the contract)
        self.assertIn("riscv_gcc", h)
        self.assertIn("riscv_wasm", h)


if __name__ == "__main__":
    unittest.main()
