"""A download several units share is fetched once (2026-10-24): the runner claims it for every sibling first.

Interactive's lease fetches through the accession's download store, whose gc deletes an object once no live claim
keeps it, and only a pending claim keeps one for a unit that has not started. The runner made none, so the first
unit's cleanup let the store collect the object its siblings were about to fetch, and each fetched it again:
MPST000012's four units fetched one 14.78 GB tar four times while the approved summary said "fetched once". These
tests drive the state machine against the fake store in campaign_fakes (claims, objects, fetches and the gc, as the
real store keeps them); test_campaign_contract.py holds the ports to the real store and Catalog. Nothing here
downloads a byte or starts a Console.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402
import test_campaign_resume as resume  # noqa: E402
from campaign import ledger, machine  # noqa: E402

GROUP = "group-shared"
URL = f"https://repository.example/download/{GROUP}.tar"


def claim_states(world: fakes.World) -> dict[str, tuple]:
    """Each unit's claim on the shared object: (state, who made it, the reason it was released with)."""
    return {unit: (claim["state"], claim["source"], claim["reason"])
            for (url, unit), claim in sorted(world.interactive.claims.items()) if url == URL}


class Base(unittest.TestCase):
    def world(self, units=("u1", "u2", "u3"), shared=None, **options) -> fakes.World:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        groups = {GROUP: list(shared if shared is not None else units)}
        return fakes.World(Path(self.directory.name), list(units), download_groups=groups, **options)

    def finish(self, world: fakes.World, **options) -> ledger.Ledger:
        world.run(**options)
        book = world.open()
        self.addCleanup(book.close)
        return book

    @staticmethod
    def calls(world: fakes.World, name: str) -> list[dict]:
        return [arguments for called, arguments in world.interactive.calls if called == name]

    @staticmethod
    def events(book: ledger.Ledger, kind: str) -> list[tuple[str, dict]]:
        return [(row["unit_key"], json.loads(row["detail_json"])) for row in book.events(kind)]


