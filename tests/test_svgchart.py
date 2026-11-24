"""SVG chart writer (ported from evsim tests/test_cost.py)."""
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.svgchart import chart, linear_ticks, log_ticks  # noqa: E402


class TestSvgChart(unittest.TestCase):
    def test_ticks(self):
        self.assertEqual(log_ticks(10, 1e4), [10, 100, 1000, 10000])
        self.assertEqual(linear_ticks(0, 10), [0, 2, 4, 6, 8, 10])

    def test_well_formed_with_tooltips_and_dark_mode(self):
        svg = chart([{"title": "a", "series": [{"label": "s", "slot": 1, "points": [(0, 10, "t<0>"), (1, 100, "t1")],
                                                "band": [(0, 5, 20), (1, 50, 200)], "end_label": "S"}],
                      "refs": [{"label": "r", "points": [(0, 365), (1, 365)]}]}],
                    title="T", subtitle="x\ny", legend=[("s", 1, "line")], ylog=True)
        root = ET.fromstring(svg)
        ns = "{http://www.w3.org/2000/svg}"
        tips = [e.text for e in root.iter(ns + "title")]
        self.assertIn("t<0>", tips)                                     # escaped, then parsed back
        self.assertIn("prefers-color-scheme: dark", svg)
        self.assertNotIn("nan", svg.lower().replace("nanum", ""))


if __name__ == "__main__":
    unittest.main()
