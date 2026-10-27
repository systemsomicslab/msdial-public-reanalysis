"""Only approve makes a campaign's ledger (the defect of 2026-10-15).

`verify-env --campaign X` run before `approve` opened the ledger, which created an empty ledger.sqlite (the schema
and its first rows, no campaign, no approval, no unit); `approve` then refused with "Campaign X already has a ledger
and an approval.", and the scheduled task's `run`, restarted on failure every five minutes, made the empty ledger
again each time. Every command but approve now reads the ledger's state first and opens nothing that is absent or
empty: it says the campaign has no approval yet and exits 2, creating and writing nothing. approve takes an empty
ledger as absent (tests/test_campaign_plan.py approves into one end to end).
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402
from campaign import ledger  # noqa: E402

CAMPAIGN = "c-unapproved"
# Every command that reads a campaign's ledger, with the arguments it needs; approve and plan are tested apart.
COMMANDS = {
    "run": ["run", "--until-idle"],
    "verify-env": ["verify-env"],
    "status": ["status"],
    "export": ["export", "--out", "{root}/status.json"],
    "pause": ["pause", "--reason", "why"],
    "resume": ["resume"],
    "skip": ["skip", "--unit", "u1", "--reason", "why"],
    "retry": ["retry", "--unit", "u1", "--reason", "why"],
    "release-held": ["release-held", "--unit", "u1", "--reason", "why"],
    "recheck-held": ["recheck-held"],
    "revoke": ["revoke", "--by", "Test Person", "--reason", "why"],
}


def _cli():
    spec = importlib.util.spec_from_file_location("campaign_runner_cli_unapproved", TESTS.parent / "scripts" / "campaign-runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UnapprovedCampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = _cli()

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace_root = self.root / "analysis"
        # A planned campaign: its manifest and the plan's summary, and no approval yet.
        self.directory = self.workspace_root / "_campaigns" / CAMPAIGN
        self.directory.mkdir(parents=True)
        (self.directory / "campaign-manifest.json").write_text("{}", encoding="utf-8")
        (self.directory / "campaign-manifest.summary.json").write_text("{}", encoding="utf-8")
        self.ledger_path = self.directory / "ledger.sqlite"
        # Roots that hold no Interactive or Catalog: none of these commands may need either to refuse.
        self.interactive_root = self.root / "no-interactive"
        self.catalog_root = self.root / "no-catalog"

    def main(self, command: str, *extra: str, workspace_root: Path | None = None, campaign: str = CAMPAIGN,
             arguments: list[str] | None = None) -> tuple[int, str, str]:
        arguments = arguments or [item.format(root=self.root) for item in COMMANDS[command]]
        argv = ["--workspace-root", str(workspace_root or self.workspace_root), "--interactive-root",
                str(self.interactive_root), "--catalog-root", str(self.catalog_root), arguments[0], "--campaign",
                campaign, *arguments[1:], *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.cli.main(argv)
            finally:
                # main points both streams at --log-file's stream when it is given; the redirections restore them,
                # and the log is closed here so that the temporary folder can go.
                logged = sys.stdout
        if logged is not out:
            logged.close()
        return code, out.getvalue(), err.getvalue()

    def listing(self) -> list[str]:
        return sorted(str(path.relative_to(self.directory)) for path in self.directory.rglob("*"))

    def test_verify_env_and_run_before_approve_create_no_ledger(self) -> None:
        before = self.listing()
        for command in ("verify-env", "run"):
            with self.subTest(command):
                code, out, err = self.main(command)
                self.assertEqual(code, self.cli.EXIT_REFUSED, err)
                self.assertFalse(self.ledger_path.exists(), "no ledger is made")
                self.assertEqual(self.listing(), before, "nothing at all is made in the campaign directory")
                self.assertIn(f"Campaign {CAMPAIGN} has no approval yet", err)
                self.assertIn("there is no ledger at", err)
                self.assertIn(f"approve it first (campaign-runner.py approve --campaign {CAMPAIGN}", err)
                self.assertIn("Nothing was created.", err)
                self.assertEqual(out, "")

    def test_the_scheduled_tasks_run_writes_its_refusal_to_its_log_and_makes_no_ledger(self) -> None:
        """As the task starts it, with --log-file under the campaign's logs folder: the log says why, and the ledger
        is not made again at each restart."""
        log = self.directory / "logs" / "runner.log"
        for _restart in range(3):
            code, _out, _err = self.main("run", "--log-file", str(log))
            self.assertEqual(code, self.cli.EXIT_REFUSED)
        self.assertFalse(self.ledger_path.exists())
        text = log.read_text(encoding="utf-8")
        self.assertEqual(text.count(f"Campaign {CAMPAIGN} has no approval yet"), 3)

    def test_every_command_that_reads_the_ledger_refuses_without_making_one(self) -> None:
        before = self.listing()
        for command in COMMANDS:
            with self.subTest(command):
                code, _out, err = self.main(command)
                self.assertEqual(code, self.cli.EXIT_REFUSED, err)
                self.assertIn("has no approval yet", err)
                self.assertFalse(self.ledger_path.exists())
                self.assertEqual(self.listing(), before)
        self.assertFalse((self.root / "status.json").exists(), "export wrote nothing")

    def test_an_empty_ledger_an_earlier_runner_made_is_refused_and_left_as_it_is(self) -> None:
        ledger.Ledger(self.ledger_path).close()
        self.assertEqual(ledger.ledger_state(self.ledger_path)["state"], ledger.LEDGER_EMPTY)
        digest = _sha256(self.ledger_path)
        for command in COMMANDS:
            with self.subTest(command):
                code, _out, err = self.main(command)
                self.assertEqual(code, self.cli.EXIT_REFUSED, err)
                self.assertIn("has no approval yet", err)
                self.assertIn("holds no campaign, no approval and no unit", err)
                self.assertIn("which records the approval into it", err)
                self.assertEqual(ledger.ledger_state(self.ledger_path)["state"], ledger.LEDGER_EMPTY)
                self.assertEqual(_sha256(self.ledger_path), digest, "nothing is written to it")
        self.assertEqual(sorted(path.name for path in self.directory.iterdir() if path.name.startswith("ledger")),
                         ["ledger.sqlite"], "no -wal or -shm file is left beside it")

    def test_an_approved_ledger_is_still_opened(self) -> None:
        world = fakes.World(self.root / "approved", ["u1"])
        code, out, err = self.main("pause", workspace_root=world.workspace_root, campaign="test-campaign")
        self.assertEqual(code, self.cli.EXIT_OK, err)
        self.assertIn("Paused", out)
        code, out, err = self.main("status", workspace_root=world.workspace_root, campaign="test-campaign")
        self.assertEqual(code, self.cli.EXIT_OK, err)
        self.assertIn('"pending": 1', out)
        with world.open() as book:
            self.assertEqual(book.runner()["pause_kind"], "operator")


class LedgerStateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_absent_empty_held_and_unreadable(self) -> None:
        path = self.root / "a folder #1" / "ledger.sqlite"
        self.assertEqual(ledger.ledger_state(path), {"state": ledger.LEDGER_ABSENT, "rows": {}, "detail": ""})
        self.assertFalse(path.exists(), "reading the state makes no file")
        self.assertFalse(path.parent.exists(), "nor its folder")
        path.parent.mkdir()
        ledger.Ledger(path).close()
        self.assertEqual(ledger.ledger_state(path)["state"], ledger.LEDGER_EMPTY)
        with ledger.Ledger(path) as book:
            # Read while another connection holds it, as a runner's would.
            self.assertEqual(ledger.ledger_state(path)["rows"], {"campaign": 0, "approval": 0, "unit": 0})
            book.connection.execute(
                "INSERT INTO unit(unit_key, catalog_unit_id, repository, accession, order_index, state, state_since, "
                "updated_at) VALUES ('u1', 'u1', 'r', 'a', 0, 'pending', 'now', 'now')")
        found = ledger.ledger_state(path)
        self.assertEqual((found["state"], found["rows"]), (ledger.LEDGER_HELD, {"campaign": 0, "approval": 0, "unit": 1}))
        self.assertEqual(ledger.describe_rows(found["rows"]), "0 campaigns, 0 approvals and 1 unit")
        world = fakes.World(self.root / "w", ["u1", "u2"])
        found = ledger.ledger_state(world.ledger_path)
        self.assertEqual((found["state"], found["rows"]), (ledger.LEDGER_HELD, {"campaign": 1, "approval": 1, "unit": 2}))
        broken = self.root / "broken.sqlite"
        broken.write_bytes(b"not a database " * 100)
        found = ledger.ledger_state(broken)
        self.assertEqual(found["state"], ledger.LEDGER_UNREADABLE)
        self.assertIn("not a database", found["detail"])

    def test_create_records_a_campaign_only_into_an_empty_ledger(self) -> None:
        world = fakes.World(self.root / "w", ["u1"])
        path = self.root / "units-only.sqlite"
        with ledger.Ledger(path) as book:
            book.connection.execute(
                "INSERT INTO unit(unit_key, catalog_unit_id, repository, accession, order_index, state, state_since, "
                "updated_at) VALUES ('stray', 'stray', 'r', 'a', 0, 'pending', 'now', 'now')")
            with self.assertRaisesRegex(ledger.LedgerError, "already holds 0 campaigns, 0 approvals and 1 unit"):
                book.create(world.campaign, world.approval, world.units, world.groups, "2026-10-15T00:00:00+00:00")
            self.assertIsNone(book.connection.execute("SELECT * FROM campaign").fetchone(), "nothing was recorded")
        with ledger.Ledger(world.ledger_path) as book, self.assertRaisesRegex(ledger.LedgerError, "already holds 1 campaign"):
            book.create(world.campaign, world.approval, world.units, world.groups, "2026-10-15T00:00:00+00:00")
        empty = self.root / "empty.sqlite"
        ledger.Ledger(empty).close()
        with ledger.Ledger(empty) as book:
            book.create(world.campaign, world.approval, world.units, world.groups, "2026-10-15T00:00:00+00:00")
            self.assertEqual(book.campaign()["campaign_id"], "test-campaign")

    def test_recorded_authorization_reads_the_campaigns_record_and_makes_no_ledger(self) -> None:
        """What approve reads after an error to tell whether its approval was committed (review of 2026-10-15)."""
        path = self.root / "none" / "ledger.sqlite"
        self.assertEqual(ledger.recorded_authorization(path), "")
        self.assertFalse(path.parent.exists(), "reading makes no file, nor its folder")
        path.parent.mkdir()
        ledger.Ledger(path).close()
        self.assertEqual(ledger.recorded_authorization(path), "", "an empty ledger records no approval")
        world = fakes.World(self.root / "w", ["u1"])
        self.assertEqual(ledger.recorded_authorization(world.ledger_path), world.campaign["authorization_sha256"])
        broken = self.root / "broken.sqlite"
        broken.write_bytes(b"not a database " * 100)
        with self.assertRaises(sqlite3.DatabaseError):
            ledger.recorded_authorization(broken)

    def test_create_records_its_events_in_its_own_transaction(self) -> None:
        """approve's empty_ledger_approved goes into the ledger with the approval or not at all (review of
        2026-10-15): a commit that fails loses both."""
        world = fakes.World(self.root / "w", ["u1"])
        path = self.root / "empty.sqlite"
        ledger.Ledger(path).close()
        events = [("empty_ledger_approved", {"approval_id": "approval-1"})]

        def hook(at: str) -> None:
            if at == "before":
                raise sqlite3.OperationalError("disk I/O error")

        with ledger.Ledger(path, commit_hook=hook) as book, self.assertRaises(sqlite3.OperationalError):
            book.create(world.campaign, world.approval, world.units, world.groups, "2026-10-15T00:00:00+00:00",
                        events=events)
        self.assertEqual(ledger.ledger_state(path)["state"], ledger.LEDGER_EMPTY)
        with ledger.Ledger(path) as book:
            self.assertEqual(book.events(), [])
            book.create(world.campaign, world.approval, world.units, world.groups, "2026-10-15T00:00:00+00:00",
                        events=events)
            self.assertEqual([(row["at"], row["unit_key"], json.loads(row["detail_json"]))
                              for row in book.events("empty_ledger_approved")],
                             [("2026-10-15T00:00:00+00:00", None, {"approval_id": "approval-1"})])


if __name__ == "__main__":
    unittest.main()