class SharedDownloadTests(Base):
    def test_siblings_fetch_the_object_they_share_once(self) -> None:
        world = self.world()
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["done"] * 3)
        self.assertEqual(world.interactive.fetches, [("u1", URL)], "one fetch for the group")
        # Before each unit of the group downloads, every unit of it still to download is claimed; none after the last.
        self.assertEqual([call["units"] for call in self.calls(world, "pre_claim")], [["u2", "u3"], ["u3"]])
        self.assertEqual(world.catalog.object_requests, [["u2", "u3"], ["u3"]])
        self.assertEqual([unit for unit, _detail in self.events(book, machine.SHARED_DOWNLOAD_CLAIMED)], ["u1", "u2"])
        # Each cleanup released its unit's claim, and the last one let the store collect the object.
        self.assertEqual(claim_states(world), {unit: ("released", source, "raw_cleaned") for unit, source in
                                               (("u1", "lease"), ("u2", "batch_plan"), ("u3", "batch_plan"))})
        self.assertEqual((sorted(world.interactive.objects), world.interactive.collected), ([], [URL]))
        # Nothing ended without its outputs, so nothing released a claim of its own.
        self.assertEqual(self.calls(world, "release_claims"), [])

    def test_a_pending_siblings_claim_keeps_the_object_through_the_first_units_cleanup(self) -> None:
        world = self.world(("u1", "u2"))
        seen = {}
        cleanup = world.interactive.cleanup

        def cleanup_then_look(**arguments):
            result = cleanup(**arguments)
            if Path(arguments["manifest_path"]).parent.parent.name == "u1":
                seen.update(objects=sorted(world.interactive.objects), claims=claim_states(world))
            return result

        world.interactive.cleanup = cleanup_then_look
        book = self.finish(world)
        self.assertEqual(seen["objects"], [URL], "the first unit's cleanup leaves the object its sibling claimed")
        self.assertEqual(seen["claims"], {"u1": ("released", "lease", "raw_cleaned"), "u2": ("pending", "batch_plan", None)})
        self.assertEqual(world.interactive.fetches, [("u1", URL)])
        finished = [json.loads(row["detail_json"]) for row in book.attempts("u2")
                    if row["step"] == "download" and row["outcome"] == "ok"]
        self.assertEqual(finished[0]["transfer"],
                         {"transferred_bytes": 0, "objects_fetched": 0, "objects_refetched": 0, "cache_hits": 1})

    def test_without_the_claim_each_sibling_fetches_the_object_again(self) -> None:
        """The defect, played on an Interactive without pre_claim_downloads: each cleanup lets the store collect the
        object, the next sibling fetches it again, and the ledger's transfer says so where the units' bytes cannot."""
        world = self.world()
        world.interactive.pre_claim_supported = False
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["done"] * 3, "never a unit's failure")
        self.assertEqual(world.interactive.fetches, [("u1", URL), ("u2", URL), ("u3", URL)])
        self.assertEqual([(unit, detail["reason"]) for unit, detail in self.events(book, machine.SHARED_DOWNLOAD_NOT_CLAIMED)],
                         [("u1", "unsupported"), ("u2", "unsupported")])
        downloads = machine.summary(book)["downloads"]
        self.assertEqual(downloads, {"finished": 3, "bytes": 3000, "through_store": 3, "transferred_bytes": 3000,
                                     "objects_fetched": 3, "objects_refetched": 2, "cache_hits": 0})

    def test_the_transfer_shows_one_fetch_where_the_object_is_shared(self) -> None:
        book = self.finish(self.world())
        self.assertEqual(machine.summary(book)["downloads"],
                         {"finished": 3, "bytes": 3000, "through_store": 3, "transferred_bytes": 1000,
                          "objects_fetched": 1, "objects_refetched": 0, "cache_hits": 2})

    def test_an_excluded_sibling_releases_its_claim_and_the_object_is_collected(self) -> None:
        """A sibling Interactive refuses before any byte (download_blocked) never reads the object it was claimed
        for: it releases the claim as it ends, with the approval, and the store collects what no unit claims."""
        world = self.world(("u1", "u2"))
        world.scripts["u2"] = fakes.UnitScript(downloads=["blocked"])
        book = self.finish(world)
        self.assertEqual((book.unit("u2")["state"], book.unit("u2")["terminal_reason"]), ("excluded", "download_blocked"))
        self.assertEqual(book.unit("u2")["raw_disposition"], "none")
        self.assertEqual(self.calls(world, "release_claims"), [
            {"unit_id": "u2", "reason": "excluded", "authorization_path": book.unit("u2")["authorization_copy"]}])
        self.assertEqual(claim_states(world)["u2"], ("released", "batch_plan", "excluded"))
        self.assertEqual((sorted(world.interactive.objects), world.interactive.collected), ([], [URL]))
        released = self.events(book, machine.SHARED_DOWNLOAD_RELEASED)
        self.assertEqual(released, [("u2", {"ending": "excluded", "release_reason": "excluded", "approval_passed": True,
                                             "released": 1, "collected": [URL], "kept": [], "refused": []})])
        self.assertEqual(world.interactive.fetches, [("u1", URL)])

    def test_a_sibling_excluded_at_its_preflight_reads_the_object_from_the_store(self) -> None:
        """A sibling that downloaded and was then excluded holds its lease's claim, which Interactive's discard
        releases with its raw tree; the runner releases nothing of it."""
        world = self.world()
        world.scripts["u2"] = fakes.UnitScript(disposition="exclude")
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["done", "excluded", "done"])
        self.assertEqual(world.interactive.fetches, [("u1", URL)])
        self.assertEqual(claim_states(world)["u2"], ("released", "batch_plan", "discarded"))
        self.assertEqual([call["unit_id"] for call in self.calls(world, "release_claims")], ["u2"])
        self.assertEqual(self.events(book, machine.SHARED_DOWNLOAD_RELEASED), [], "nothing pending was left to release")
        self.assertEqual(world.interactive.collected, [URL])

    def test_a_stopped_sibling_releases_its_claim(self) -> None:
        world = self.world(("u1", "u2"))
        world.catalog.drift = {"u2"}
        book = self.finish(world)
        self.assertEqual(book.unit("u2")["state"], "stopped_policy_drift")
        self.assertEqual(claim_states(world)["u2"], ("released", "batch_plan", "excluded"))
        self.assertEqual(world.interactive.collected, [URL])

    def test_a_sibling_stopped_by_a_revoked_approval_releases_its_claim(self) -> None:
        """Review of 2026-10-25: u1's download claimed the object for u2, and the approval is revoked before u2
        downloads. u2 ends stopped_no_approval and releases its pending claim, without the revoked approval, so the
        store deletes nothing then, but no claim keeps the archive through every later cleanup."""
        world = self.world(("u1", "u2"))
        with world.open() as book:
            runner = world.runner(book)
            for _ in range(400):
                runner.iterate()
                if claim_states(world).get("u2", ("",))[0] == "pending":
                    break
                world.clock.sleep(30)
            else:
                self.fail("u2 was never claimed")
            self.assertNotIn("u2", world.interactive.download_starts)
            book.revoke("approval-1", "Test Person", "stop the campaign", runner.stamp())
            runner.run(until_idle=True, max_iterations=200)
            unit = book.unit("u2")
            self.assertEqual((unit["state"], unit["terminal_reason"]), ("stopped_no_approval", "approval_missing"))
            released = self.events(book, machine.SHARED_DOWNLOAD_RELEASED)
        self.assertNotIn("u2", world.interactive.download_starts)
        self.assertIn({"unit_id": "u2", "reason": "excluded", "authorization_path": ""}, self.calls(world, "release_claims"),
                      "released without the approval, which is revoked")
        self.assertEqual(claim_states(world)["u2"], ("released", "batch_plan", "excluded"))
        self.assertEqual([(unit, detail["ending"], detail["approval_passed"]) for unit, detail in released],
                         [("u2", "stopped_no_approval", False)])

    def test_a_sibling_whose_download_interactive_refuses_under_the_approval_releases_its_claim(self) -> None:
        """Review of 2026-10-25: Interactive refuses the campaign authorization at u2's download
        (campaign_authorization_refused). u2 ends stopped_no_approval and releases the claim u1's download made."""
        world = self.world(("u1", "u2"))
        download = world.interactive.download

        def refuse_u2(**arguments):
            if Path(arguments["handoff_path"]).parent.name == "u2":
                world.interactive.calls.append(("download", arguments))
                return {"ok": False, "reason": "campaign_authorization_refused", "codes": ["unit_not_covered"],
                        "detail": "campaign_authorization_refused [unit_not_covered]: approval-1 does not name u2."}
            return download(**arguments)

        world.interactive.download = refuse_u2
        book = self.finish(world)
        unit = book.unit("u2")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("stopped_no_approval", "campaign_authorization_refused"))
        self.assertEqual(book.unit("u1")["state"], "done")
        self.assertEqual(self.calls(world, "release_claims"), [
            {"unit_id": "u2", "reason": "excluded", "authorization_path": unit["authorization_copy"]}])
        self.assertEqual(claim_states(world)["u2"], ("released", "batch_plan", "excluded"))
        self.assertEqual((sorted(world.interactive.objects), world.interactive.collected), ([], [URL]))
        self.assertEqual(world.interactive.fetches, [("u1", URL)])

    def test_a_sibling_that_fails_before_its_download_releases_its_claim_as_failed(self) -> None:
        world = self.world(("u1", "u2"))
        handoff = world.catalog.handoff

        def no_handoff_for_u2(*, unit_id, class_proposal_id):
            if unit_id == "u2":
                return {"ok": False, "reason": "not_written", "detail": "the Catalog wrote no handoff"}
            return handoff(unit_id=unit_id, class_proposal_id=class_proposal_id)

        world.catalog.handoff = no_handoff_for_u2
        book = self.finish(world, max_iterations=5000)
        self.assertEqual((book.unit("u2")["state"], book.unit("u2")["failures"]), ("failed", 3))
        self.assertEqual(claim_states(world)["u2"], ("released", "batch_plan", "failed_terminal"))
        self.assertEqual(world.interactive.collected, [URL])

    def test_a_campaign_that_keeps_raw_data_releases_the_claim_and_deletes_nothing(self) -> None:
        world = self.world(("u1", "u2"), retention="keep", covers=("1", "3", "4", "split"))
        world.scripts["u2"] = fakes.UnitScript(downloads=["blocked"])
        book = self.finish(world)
        self.assertEqual(book.unit("u1")["raw_disposition"], "kept")
        self.assertEqual(self.calls(world, "release_claims"), [{"unit_id": "u2", "reason": "excluded", "authorization_path": ""}])
        self.assertEqual(claim_states(world)["u2"], ("released", "batch_plan", "excluded"))
        self.assertEqual((sorted(world.interactive.objects), world.interactive.collected), ([URL], []))

    def test_a_unit_retried_after_it_ended_is_claimed_again(self) -> None:
        """u2 ends excluded and releases its claim; an operator retries it before u3 downloads. u3's download claims
        it again, so u3's cleanup keeps the object and the retried u2 reads it from the store."""
        world = self.world()
        world.scripts["u2"] = fakes.UnitScript(downloads=["blocked", "ok"])
        with world.open() as book:
            runner = world.runner(book)
            for _ in range(400):
                runner.iterate()
                if book.unit("u2")["state"] == "excluded":
                    break
                world.clock.sleep(30)
            else:
                self.fail("u2 never ended")
            self.assertEqual(book.unit("u3")["state"], "class_settled", "u3 has not downloaded yet")
            self.assertEqual(claim_states(world)["u2"], ("released", "batch_plan", "excluded"))
            book.add_request("retry", "u2", "the repository fixed the record", "Test Person", runner.stamp())
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["done"] * 3)
        self.assertEqual([call["units"] for call in self.calls(world, "pre_claim")], [["u2", "u3"], ["u3"], ["u2"]])
        self.assertEqual(world.interactive.fetches, [("u1", URL)], "the retried unit read the object from the store")
        self.assertEqual(claim_states(world)["u2"], ("released", "batch_plan", "raw_cleaned"))
        self.assertEqual(world.interactive.collected, [URL])

    def test_a_group_of_one_is_downloaded_as_before(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        world = fakes.World(Path(directory.name), ["u1", "u2"])
        world.scripts["u2"] = fakes.UnitScript(downloads=["blocked"])
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "excluded"])
        self.assertEqual([name for name, _arguments in world.interactive.calls if name in ("pre_claim", "release_claims")], [])
        self.assertEqual(world.catalog.object_requests, [])
        for kind in (machine.SHARED_DOWNLOAD_CLAIMED, machine.SHARED_DOWNLOAD_NOT_CLAIMED, machine.SHARED_DOWNLOAD_RELEASED,
                     machine.SHARED_DOWNLOAD_NOT_RELEASED):
            self.assertEqual(book.events(kind), [], kind)
        finished = [json.loads(row["detail_json"]) for row in book.attempts("u1")
                    if row["step"] == "download" and row["outcome"] == "ok"]
        self.assertEqual(finished[0], {"job_id": book.unit("u1")["download_job_id"], "bytes": 1000},
                         "a lease that fetched without the store records what it always did")

    def test_a_claim_that_cannot_be_made_never_stops_the_download(self) -> None:
        world = self.world(("u1", "u2"))
        world.catalog.download_objects = lambda unit_ids: {"ok": False, "reason": "exception", "detail": "locked"}
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertEqual(self.events(book, machine.SHARED_DOWNLOAD_NOT_CLAIMED),
                         [("u1", {"siblings": ["u2"], "reason": "exception", "detail": "locked"})])
        self.assertEqual(self.calls(world, "pre_claim"), [])


