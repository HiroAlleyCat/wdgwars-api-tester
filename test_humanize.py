"""Tests for the plain-English humanizers added in v0.10.0.

Run with: python -m unittest test_humanize
"""

import unittest

from wdgwars_api_tester import (
    _humanize_verdict,
    _humanize_overall,
    _humanize_delta_line,
    _humanize_verdict_summary,
    _format_webhook_payload,
)


class TestHumanizeVerdict(unittest.TestCase):
    def test_common_verdicts(self):
        self.assertEqual(_humanize_verdict("OK"), "healthy")
        self.assertEqual(_humanize_verdict("ERROR"), "timed out or unreachable")
        self.assertIn("route missing", _humanize_verdict("DEAD"))
        self.assertIn("unauthorized", _humanize_verdict("AUTH-REQUIRED"))
        self.assertIn("LiteSpeed", _humanize_verdict("LEAK"))

    def test_unknown_falls_back_to_raw(self):
        # HTTP-numeric verdicts and other unknowns return the raw string.
        self.assertEqual(_humanize_verdict("500"), "500")
        self.assertEqual(_humanize_verdict("WHATEVER"), "WHATEVER")


class TestHumanizeOverall(unittest.TestCase):
    def test_base_states(self):
        self.assertEqual(_humanize_overall("HEALTHY"), "all endpoints healthy")
        self.assertEqual(_humanize_overall("DEGRADED"), "some endpoints down")
        self.assertEqual(_humanize_overall("OUTAGE"), "main API endpoint down")
        self.assertEqual(_humanize_overall("UNREACHABLE"), "can't reach the API")

    def test_leak_suffix(self):
        self.assertIn("LiteSpeed admin telemetry leaking",
                     _humanize_overall("DEGRADED+LEAK"))
        self.assertIn("LiteSpeed", _humanize_overall("OUTAGE+LEAK"))

    def test_sentinel_diverged_suffix(self):
        self.assertIn("sentinel broken",
                     _humanize_overall("DEGRADED+SENTINEL-DIVERGED"))

    def test_unknown_base_returns_raw(self):
        self.assertEqual(_humanize_overall("MARS"), "MARS")


class TestHumanizeDeltaLine(unittest.TestCase):
    def test_ok_to_error_transition(self):
        line = "wdgwars.pl team-me/valid                        OK/200 -> ERROR/-"
        out = _humanize_delta_line(line)
        self.assertIn("team-me/valid", out)
        self.assertIn("was healthy", out)
        self.assertIn("timing out", out)

    def test_error_to_ok_recovery(self):
        line = "wdgwars.pl team-me/valid                        ERROR/- -> OK/200"
        out = _humanize_delta_line(line)
        self.assertIn("recovered", out)

    def test_ok_to_dead(self):
        line = "wdgwars.pl me/valid                             OK/200 -> DEAD/404"
        out = _humanize_delta_line(line)
        self.assertIn("route missing", out)

    def test_dead_to_ok(self):
        line = "wdgwars.pl me/valid                             DEAD/404 -> OK/200"
        out = _humanize_delta_line(line)
        self.assertIn("route restored", out)

    def test_ok_to_524_upstream_flap(self):
        line = "wdgwars.pl team-me/valid                        OK/200 -> 524/524"
        out = _humanize_delta_line(line)
        self.assertIn("CDN/origin", out)
        self.assertIn("524", out)

    def test_new_probe(self):
        line = "wdgwars.pl badge-catalog                        NEW -> OK/200"
        out = _humanize_delta_line(line)
        self.assertIn("badge-catalog", out)
        self.assertIn("new probe", out)
        self.assertIn("healthy", out)

    def test_gone_probe(self):
        line = "wdgwars.pl removed-probe                        GONE (was OK/200)"
        out = _humanize_delta_line(line)
        self.assertIn("removed-probe", out)
        self.assertIn("probe removed", out)

    def test_malformed_line_returns_raw(self):
        line = "this line has no arrow"
        self.assertEqual(_humanize_delta_line(line), line)


