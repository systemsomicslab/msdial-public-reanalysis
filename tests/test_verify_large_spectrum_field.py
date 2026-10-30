"""The gate reads an MS-DIAL export whose MS/MS spectrum field is longer than Python's default csv field limit.

V5 (v5-mpst48-20261029): 9 of the 19,623 rows of a ZenoTOF 7600 .mdpeak of MB-POST MPST000048 carried a spectrum
of 136,199-270,713 characters, past the default limit of 131,072. Each script is loaded in a fresh interpreter,
so no other module of the suite has raised the limit already.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent("""
    import csv, importlib.util, sys
    spec = importlib.util.spec_from_file_location("probe", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe"] = module
    spec.loader.exec_module(module)
    print(csv.field_size_limit())
    if hasattr(module, "_tsv_rows"):
        rows, why = module._tsv_rows(__import__("pathlib").Path(sys.argv[2]))
        print(len(rows) if rows is not None else why)
""")


class LargeSpectrumFieldTests(unittest.TestCase):
    def probe(self, script: str, table: Path) -> list[str]:
        result = subprocess.run([sys.executable, "-c", PROBE, str(_ROOT / "scripts" / script), str(table)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_the_gate_reads_a_peak_table_with_a_spectrum_past_the_default_limit(self) -> None:
        spectrum = " ".join(f"{100 + index * 0.0001:.4f}:{index % 997}" for index in range(24000))
        self.assertGreater(len(spectrum), 270_713)
        with tempfile.TemporaryDirectory() as folder:
            table = Path(folder) / "sample.mdpeak"
            table.write_text("Peak ID\tName\tMSMS spectrum\n0\tUnknown\t100.0000:1\n1\tUnknown\t" + spectrum + "\n",
                             encoding="utf-8")
            limit, rows = self.probe("verify-run-invariants.py", table)
        self.assertGreaterEqual(int(limit), 2**31 - 1)
        self.assertEqual(rows, "2")

    def test_the_concordance_report_raises_the_limit_too(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (limit,) = self.probe("report-insilico-concordance.py", Path(folder) / "none.tsv")
        self.assertGreaterEqual(int(limit), 2**31 - 1)


if __name__ == "__main__":
    unittest.main()
