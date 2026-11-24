"""SVG chart writer (ported from evsim tests/test_cost.py)."""
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gridq.svgchart import chart, heat_bucket, linear_ticks, log_ticks, matrix  # noqa: E402


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

    def test_reference_label_in_legend_only(self):
        panel = {"title": "", "series": [{"label": "s", "slot": 1, "points": [(0, 0.2, "t0"), (1, 0.4, "t1")]}],
                 "refs": [{"label": "plan<40%>", "points": [(0.4, 0.0), (0.4, 1.0)], "label_at": "none"}]}
        root = ET.fromstring(chart([panel], title="T", legend=[("s", 1, "line"), ("plan<40%>", 0, "ref")]))
        ns = "{http://www.w3.org/2000/svg}"
        shown = [e.text for e in root.iter(ns + "text")]
        self.assertEqual(shown.count("plan<40%>"), 1)                   # the legend entry, no label at the line
        self.assertIn("plan<40%>", [e.text for e in root.iter(ns + "title")])   # the line keeps its tooltip

    def test_heat_table(self):
        self.assertEqual([heat_bucket(v) for v in (0.0, 0.001, 1 / 6, 0.5, 1.0)], [0, 1, 1, 3, 6])
        svg = matrix(["a", "b"], ["x", "y\nz"], [[0.0, 1.0], [0.5, None]], title="M", subtitle="s1\ns2",
                     tips=[["t<1>", "t2"], ["t3", "t4"]], text_cols=[("note", ["n1", "n2"])], breaks=[0],
                     heat_header="share", legend_label="share")
        root = ET.fromstring(svg)
        ns = "{http://www.w3.org/2000/svg}"
        cells = [e for e in root.iter(ns + "rect") if "cell" in e.get("class", "").split()]
        self.assertEqual([c.get("class") for c in cells], ["cell h0", "cell h6", "cell h3", "cell h0"])
        shown = [e.text for e in root.iter(ns + "text") if (e.get("class") or "").startswith("cv")]
        self.assertEqual(shown, ["0%", "100%", "50%", "—"])                 # nothing to measure is not 0%
        self.assertIn("t<1>", [e.text for e in root.iter(ns + "title")])
        self.assertIn("prefers-color-scheme: dark", svg)


if __name__ == "__main__":
    unittest.main()