class TestHumanizeVerdictSummary(unittest.TestCase):
    def test_empty_dict(self):
        self.assertEqual(_humanize_verdict_summary({}), [])

    def test_singular_vs_plural(self):
        bullets = _humanize_verdict_summary({"OK": 1})
        self.assertEqual(bullets, ["1 endpoint healthy"])
        bullets = _humanize_verdict_summary({"OK": 2})
        self.assertEqual(bullets, ["2 endpoints healthy"])

    def test_priority_ordering(self):
        bullets = _humanize_verdict_summary({
            "SENTINEL": 3, "OK": 13, "ERROR": 2, "AUTH-REQUIRED": 27, "DEAD": 2,
        })
        joined = " | ".join(bullets)
        # OK before AUTH-REQUIRED before ERROR before DEAD before SENTINEL
        self.assertLess(joined.index("healthy"), joined.index("rejecting"))
        self.assertLess(joined.index("rejecting"), joined.index("timed out"))
        self.assertLess(joined.index("timed out"), joined.index("missing"))
        self.assertLess(joined.index("missing"), joined.index("background"))

    def test_leak_present(self):
        bullets = _humanize_verdict_summary({"LEAK": 1, "OK": 10})
        joined = " ".join(bullets)
        self.assertIn("LiteSpeed", joined)

    def test_extras_caught(self):
        bullets = _humanize_verdict_summary({"OK": 5, "500": 2, "400": 1})
        joined = " ".join(bullets)
        self.assertIn("HTTP 400", joined)
        self.assertIn("HTTP 500", joined)


class TestFormatWebhookPayloadHuman(unittest.TestCase):
    def test_regression_payload_is_human_readable(self):
        p = _format_webhook_payload(
            "HEALTHY", "DEGRADED",
            ["wdgwars.pl team-me/valid                        OK/200 -> ERROR/-",
             "wdgwars.pl team-id/valid                        OK/200 -> ERROR/-"],
            {"OK": 13, "AUTH-REQUIRED": 27, "ERROR": 2, "DEAD": 2},
        )
        content = p["content"]
        # The headline is plain English.
        self.assertIn("status changed", content)
        self.assertIn("some endpoints down", content)
        # The per-probe deltas are plain English.
        self.assertIn("was healthy", content)
        self.assertIn("timing out", content)
        # The summary bullets are plain English, NOT a Python dict.
        self.assertIn("13 endpoints healthy", content)
        self.assertIn("2 timed out", content)
        self.assertNotIn("by_verdict", content)
        self.assertNotIn("verdicts:", content)  # the old jargon line
        # Structured fields still present for tooling.
        self.assertEqual(p["overall"], "DEGRADED")
        self.assertEqual(p["by_verdict"]["OK"], 13)
        self.assertEqual(len(p["deltas_human"]), 2)
        self.assertIn("13 endpoints healthy", p["by_verdict_human"])

    def test_recovery_payload_is_human_readable(self):
        p = _format_webhook_payload(
            "DEGRADED", "HEALTHY",
            ["wdgwars.pl team-me/valid                        ERROR/- -> OK/200"],
            {"OK": 15, "AUTH-REQUIRED": 27},
        )
        self.assertIn("fully healthy again", p["content"])
        self.assertEqual(p["kind"], "recovery")

    def test_upstream_flap_payload(self):
        p = _format_webhook_payload(
            "DEGRADED", "DEGRADED",
            ["wdgwars.pl team-me/valid                        OK/200 -> 524/524",
             "wdgwars.pl team-id/valid                        OK/200 -> 524/524"],
            {"OK": 12, "AUTH-REQUIRED": 27, "DEAD": 2},
        )
        # Should still produce human prose, mention flap.
        self.assertIn("LOCOSP", p["content"])
        self.assertIn("CDN/origin", p["content"])

    def test_text_machine_preserved(self):
        p = _format_webhook_payload(
            "HEALTHY", "OUTAGE+LEAK", [],
            {"OK": 1, "DEAD": 10, "LEAK": 1},
        )
        # Backward-compat surface: tooling that parsed the old `text` reads
        # `text_machine` now.
        self.assertIn("OUTAGE+LEAK", p["text_machine"])
        self.assertIn("verdicts:", p["text_machine"])


if __name__ == "__main__":
    unittest.main()