class SharedDownloadResumeTests(unittest.TestCase):
    """The crash at every commit (test_campaign_resume), on a group of three that share one object: one run, one
    excluded at its preflight, one Interactive refuses before any byte. Stopped anywhere, the runner resumes to the
    same end, with the object fetched once and every claim released the same way."""

    def fresh(self, directory: str) -> fakes.World:
        world = fakes.World(Path(directory), ["s1", "s2", "s3"], download_groups={GROUP: ["s1", "s2", "s3"]})
        world.scripts["s2"] = fakes.UnitScript(disposition="exclude", ticks=1)
        world.scripts["s3"] = fakes.UnitScript(downloads=["blocked"])
        return world

    @staticmethod
    def outcome(world: fakes.World) -> dict:
        return {**resume.outcome(world), "fetches": list(world.interactive.fetches), "claims": claim_states(world),
                "objects": sorted(world.interactive.objects), "collected": list(world.interactive.collected)}

    def run_to_end(self, at: int | None, phase: str) -> tuple[dict, int]:
        with tempfile.TemporaryDirectory() as directory:
            world = self.fresh(directory)
            seen = []

            def hook(when: str) -> None:
                if when == phase:
                    seen.append(1)
                    if at is not None and len(seen) == at:
                        raise fakes.Crash()

            try:
                world.run(commit_hook=hook)
            except fakes.Crash:
                world.clock.sleep(3600)
                world.run()
            else:
                if at is not None:
                    self.fail(f"the run made fewer than {at} commits")
            result = self.outcome(world)
            result["class_saves"] = sorted(set(result["class_saves"]))
            return result, len(seen)

    def test_a_crash_at_every_commit_resumes_to_the_same_end(self) -> None:
        expected, commits = self.run_to_end(None, "after")
        self.assertEqual([expected["units"][key][:2] for key in ("s1", "s2", "s3")],
                         [("done", "outputs_produced"), ("excluded", "preflight_exclude"), ("excluded", "download_blocked")])
        self.assertEqual(expected["fetches"], [("s1", URL)])
        self.assertEqual(expected["claims"], {"s1": ("released", "lease", "raw_cleaned"),
                                              "s2": ("released", "batch_plan", "discarded"),
                                              "s3": ("released", "batch_plan", "excluded")})
        self.assertEqual((expected["objects"], expected["collected"]), ([], [URL]))
        for phase in ("before", "after"):
            for at in range(1, commits + 1):
                with self.subTest(phase=phase, commit=at):
                    self.assertEqual(self.run_to_end(at, phase)[0], expected)


if __name__ == "__main__":
    unittest.main()
