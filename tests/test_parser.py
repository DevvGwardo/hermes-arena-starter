"""
Tolerant-JSON parser tests for hermes_parse.py.

Pins behavior across the full set of malformed, fenced, prose-padded, and
truncated outputs we've seen real LLMs produce. Run with:

    python -m unittest tests.test_parser -v

Or with the existing project convention:

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import pathlib
import sys
import unittest

# Make hermes_parse.py importable from this tests/ dir.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hermes_parse import extract_json_object, safe_json_parse  # noqa: E402


class ExtractJsonObjectTests(unittest.TestCase):
    def test_balances_braces(self):
        self.assertEqual(extract_json_object('{"a":1}'), '{"a":1}')

    def test_balances_brackets_for_top_level_array(self):
        self.assertEqual(
            extract_json_object('[{"a":1},{"b":2}]'),
            '[{"a":1},{"b":2}]',
        )

    def test_strips_json_fences(self):
        fenced = '```json\n{"decisions":[{"symbol":"BTC","action":"FLAT"}]}\n```'
        self.assertEqual(
            extract_json_object(fenced),
            '{"decisions":[{"symbol":"BTC","action":"FLAT"}]}',
        )

    def test_strips_bare_fences(self):
        self.assertEqual(extract_json_object("```\n{\"a\":1}\n```"), '{"a":1}')

    def test_first_balanced_object_when_prose_follows(self):
        self.assertEqual(
            extract_json_object('Here is JSON: {"a":1} hope this helps'),
            '{"a":1}',
        )

    def test_first_balanced_array_when_prose_follows(self):
        self.assertEqual(extract_json_object("Result: [1,2,3] thanks"), "[1,2,3]")

    def test_picks_whichever_opener_is_first(self):
        self.assertEqual(extract_json_object('[{"a":1}]'), '[{"a":1}]')
        self.assertEqual(extract_json_object('{"a":[1,2]}'), '{"a":[1,2]}')

    def test_does_not_break_on_quoted_braces(self):
        self.assertEqual(extract_json_object('{"a":"}{"}'), '{"a":"}{"}')

    def test_returns_trimmed_input_when_no_delimiter(self):
        self.assertEqual(extract_json_object("  no json here  "), "no json here")


class SafeJsonParseTests(unittest.TestCase):
    """The decision path in agent.py and the narration path in agent_v2.py
    both feed model output through safe_json_parse. Each of these cases
    mirrors something a real LLM has produced in practice."""

    def test_canonical_decisions_envelope(self):
        text = '{"decisions":[{"symbol":"BTC","action":"LONG","positionSizePercent":10,"reason":"r"}]}'
        out = safe_json_parse(text)
        self.assertIsInstance(out, dict)
        self.assertEqual(len(out["decisions"]), 1)

    def test_top_level_array(self):
        text = '[{"symbol":"ETH","action":"SHORT","positionSizePercent":5,"reason":"x"}]'
        out = safe_json_parse(text)
        self.assertIsInstance(out, list)
        self.assertEqual(out[0]["symbol"], "ETH")

    def test_json_fenced_output(self):
        text = (
            '```json\n{"decisions":[{"symbol":"BTC","action":"FLAT",'
            '"positionSizePercent":0,"reason":"r"}]}\n```'
        )
        out = safe_json_parse(text)
        self.assertEqual(out["decisions"][0]["action"], "FLAT")

    def test_prose_padded_output(self):
        text = (
            'Here is the JSON: {"decisions":[{"symbol":"BTC","action":"LONG",'
            '"positionSizePercent":10,"reason":"r"}]} — let me know if you need anything else.'
        )
        out = safe_json_parse(text)
        self.assertEqual(out["decisions"][0]["symbol"], "BTC")

    def test_repairs_trailing_commas(self):
        text = (
            '{"decisions":[{"symbol":"BTC","action":"LONG",'
            '"positionSizePercent":10,"reason":"r"},]}'
        )
        out = safe_json_parse(text)
        self.assertEqual(len(out["decisions"]), 1)

    def test_repairs_unquoted_keys(self):
        text = (
            '{decisions:[{symbol:"BTC",action:"LONG",'
            'positionSizePercent:10,reason:"r"}]}'
        )
        out = safe_json_parse(text)
        self.assertEqual(out["decisions"][0]["symbol"], "BTC")

    def test_repairs_single_quotes(self):
        text = (
            "{'decisions':[{'symbol':'BTC','action':'LONG',"
            "'positionSizePercent':10,'reason':'r'}]}"
        )
        out = safe_json_parse(text)
        self.assertEqual(out["decisions"][0]["action"], "LONG")

    def test_repairs_truncated_json(self):
        # Model hit max_tokens mid-array. json-repair closes the structure.
        text = (
            '{"decisions":[{"symbol":"BTC","action":"LONG",'
            '"positionSizePercent":10,"reason":"r"}'
        )
        out = safe_json_parse(text)
        self.assertIsNotNone(out)
        self.assertEqual(out["decisions"][0]["symbol"], "BTC")

    def test_returns_none_for_total_garbage(self):
        self.assertIsNone(safe_json_parse("this is not json at all"))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(safe_json_parse(""))

    def test_handles_narration_v2_envelope(self):
        # Shape produced for agent_v2.narrate_reasons.
        text = '{"reasons":["sharp move on BTC", "fading the rally"]}'
        out = safe_json_parse(text)
        self.assertEqual(out["reasons"], ["sharp move on BTC", "fading the rally"])


if __name__ == "__main__":
    unittest.main()
