"""scripts/check-release-redaction.py: what a release bundle must never hold (docs/llm-assisted-curation.md 5.3).

Every rule is made to fire once on a synthetic bundle, and what the release projection writes in place of a path
(relative paths, <external>/<name>, <library:<name> sha256=...>) and a URL must not fire. The release command
(curated.write_release, campaign-runner.py release) applies this script and refuses a bundle with a finding. Synthetic
names only; the paths below are made up and name no machine.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "scripts"))
from campaign import curated  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("check_release_redaction_under_test", ROOT / "scripts" / "check-release-redaction.py")
assert _SPEC and _SPEC.loader
crr = importlib.util.module_from_spec(_SPEC)
sys.modules["check_release_redaction_under_test"] = crr
_SPEC.loader.exec_module(crr)

SHA = "a" * 64


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self._tmp.name) / "bundle"
        self.bundle.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def put(self, relative: str, text: str | bytes) -> Path:
        path = self.bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def rules(self, **options) -> list[tuple[str, str]]:
        return [(item["file"], item["rule"]) for item in crr.check_bundle(self.bundle, **options)]


class PathRuleTests(_Case):
    def test_a_clean_bundle_passes(self) -> None:
        self.put("release-index.json", json.dumps({"schema": "msdial-release-index.v1", "manifests": []}))
        self.put("units/AN000001/run-manifest.release.json", json.dumps({
            "raw": "raw/S01.mzML", "outside": "<external>/files.json",
            "library": f"<library:Synthetic-Private-Lib.msp sha256={SHA}>", "lbm": f"<library:Lab.lbm2 sha256={SHA}>",
            "source": "https://www.metabolomicsworkbench.org/data/ST000001_rawdata.zip",
            "official": "https://example.org/libraries/Synthetic-Public-Lib.msp", "time": "12:30:00", "ratio": "a:b/c"}))
        self.assertEqual(self.rules(), [])

    def test_drive_letter_paths_as_text_and_as_json_escapes(self) -> None:
        self.put("a.txt", "written to D:/analysis/out\n")
        self.put("b.json", json.dumps({"path": "E:\\data\\x.raw"}))
        self.put("c.tsv", "file\tF:\\y.mzML\n")
        self.assertEqual(self.rules(), [("a.txt", "drive_letter_path"), ("b.json", "drive_letter_path"),
                                        ("c.tsv", "drive_letter_path")])

    def test_unc_paths(self) -> None:
        self.put("a.txt", "copied from \\\\nas01\\share\\libraries\n")
        self.put("b.json", json.dumps({"from": "\\\\nas01\\share"}))
        self.assertEqual(self.rules(), [("a.txt", "unc_path"), ("b.json", "unc_path")])

    def test_user_profile_paths(self) -> None:
        self.put("a.log", "read /home/someone/.config/x\n")
        self.put("b.json", json.dumps({"p": "/Users/someone/Desktop/y"}))
        self.assertEqual(self.rules(), [("a.log", "user_profile_path"), ("b.json", "user_profile_path")])

    def test_each_rule_is_reported_once_per_file_with_its_line(self) -> None:
        self.put("a.txt", "ok\nD:/one\nE:/two\n")
        findings = crr.check_bundle(self.bundle)
        self.assertEqual([(item["rule"], item["line"]) for item in findings], [("drive_letter_path", 2)])
        self.assertEqual(findings[0]["at"], 3)

    def test_a_match_across_a_chunk_boundary_and_a_later_line_are_found(self) -> None:
        old = crr.CHUNK, crr.OVERLAP
        crr.CHUNK, crr.OVERLAP = 64, 16
        try:
            self.put("big.txt", "x" * 60 + "\n" + "y" * 50 + "\nZ:\\boundary\n")
            findings = crr.check_bundle(self.bundle)
        finally:
            crr.CHUNK, crr.OVERLAP = old
        self.assertEqual([(item["rule"], item["line"], item["at"]) for item in findings], [("drive_letter_path", 3, 112)])

    def test_a_utf16_file_is_read(self) -> None:
        self.put("utf16.txt", "\ufeffout at G:\\results\n".encode("utf-16-le"))
        self.assertEqual(self.rules(), [("utf16.txt", "drive_letter_path")])

    def test_holds_absolute_path_is_the_three_path_rules(self) -> None:
        self.assertTrue(crr.holds_absolute_path(b'{"p": "C:\\\\x"}'))
        self.assertTrue(crr.holds_absolute_path(b"\\\\host\\share"))
        self.assertFalse(crr.holds_absolute_path(b"https://example.org/a.msp <external>/b.json"))


class LibraryRuleTests(_Case):
    def test_a_library_file_with_a_path_is_refused(self) -> None:
        self.put("a.json", json.dumps({"msp": "libraries/Synthetic-Private-Lib.msp"}))
        self.put("b.txt", "lbm=lab\\Lab_lipids.lbm2\n")
        self.assertEqual(self.rules(), [("a.json", "library_path"), ("b.txt", "library_path")])

    def test_a_library_by_name_external_or_url_is_not(self) -> None:
        self.put("a.json", json.dumps({"msp": "Synthetic-Private-Lib.msp", "lbm": "<external>/Lab_lipids.lbm2",
                                       "pinned": f"<library:Lab_lipids.lbm2 sha256={SHA}>",
                                       "url": "https://example.org/x/Synthetic-Public-Lib.msp"}))
        self.assertEqual(self.rules(), [])

    def test_a_pinned_or_named_library_of_any_extension_is_refused_with_a_path(self) -> None:
        self.put("index.json", json.dumps({"pinned": f"<library:Lab-RT-table.txt sha256={SHA}>"}))
        self.put("a.json", json.dumps({"rt": "tables/Lab-RT-table.txt"}))
        self.put("b.json", json.dumps({"is": "standards/Lab-IS.csv"}))
        self.assertEqual(self.rules(), [("a.json", "library_path")])
        self.assertEqual(self.rules(library_names=["Lab-IS.csv"]), [("a.json", "library_path"), ("b.json", "library_path")])


class TranscriptRuleTests(_Case):
    BLOCKS = (
        "Contents of /home/someone/.claude/CLAUDE.md (user's private global instructions for all projects):",
        "Codebase and user instructions are shown below. Be sure to adhere to these instructions.",
        "Contents of x/memory/MEMORY.md (user's auto-memory, persists across conversations):",
        "# claudeMd",
    )

    def test_a_context_or_memory_block_in_a_transcript_is_refused(self) -> None:
        for number, block in enumerate(self.BLOCKS):
            line = json.dumps({"type": "user", "message": {"content": "<system-reminder>\n" + block + "\n</system-reminder>"}})
            self.put(f"transcripts/AN00000{number}.redacted.jsonl", line + "\n")
        found = [rule for _file, rule in self.rules() if rule == "context_block"]
        self.assertEqual(len(found), len(self.BLOCKS), self.rules())

    def test_only_transcripts_are_read_for_context_blocks(self) -> None:
        self.put("docs/note.md", "Codebase and user instructions are shown below.\n")
        self.put("triage/ST000001/session-transcript.jsonl", '{"text": "Codebase and user instructions are shown below."}\n')
        self.assertEqual(self.rules(), [("triage/ST000001/session-transcript.jsonl", "context_block")])
        self.assertTrue(crr.is_transcript("transcripts/AN000001.jsonl"))
        self.assertTrue(crr.is_transcript("x/AN000001.redacted.jsonl"))
        self.assertFalse(crr.is_transcript("decision-ledger.jsonl"))

    def test_a_clean_transcript_passes(self) -> None:
        lines = [{"type": "system", "subtype": "init", "tools": ["Read"], "mcp_servers": [{"name": "msdial-repository-catalog"}]},
                 {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read",
                                                                 "input": {"file_path": "../inputs/AN000001/handoff.json"}}]}}]
        self.put("transcripts/AN000001.redacted.jsonl", "".join(json.dumps(line) + "\n" for line in lines))
        self.assertEqual(self.rules(), [])


class CommandLineTests(_Case):
    def test_exit_status_and_report(self) -> None:
        self.put("ok.json", "{}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(crr.main([str(self.bundle), "--json"]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["tool"], report["version"], report["verdict"], report["files"]),
                         ("check-release-redaction.py", "v1", "pass", 1))
        self.assertEqual(report["script_sha256"], crr.script_sha256())
        self.put("bad.json", json.dumps({"p": "C:\\x"}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(crr.main([str(self.bundle)]), 1)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(crr.main([str(self.bundle / "missing")]), 2)


class ReleaseCommandTests(_Case):
    """The release command applies this script: curated.release_redaction_findings is its check, and write_release
    refuses a bundle with a finding of any rule, not only the three path rules it had before."""

    def campaign(self, ledger_detail: dict | None = None) -> Path:
        root = Path(self._tmp.name)
        campaign = root / "campaign"
        (campaign / "triage" / "ST000001").mkdir(parents=True)
        (campaign / "triage" / "ST000001" / "AN000001.json").write_text("{}", encoding="utf-8")
        (campaign / "campaign-manifest.json").write_text(
            json.dumps({"units": [], "triage": {"directory": str(campaign / "triage")}}), encoding="utf-8")
        if ledger_detail is not None:
            (campaign / curated.DECISION_LEDGER).write_text(json.dumps({"detail": ledger_detail}) + "\n", encoding="utf-8")
        return campaign

    def test_the_curated_check_is_the_scripts(self) -> None:
        self.put("a.json", json.dumps({"msp": "libraries/Synthetic-Private-Lib.msp", "p": "D:/x"}))
        self.assertIs(curated.redaction_check().check_bundle.__code__.co_filename.endswith("check-release-redaction.py"), True)
        self.assertEqual(curated.release_redaction_findings(self.bundle), crr.check_bundle(self.bundle))

    def test_a_bundle_with_a_library_path_is_refused_and_gets_no_index(self) -> None:
        root = Path(self._tmp.name)
        campaign = self.campaign({"library": "libs/Synthetic-Private-Lib.msp"})
        with self.assertRaisesRegex(curated.TriageError, "library_path"):
            curated.write_release(campaign_dir=campaign, out=root / "out", workspace_root=root / "analysis",
                                  approved_digest="sha256:" + "0" * 64)
        self.assertFalse((root / "out" / curated.RELEASE_INDEX).exists())
        campaign2 = Path(self._tmp.name) / "campaign"
        (campaign2 / curated.DECISION_LEDGER).write_text(json.dumps({"detail": {"library": "Synthetic-Private-Lib.msp"}}) + "\n",
                                                         encoding="utf-8")
        index = curated.write_release(campaign_dir=campaign2, out=root / "out2", workspace_root=root / "analysis",
                                      approved_digest="sha256:" + "0" * 64)
        self.assertEqual(index["schema"], curated.RELEASE_INDEX_SCHEMA)

    def test_a_pinned_library_name_is_checked_whatever_its_extension(self) -> None:
        root = Path(self._tmp.name)
        campaign = self.campaign({"rt": "tables/Lab-RT-table.txt"})
        with self.assertRaisesRegex(curated.TriageError, "library_path"):
            curated.write_release(campaign_dir=campaign, out=root / "out", workspace_root=root / "analysis",
                                  approved_digest="sha256:" + "0" * 64, libraries={"Lab-RT-table.txt": SHA})


class DocumentTests(unittest.TestCase):
    def test_section_5_3_names_every_rule_and_the_version(self) -> None:
        text = (ROOT / "docs" / "llm-assisted-curation.md").read_text(encoding="utf-8")
        section = text[text.index("### 5.3"):text.index("## 6.")]
        for rule in [name for name, _pattern in crr.PATH_RULES] + ["library_path", "context_block"]:
            self.assertIn(f"`{rule}`", section, rule)
        self.assertIn(f"`{crr.TOOL} {crr.VERSION}`", section)
        self.assertNotIn("still to be built", section)


if __name__ == "__main__":
    unittest.main()
