"""Run an approved full-repository MS-DIAL reanalysis campaign, unattended, one unit at a time.

A campaign is one approved manifest of analysis units (scripts/campaign/plan.py), one ledger that records
every unit's state and how it got there (scripts/campaign/ledger.py), and one runner process that moves
the units along (scripts/campaign/machine.py) through Interactive and the Catalog (scripts/campaign/
ports.py) under the rules the user decided on 2026-09-30 (scripts/campaign/policy.py).

THE ORDER OF USE
    python scripts/campaign-runner.py plan --campaign ID --pool declared --purpose "..." \\
        --retention delete_after_validated_output --console <MSDIALCUI.exe> [--extractor <RawMetadataConsoleApp.exe>] \\
        --profile <profile.json> [--resources <campaign-resources.local.json>] [--catalog <db>] \\
        [--replan-from <an earlier campaign whose approval is revoked> ...]
        read-only on the Catalog and the analysis root; writes campaign-manifest.json and prints its digest.
        A pilot names its units instead of a pool: --units <file or comma list> plans exactly those units,
        from both pools, each held to its own pool's rules (selection_basis), in one manifest (pool "pilot").
        The extractor defaults to the newest built pin of Interactive's PINNED_BUILDS beside the Interactive
        checkout; a manifest is approvable only with a verified, pinned extractor and clean checkouts.
    python scripts/campaign-runner.py approve --campaign ID --digest sha256:... --approval-id ID \\
        --by NAME --statement "the person's words" --covers 1,3,4,5,split
        only after the person approved that digest in the conversation; the approval id is theirs
    python scripts/campaign-runner.py run --campaign ID [--until-idle] [--max-units N] [--prefetch N]
    python scripts/campaign-runner.py status|export|verify-env|pause|resume|skip|retry|release-held|recheck-held|revoke --campaign ID ...
        release-held --unit KEY deletes, under boundary 5, the raw data an ended unit holds against the rules
        once Interactive's deletion accepts them (the runner also looks again at every start and every few
        hours); retry runs the unit again from its Class decision. recheck-held [--unit KEY] makes the step
        every held unit (or the one) was held at again now: for a unit the before-production gate gave no
        usable report for, the gate. The runner also makes it again at every start and every few hours.
        A unit Interactive's disposition holds (disposition_held: a multi-collision-energy AIF unit waiting for
        a patched Console, the rule of 2026-10-07) is rechecked only when asked: recheck-held --unit KEY, or
        recheck-held --disposition-held for every such unit, makes its preflight again, decided for the pinned
        Console: with Interactive 0.5.34 and a pinned Console that has MsdialWorkbench #825 the unit then runs as
        AIF (multi_ce_aif_with_console_825).
        Every request is acted on by the runner that holds the campaign; with none running, by the next run.

A UNIT STOPS, NEVER THE RUNNER (the user's rule of 2026-10-02). A gate verdict, a failure or a hold stops
one unit's analysis, and the runner goes on with the others. A unit is held, unrun and uncounted with its
raw data kept, when the before-production gate gives no usable report (gate_held), or when Interactive
gives a reply or a record for it that the runner cannot read or act on, a reply that does not parse among
them (contract_held, the user's default of 2026-10-03), a job poll's among them once the job's Console or
download no longer runs; a deletion Interactive cannot make as called leaves that unit's raw data held. What
pauses the whole campaign is what every unit would meet alike, the four pauses the status export names: a
pin change, a short disk, a repository outage, and an Interactive backend that does not answer (a refused,
timed-out or broken connection, to any call, the diagnostic's estimate included; the user's default of
2026-10-03), the last two looked at again hourly. Each lifts by itself once its cause has gone. Only an operator's own pause waits
for an operator's resume, as does a "contract" pause a runner before 2026-10-02 left in the ledger.

LAUNCH AN UNATTENDED CAMPAIGN THROUGH TASK SCHEDULER, NOT FROM A CLAUDE SESSION (2026-10-06). A process
started from Claude Code or the Claude desktop app, Start-Process included, sits in the app's job object,
which allows no breakaway, and the app is force-closed when it updates. `schedule-command` prints a task
definition with no execution time limit, one instance, restart on failure and an hourly start. The runner
starts the Interactive backend through WMI (run --backend-launch, default auto), so the backend is neither
the runner's child nor in its job: a tree kill of the runner, the end of its task or of the Claude app leaves
the backend and a running Console alone, and the next runner reattaches. A backend already answering on the
port is reused, and the runner says how it was started where that is knowable (backend-launch.json beside the
job registry). Readiness is /api/agent/status within --backend-start-timeout (300 s); /api/config, which starts
every Console candidate, then has --backend-config-timeout (120 s) of its own. Three failed starts in a row
pause starting for an hour, and the ledger keeps the failures, the pause and a start still waited for, so the
next runner the task starts honours them (2026-10-07).

A CAMPAIGN WITH NO WORK LEFT (every unit ended, no request waiting, no ended unit holding raw data the runner
would look at again) makes run exit at once, before it takes the campaign, locks the Catalog or starts a
backend, with the schtasks lines that disable or delete the task; the task is the user's to end. It still
releases a Catalog lock this campaign's approval holds whose owner has died, as run does before it locks the
Catalog: otherwise the lock a runner left when it was killed after the last unit would hold the Catalog for good.
A lock that cannot be checked (the Catalog cannot be imported, a lock file that cannot be read or is not a lock
record) makes it exit 3 and says so; it is never reported as another approval's.

RUN --UNTIL-IDLE returns once every unit has ended or waits for disk. A held unit is not idle: the runner
stays, polling, and makes the held unit's step again every few hours, so a unit that stays held keeps it
running until a recheck gives what was missing or an operator skips the unit.

A UNIT INTERACTIVE'S DISPOSITION HOLDS (2026-10-07). A multi-collision-energy AIF unit is held until a patched
Console exists: Interactive 0.5.31's disposition says skip, hold true, aif_multi_ce_awaiting_console. The runner
holds it (disposition_held): not run, raw data kept, no retry counted, reported as held in status
(summary disposition_held, with the count and the reasons), and the other units go on. Nothing rechecks it by
itself and it keeps no runner running: only recheck-held --unit KEY (or --disposition-held for all of them) lifts
it, by making its preflight again, and Interactive then decides it anew. skip --unit KEY is the explicit decision
that lifts the hold: the unit ends as skipped, and its discard passes Interactive release_disposition_hold, which
records disposition_hold_released_by operator_skip and deletes its raw data under boundary 5; a held split part's
discard records that the part has ended, and its parent's release then goes ahead, passing the release only when
every held part was skipped. A held part that ended otherwise keeps the parent's raw data (kept, waiting for that
part). Nothing else releases a hold:
Interactive never discards a held unit without it, and an Interactive that cannot take it leaves the raw data held.

THE PATCHED CONSOLE (MsdialWorkbench #825, Interactive 0.5.34). The preflight is sent the pinned Console's path,
and Interactive decides a multi-energy AIF unit for it: with #825 the unit runs as AIF under
multi_ce_aif_with_console_825 (its disposition records aif_multi_ce_run and the probe of that Console), without
#825 it is held as above. The plan prints which (pins.console.multi_energy_aif) and refuses neither. A recheck that
releases a held unit writes a disposition_hold_lifted event (what held it, and aif_multi_ce_run), and status
counts such runs under multi_energy_aif_runs. A unit whose AIF inputs' energies differ, or one with an input whose
energy is unrecorded, is still held with that Console. A campaign pinned to a Console without #825 cannot take it
up in place, since a changed pin pauses the campaign: plan a new campaign pinning the #825 Console with
--replan-from, which takes its disposition_held units again.

THE PROFILE (--profile, schema msdial-campaign-profile.v1) is the answers every unit's run shares, part
of the approved manifest, naming each library as "library:<file name>" and never by location:
    {"schema": "msdial-campaign-profile.v1",
     "answers": {"library_strategy": "existing", "use_retention_time_for_annotation": false},
     "by_ion_mode": {
       "Positive": {"libraries": {"msp_paths": ["library:<positive MSP file>"], "lbm_path": "library:<LBM2 file>"}},
       "Negative": {"libraries": {"msp_paths": ["library:<negative MSP file>"], "lbm_path": "library:<LBM2 file>"}}}}
THE RESOURCE MAP (--resources, default campaign-resources.local.json beside this repository's README; any
*.local.json is git-ignored) is the only place a library's location is written:
    {"schema": "msdial-campaign-resources.v1", "libraries": {"<file name>": "<where it is on this machine>"}}

WHAT IT NEVER DOES
- Decide whether a unit may run: Interactive's campaign_disposition says so after the raw-header preflight.
- Record a person's reading of the sentences a gate check left for them (boundary 6), or cover boundary 2.
- Name a private library's location anywhere but the call to Interactive: the git-ignored resource map
  (*.local.json) says where each library is on this machine; the manifest, the authorization record, the
  ledger, the logs and every unit's campaign-record.json name libraries by file name and sha256.
- Register itself with Task Scheduler or keep the machine awake. That is persistent system configuration
  and the user's to set up; `schedule-command` prints the commands (or, with --xml-out, writes the task
  definition to a file) and registers nothing.

Exit codes: 0 ok, 2 refused (digest mismatch, missing or revoked approval, a manifest that cannot be
approved), 3 unusable environment (a checkout, the Console, the extractor, a library or the backend), 5 the
campaign lock is held by another runner.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
GATE_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

DEFAULT_WORKSPACE_ROOT = Path(r"D:\13_MSDIAL_Public_Reanalysis\analysis")
DEFAULT_INTERACTIVE_ROOT = Path(os.environ.get("MSDIAL_INTERACTIVE_ROOT") or r"D:\0_SourceCode\msdial_interactive_app")
DEFAULT_CATALOG_ROOT = Path(os.environ.get("MSDIAL_CATALOG_ROOT") or r"D:\0_SourceCode\msdial_repository_catalog")
DEFAULT_RESOURCES = GATE_ROOT / "campaign-resources.local.json"
DEFAULT_PORT = 8766
# ports.BACKEND_LAUNCH_METHODS, repeated so that parsing the command line imports nothing.
BACKEND_LAUNCH_METHODS = ("auto", "wmi", "child")
EXIT_OK, EXIT_REFUSED, EXIT_ENVIRONMENT, EXIT_LOCKED = 0, 2, 3, 5


def _import_roots(interactive_root: Path, catalog_root: Path) -> None:
    for root in (catalog_root / "src", interactive_root):
        text = str(root)
        if text not in sys.path:
            sys.path.insert(0, text)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _campaign_directory(workspace_root: Path, campaign_id: str) -> Path:
    return Path(workspace_root) / "_campaigns" / campaign_id


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True, default=str))


# ---- plan -----------------------------------------------------------------------------------------------

def command_plan(args: argparse.Namespace) -> int:
    from campaign import plan, policy, ports

    catalog_path = Path(args.catalog) if args.catalog else _default_catalog()
    out = Path(args.out) if args.out else None
    if out is not None and out.suffix.casefold() == ".json":
        # A dry run named as a file: the manifest is that file, and its summary sits beside it.
        directory, manifest_path = out.parent, out
    else:
        directory = out or _campaign_directory(args.workspace_root, args.campaign)
        manifest_path = directory / "campaign-manifest.json"
    if (directory / "ledger.sqlite").exists():
        print(f"Campaign {args.campaign} is already approved; its manifest is not replaced.", file=sys.stderr)
        return EXIT_REFUSED
    libraries: dict[str, str] = {}
    if args.resources:
        libraries = ports.load_resources(args.resources)["libraries"]
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8-sig")) if args.profile else None
    campaign_policy = policy.CampaignPolicy.from_dict(
        json.loads(Path(args.policy).read_text(encoding="utf-8-sig")) if args.policy else None
    )
    # The extractor is the newest built pin of Interactive's PINNED_BUILDS unless another is named; either
    # way the plan records what it inspects as, and a build that is not verified and pinned is not approvable.
    extractor = args.extractor or str(ports.default_extractor_path(args.interactive_root))
    reader = ports.PinReader(
        console_path=args.console or "", extractor_path=extractor, libraries=libraries,
        interactive_root=args.interactive_root, catalog_root=args.catalog_root,
    )
    pins = {
        "console": reader.console() if args.console else {"exists": False},
        "extractor": reader.extractor(),
        "libraries": reader.library_identities(),
        **reader.code(),
    }
    try:
        replan = plan.replan_states(Path(args.workspace_root), args.replan_from) if args.replan_from else None
        unit_ids = plan.read_unit_list(args.units) if args.units else None
    except plan.PlanError as error:
        print(str(error), file=sys.stderr)
        return EXIT_REFUSED
    catalog = ports.read_only_catalog(catalog_path)
    try:
        manifest = plan.build_manifest(
            catalog, pool=args.pool, campaign_id=args.campaign, analysis_purpose=args.purpose,
            workspace_root=Path(args.workspace_root), raw_retention_policy=args.retention, pins=pins,
            profile=profile, campaign_policy=campaign_policy,
            class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, args.purpose),
            catalog_database=str(catalog_path), progress=lambda message: print(message, file=sys.stderr),
            replan=replan, unit_ids=unit_ids,
        )
    except plan.PlanError as error:
        print(str(error), file=sys.stderr)
        return EXIT_REFUSED
    finally:
        catalog.close()
    digest = plan.write_manifest(manifest_path, manifest)
    # What an approval would cover: boundary 5 only where the retention deletes (a pilot that keeps raw data
    # is approved without it).
    covers = ["1", "3", "4", "split"] + (["5"] if args.retention == "delete_after_validated_output" else [])
    problems = plan.approval_problems(manifest, covers)
    ports.write_json_atomic(manifest_path.with_name(manifest_path.stem + ".summary.json"), {
        "manifest_path": str(manifest_path), "manifest_digest": digest, "totals": manifest["totals"],
        "approval_problems": problems, "approval_covers": covers,
    })
    print(plan.summary_text(manifest, digest))
    if problems:
        print("Not approvable as it stands: " + "; ".join(problems), file=sys.stderr)
    return EXIT_OK


def _default_catalog() -> Path:
    from msdial_repository_catalog.mcp_server import DEFAULT_DATABASE

    return Path(DEFAULT_DATABASE)


# ---- approve --------------------------------------------------------------------------------------------

def command_approve(args: argparse.Namespace) -> int:
    from campaign import ledger, plan, ports
    from msdial_app.campaign_authorization import CampaignAuthorization, CampaignAuthorizationError

    directory = _campaign_directory(args.workspace_root, args.campaign)
    manifest_path = directory / "campaign-manifest.json"
    if (directory / "ledger.sqlite").exists():
        print(f"Campaign {args.campaign} already has a ledger and an approval.", file=sys.stderr)
        return EXIT_REFUSED
    manifest, digest = plan.read_manifest(manifest_path)
    if digest != args.digest:
        print(f"The manifest hashes to {digest}, not the approved {args.digest}; nothing was recorded.", file=sys.stderr)
        return EXIT_REFUSED
    covers = [item.strip() for item in args.covers.split(",") if item.strip()]
    problems = plan.approval_problems(manifest, covers)
    if problems:
        print("The manifest cannot be approved: " + "; ".join(problems), file=sys.stderr)
        return EXIT_REFUSED
    approved_at = _now()
    record = plan.authorization_record(
        manifest, manifest_path, digest, approval_id=args.approval_id, approved_by=args.by,
        approved_at=approved_at, statement=args.statement, covers=covers,
    )
    authorization_path = directory / "campaign-authorization.json"
    ports.write_json_atomic(authorization_path, record)
    try:
        authorization = CampaignAuthorization.load(authorization_path)
    except CampaignAuthorizationError as error:
        authorization_path.unlink(missing_ok=True)
        print(f"Interactive refuses the authorization record: {error}", file=sys.stderr)
        return EXIT_REFUSED
    units, groups = plan.ledger_rows(manifest)
    campaign = {
        "campaign_id": manifest["campaign_id"], "pool": manifest["pool"], "manifest_path": str(manifest_path),
        "manifest_digest": digest, "analysis_purpose": manifest["analysis_purpose"],
        "workspace_root": manifest["workspace_root"], "raw_retention_policy": manifest["raw_retention_policy"],
        "catalog_database": manifest["catalog"]["database"], "authorization_path": str(authorization_path),
        "authorization_sha256": authorization.sha256, "policy": manifest["policy"], "profile": manifest["profile"],
        "pins": manifest["pins"],
    }
    approval = {
        "approval_id": args.approval_id, "manifest_digest": digest, "approved_by": args.by,
        "approved_at": approved_at, "statement": args.statement, "covers": covers,
    }
    with ledger.Ledger(directory / "ledger.sqlite") as book:
        book.create(campaign, approval, units, groups, approved_at)
    print(f"Approval {args.approval_id} of {digest} recorded for {len(units)} units; ledger {directory / 'ledger.sqlite'}.")
    return EXIT_OK


# ---- the environment -----------------------------------------------------------------------------------

class Environment:
    """What `run` and `verify-env` build from an approved campaign."""

    def __init__(self, args: argparse.Namespace) -> None:
        from campaign import ledger, policy, ports

        self.directory = _campaign_directory(args.workspace_root, args.campaign)
        self.ledger = ledger.Ledger(self.directory / "ledger.sqlite")
        self.campaign = self.ledger.campaign()
        self.policy = policy.CampaignPolicy.from_dict(self.campaign["policy"])
        pins = self.campaign["pins"]
        self.resources = ports.load_resources(args.resources or DEFAULT_RESOURCES)
        missing = sorted({item["name"] for item in pins.get("libraries") or []} - set(self.resources["libraries"]))
        if missing:
            raise ValueError(f"The resource map does not locate the pinned libraries {', '.join(missing)}.")
        self.pin_reader = ports.PinReader(
            console_path=(pins.get("console") or {}).get("path") or "",
            extractor_path=(pins.get("extractor") or {}).get("path") or "",
            libraries={name: self.resources["libraries"][name] for name in (item["name"] for item in pins.get("libraries") or [])},
            interactive_root=args.interactive_root, catalog_root=args.catalog_root,
        )
        self.port = int(args.port)
        self.interactive = ports.InteractivePort(port=self.port)
        self.catalog = ports.CatalogPort(self.campaign["catalog_database"])
        self.gate = ports.GatePort(timeout=self.policy.gate_timeout_seconds, gate_commit=(pins.get("gate") or {}).get("commit", ""))
        self.backend = None if args.no_backend else ports.BackendSupervisor(
            python=sys.executable, interactive_root=Path(args.interactive_root), host="127.0.0.1", port=self.port,
            jobs_file=self.directory / "backend" / "agent-jobs.json",
            workspace_root=self.campaign["workspace_root"],
            log_directory=self.directory / "logs" if getattr(args, "backend_log", False) else None,
            launch_method=getattr(args, "backend_launch", "auto"),
            start_timeout=getattr(args, "backend_start_timeout", 300.0),
            config_timeout=getattr(args, "backend_config_timeout", 120.0),
            # The failures in a row, the pause after them and the start still waited for outlive this runner.
            state=ports.LedgerStartState(self.ledger),
        )

    def ports(self):
        from campaign import machine, ports

        return machine.Ports(
            interactive=self.interactive, catalog=self.catalog, gate=self.gate, disk=ports.LocalDisk(),
            clock=ports.SystemClock(), pins=self.pin_reader, backend=self.backend,
        )


def command_verify_env(args: argparse.Namespace) -> int:
    from campaign import machine, policy

    try:
        environment = Environment(args)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return EXIT_ENVIRONMENT
    report: dict[str, Any] = {"campaign_id": environment.campaign["campaign_id"]}
    report["pin_differences"] = policy.pin_differences(environment.campaign["pins"], environment.pin_reader.current())
    report["interactive"] = environment.interactive.capabilities()
    if environment.backend is not None:
        running = environment.backend.status() is not None
        config, why = environment.backend.read_config() if running else (None, "")
        report["backend"] = {"running": running,
                             "problems": environment.backend.check(config) if config else
                             [f"it answers /api/agent/status, but its /api/config {why}" if running else "not running"]}
        report["backend"]["start_state"] = environment.ledger.backend_start_state()
        if running:
            report["backend"]["origin"] = environment.backend.origin()
    from campaign.ports import LocalDisk

    free, total = LocalDisk().usage(environment.campaign["workspace_root"])
    report["disk"] = {"free_bytes": free, "total_bytes": total,
                      "reserve_bytes": policy.disk_reserve(total, environment.policy.disk)}
    report["catalog_lock"] = environment.catalog.lock_module.campaign_lock_status(environment.catalog.database)
    report["summary"] = machine.summary(environment.ledger)
    # What the runner cannot run without (Interactive 0.5.17): a preflight that takes the approval and
    # applies its disposition, and the cleanup that takes it. The authorized discard and the split-parent
    # release (plan item 14) have fallbacks, and are reported only.
    capabilities = report["interactive"]
    report["missing_capabilities"] = [
        name for name in ("classify_preflight", "preflight_authorization", "disposition_hold", "authorized_cleanup")
        if not capabilities.get(name)
    ]
    _print(report)
    blocking = report["pin_differences"] or report["missing_capabilities"]
    return EXIT_ENVIRONMENT if blocking else EXIT_OK


# ---- run ------------------------------------------------------------------------------------------------

def task_name(campaign: str) -> str:
    return f"MSDIAL-campaign-{campaign}"


def task_end_commands(campaign: str) -> list[str]:
    """The lines that stop the scheduled task from starting runners on a campaign that has ended: printed,
    never run (the task is the user's persistent configuration)."""
    return [f'schtasks /Change /TN "{task_name(campaign)}" /Disable',
            f'schtasks /Delete /TN "{task_name(campaign)}" /F']


def _release_stale_catalog_lock(book: Any) -> tuple[int | None, str]:
    """On a finished campaign, release the Catalog lock this campaign's approval holds when the runner that took
    it has died (ports.release_stale_campaign_lock), as `run` does before it takes the lock. Never another
    approval's lock, nor one whose owner is alive or cannot be judged. (exit code or None, what was found).

    A lock that cannot be checked is EXIT_ENVIRONMENT: the Catalog cannot be imported, or its lock file exists
    but cannot be read or is not a lock record (campaign_lock_status then says readable False and names no
    approval; round 4 of the 2026-10-07 review: that lock was reported as another approval's, and run exited 0)."""
    from campaign import ports

    campaign, approval = book.campaign(), book.approval()
    database = campaign["catalog_database"]
    try:
        from msdial_repository_catalog import campaign_lock

        released = ports.release_stale_campaign_lock(campaign_lock, database, approval["approval_id"])
        status = campaign_lock.campaign_lock_status(database)
    except Exception as error:  # noqa: BLE001 - ImportError, an unreadable lock, the Catalog's own refusal
        return EXIT_ENVIRONMENT, (f"The Catalog's campaign lock could not be checked ({type(error).__name__}: {error}); "
                                  f"if a runner of this campaign died holding it, it still holds the Catalog.")
    if released:
        book.event("catalog_lock_released", _now(), {
            "approval_id": approval["approval_id"], "owner_pid": released.get("pid"),
            "acquired_at": released.get("acquired_at"), "why": "its owner had died and the campaign has no work left"})
        return None, (f"Released the Catalog's campaign lock that approval {approval['approval_id']} held: its owner "
                      f"(pid {released.get('pid')}, since {released.get('acquired_at')}) is no longer running.")
    if not status.get("locked"):
        return None, ""
    if not status.get("readable"):
        return EXIT_ENVIRONMENT, (f"The Catalog's campaign lock could not be checked: {status.get('message')} Whose it "
                                  f"is cannot be told, so it is left as it is, and every catalog update stays refused "
                                  f"while it exists.")
    if status.get("approval_id") != approval["approval_id"]:
        return None, f"The Catalog's campaign lock is held by another approval, and is left as it is: {status.get('message')}"
    return None, f"The Catalog's campaign lock is left as it is: {status.get('message')}"


def _no_work_left(args: argparse.Namespace) -> int | None:
    """EXIT_OK when the campaign has no work left (machine.remaining_work), read from its ledger before anything is
    locked or started, else None. If so, say so, with how to disable or delete the task, and record it once.

    The Catalog lock is the exception (2026-10-07 round 3): the only code that releases a lock this campaign's
    runner left when it died is `run`'s, before it takes the lock, so a finished campaign releases it here too.
    A lock that cannot be checked is EXIT_ENVIRONMENT."""
    from campaign import ledger, machine

    path = _campaign_directory(args.workspace_root, args.campaign) / "ledger.sqlite"
    if not path.is_file():
        return None
    with ledger.Ledger(path) as book:
        if machine.remaining_work(book):
            return None
        code, lock_note = _release_stale_catalog_lock(book)
        last = book.last_event(("runner_started", "no_work_left"))
        if last is None or last["kind"] != "no_work_left":
            book.event("no_work_left", _now(), {"task": task_name(args.campaign)})
    print(f"Campaign {args.campaign} has no work left: every unit has ended, no request waits for a runner, and no "
          f"ended unit holds raw data the runner would look at again. The runner exits without taking the campaign, "
          f"locking the Catalog or starting a backend.", file=sys.stderr)
    if lock_note:
        print(lock_note, file=sys.stderr)
    print("If a scheduled task starts this runner, disable or delete it (the runner changes no task itself):", file=sys.stderr)
    for line in task_end_commands(args.campaign):
        print(f"  {line}", file=sys.stderr)
    print(f"A request recorded later (retry, recheck-held) then waits for a runner started by hand: "
          f"campaign-runner.py run --campaign {args.campaign}.", file=sys.stderr)
    return EXIT_OK if code is None else code


def command_run(args: argparse.Namespace) -> int:
    from campaign import ledger, machine, policy, ports

    # Before the runner lock, the Catalog lock and the backend (2026-10-07 review of PR #30): the scheduled task
    # starts a runner every hour, and each one on a finished campaign used to lock the Catalog and start a
    # backend that nothing then stopped. It still releases a Catalog lock this campaign's dead runner left.
    finished = _no_work_left(args)
    if finished is not None:
        return finished
    try:
        environment = Environment(args)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return EXIT_ENVIRONMENT
    book = environment.ledger
    campaign = environment.campaign
    code = environment.pin_reader.code()
    dirty = [name for name in ("interactive", "catalog", "gate") if code[name].get("dirty")]
    if dirty and not args.allow_dirty:
        print(f"Refused: the {', '.join(dirty)} checkout has uncommitted changes (--allow-dirty to run anyway).", file=sys.stderr)
        return EXIT_ENVIRONMENT
    pid, created, host = ports.runner_process_identity()
    stale = environment.policy.runner_lock_stale_seconds
    taken, holder = book.take_lock(
        pid, created, host, _now(),
        holder_alive=lambda record: ports.runner_alive(record, datetime.now(timezone.utc), stale),
    )
    if not taken:
        print(f"Campaign {campaign['campaign_id']} is held by runner pid {holder.get('pid')} on {holder.get('host')}.", file=sys.stderr)
        return EXIT_LOCKED
    stopping = {"now": False}

    def stop(_signal: int, _frame: Any) -> None:
        stopping["now"] = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)
    heartbeat = ledger.Heartbeat(book.path, pid, environment.policy.heartbeat_seconds, _now).start()
    try:
        approval = book.approval()
        try:
            environment.catalog.lock(approval["approval_id"], campaign["campaign_id"])
        except Exception as error:  # noqa: BLE001 - the Catalog's own refusal says who holds it
            print(f"The Catalog could not be held for the campaign: {error}", file=sys.stderr)
            return EXIT_ENVIRONMENT
        if environment.backend is not None:
            started = environment.backend.ensure()
            if not started.get("ok"):
                # In the ledger, not only on the console: the failures in a row and the pause after them are
                # read again by the next runner (ports.LedgerStartState), and this says why.
                book.event("backend_unavailable", _now(), {key: started[key] for key in (
                    "detail", "pid", "process_created_at", "method", "start_failures", "retry_at", "origin")
                    if started.get(key) is not None})
                print(f"The campaign backend is not usable: {started.get('detail')}", file=sys.stderr)
                return EXIT_ENVIRONMENT
            book.event("backend_started" if started.get("started") else "backend_reused", _now(),
                       {key: started[key] for key in ("pid", "process_created_at", "launcher_pid", "method", "in_job",
                                                      "seconds", "wmi_failure", "origin") if started.get(key) is not None})
            if started.get("started"):
                print(f"Started the campaign backend: pid {started.get('pid')}, through {started.get('method')}, "
                      f"answering after {started.get('seconds')} s.", file=sys.stderr)
            else:
                origin = started.get("origin") or {}
                print(f"Reusing the campaign backend on port {environment.port}: pid {origin.get('pid')}, started "
                      f"{'by ' + origin['by'] + ' through ' + str(origin.get('how')) if origin.get('by') else 'in a way not known'}"
                      f"{'; ' + origin['note'] if origin.get('note') else ''}"
                      f"{'; ' + origin['warning'] if origin.get('warning') else ''}.", file=sys.stderr)
        if args.prefetch is not None and args.prefetch != environment.policy.prefetch:
            book.event("prefetch_changed", _now(), {"from": environment.policy.prefetch, "to": args.prefetch})
        runner = machine.Runner(book, environment.ports(), resources=environment.resources, stop=lambda: stopping["now"])
        if args.prefetch is not None:
            runner.policy = policy.CampaignPolicy.from_dict({**runner.policy.as_dict(), "prefetch": args.prefetch})
        result = runner.run(until_idle=args.until_idle, max_units=args.max_units)
        if runner.idle():
            environment.catalog.unlock(approval["approval_id"])
        _print(result)
        return EXIT_OK
    finally:
        heartbeat.stop()
        book.release_lock(pid, _now())
        book.close()


# ---- the ledger's other commands ------------------------------------------------------------------------

def _open(args: argparse.Namespace):
    from campaign import ledger

    path = _campaign_directory(args.workspace_root, args.campaign) / "ledger.sqlite"
    if not path.is_file():
        raise FileNotFoundError(f"Campaign {args.campaign} has no ledger at {path}; approve it first.")
    return ledger.Ledger(path)


def command_status(args: argparse.Namespace) -> int:
    from campaign import machine

    with _open(args) as book:
        if args.unit:
            _print({"unit": machine.unit_status(book.unit(args.unit)), "transitions": book.transitions(args.unit),
                    "attempts": book.attempts(args.unit), "gate": book.gate_verdicts(args.unit)})
        else:
            _print(machine.summary(book))
    return EXIT_OK


def command_export(args: argparse.Namespace) -> int:
    from campaign import machine, ports

    with _open(args) as book:
        document, tsv = machine.export_status(book)
    target = Path(args.out)
    ports.write_json_atomic(target, document)
    target.with_suffix(".tsv").write_text(tsv, encoding="utf-8")
    print(f"Wrote {target} and {target.with_suffix('.tsv')}: {document['summary']['units']} units.")
    return EXIT_OK


def command_pause(args: argparse.Namespace) -> int:
    with _open(args) as book:
        book.pause("operator", args.reason or "paused by the operator", _now())
    print("Paused; the runner finishes the step in hand and then only watches jobs already running.")
    return EXIT_OK


def command_resume(args: argparse.Namespace) -> int:
    from campaign import ledger

    with _open(args) as book:
        resumed = book.resume(_now(), kinds=["operator", "contract", "disk", *ledger.FAULT_PAUSES],
                              detail={"by": "operator", "reason": args.reason})
    print("Resumed." if resumed else "Nothing to resume (a pin pause lifts only when the pins match again).")
    return EXIT_OK


def _runner_note(book: Any) -> str:
    """Who acts on a request: the runner that holds the campaign now, or none until one is started.

    A request is a row in the ledger, read only by a runner's loop: recorded while no runner runs, it waits."""
    from campaign import policy, ports

    campaign = book.campaign()
    record = book.runner()
    stale = policy.CampaignPolicy.from_dict(campaign["policy"]).runner_lock_stale_seconds
    now = datetime.now(timezone.utc)
    alive = False
    if record.get("pid"):
        try:
            alive = ports.runner_alive(record, now, stale)
        except ImportError:
            # Without Interactive's process probe, the heartbeat alone says whether a runner holds the campaign.
            heartbeat = policy.parse_iso(record.get("heartbeat_at"))
            alive = heartbeat is not None and (now - heartbeat).total_seconds() <= stale
    if alive:
        return f"runner pid {record['pid']} on {record.get('host')} acts on it at its next step"
    return (f"no runner is running, so nothing acts on it until one is started "
            f"(campaign-runner.py run --campaign {campaign['campaign_id']})")


def command_request(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None) or args.command
    with _open(args) as book:
        request_id = book.add_request(action, args.unit, args.reason, args.by or "", _now())
        note = _runner_note(book)
    print(f"Request {request_id} ({action} {args.unit}) recorded; {note}.")
    return EXIT_OK


def command_recheck_held(args: argparse.Namespace) -> int:
    """A recheck_held request for the named unit, or for every held unit: held because the gate gave no
    usable report (gate_held) or because Interactive's reply or record could not be read (contract_held), and,
    with --disposition-held, every unit Interactive's disposition holds (disposition_held, 2026-10-07), which
    nothing else rechecks."""
    from campaign import ledger

    with _open(args) as book:
        states = (ledger.DISPOSITION_HELD,) if args.disposition_held else ledger.HELD_STATES
        units = [args.unit] if args.unit else [unit["unit_key"] for unit in book.units(states)]
        requests = [book.add_request("recheck_held", unit, args.reason, args.by or "", _now()) for unit in units]
        note = _runner_note(book)
    if not requests:
        print("No unit is held for a recheck.")
    for request_id, unit in zip(requests, units):
        print(f"Request {request_id} (recheck_held {unit}) recorded; {note}.")
    return EXIT_OK


def command_revoke(args: argparse.Namespace) -> int:
    from campaign import ports

    with _open(args) as book:
        approval = book.approval()
        book.revoke(approval["approval_id"], args.by, args.reason, _now())
        # Interactive checks the record it is given, so every copy says the approval is revoked.
        paths = {book.campaign()["authorization_path"], *(unit["authorization_copy"] for unit in book.units() if unit["authorization_copy"])}
    for path in sorted(paths):
        try:
            record = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        record["revoked_at"] = _now()
        ports.write_json_atomic(path, record)
    print(f"Approval {approval['approval_id']} revoked; no boundary is crossed under it from now on.")
    return EXIT_OK


def _task_python(python: str | Path) -> str:
    """pythonw.exe beside the given Python when there is one: a task's python.exe would open a console window on
    the user's desktop for weeks, and closing that window would end the runner. Started with no console, the
    runner runs itself again in a windowless one (run_in_windowless_console)."""
    windowless = Path(python).with_name("pythonw.exe")
    return str(windowless) if windowless.is_file() else str(python)


def schedule_task_xml(*, python: str | Path, script: str | Path, campaign: str, user: str, workspace_root: str | Path,
                      interactive_root: str | Path, catalog_root: str | Path, start: str) -> str:
    """The Task Scheduler definition of a campaign's runner (schema 1.2), for schtasks /Create /XML.

    What schtasks /Create /SC ONLOGON left at Task Scheduler's defaults, and why each is set here:
    - ExecutionTimeLimit PT0S: no limit. The default (72 hours) stops the task, and with it its process tree,
      three days into a campaign meant to run for weeks; a stop for the time limit is not a failure, so
      restart-on-failure does not start it again.
    - MultipleInstancesPolicy IgnoreNew: one runner. The campaign lock refuses a second one as well.
    - RestartOnFailure every 5 minutes, 3 times; and an hourly trigger besides the logon trigger, so a runner
      that ended for any reason is started again within the hour. The hourly trigger stays (2026-10-07): run
      exits with code 3 when the backend cannot be started or a pause after repeated failures is in force,
      and whether Task Scheduler counts a non-zero exit code as a failure to restart for is not established
      here; without the hourly start such a campaign would wait for the next logon. What the hourly start
      costs is bounded instead: a runner whose campaign has no work left exits before it takes the campaign,
      locks the Catalog or starts a backend (machine.remaining_work), and says how to disable or delete the
      task; the pause after repeated backend failures is kept in the ledger, so an hourly runner honours it.
    - Battery and idle conditions off, StartWhenAvailable on.
    The runner is pythonw.exe (no window) and writes its output to --log-file. The paths and the roots are the
    ones given here, so the task does not depend on the environment variables of the session that printed it.
    """
    from xml.sax.saxutils import escape

    def quoted(value: str | Path) -> str:
        return '"' + str(value) + '"'

    arguments = " ".join((
        quoted(script), "--workspace-root", quoted(workspace_root), "--interactive-root", quoted(interactive_root),
        "--catalog-root", quoted(catalog_root), "run", "--campaign", campaign, "--until-idle",
        "--log-file", quoted(_campaign_directory(Path(workspace_root), campaign) / "logs" / "runner.log"),
    ))
    return "\n".join((
        '<?xml version="1.0" encoding="UTF-16"?>',
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">',
        "  <RegistrationInfo>",
        f"    <Description>{escape(f'MS-DIAL public-repository campaign {campaign}: campaign-runner.py run --until-idle')}</Description>",
        "  </RegistrationInfo>",
        "  <Triggers>",
        "    <LogonTrigger>",
        "      <Enabled>true</Enabled>",
        f"      <UserId>{escape(user)}</UserId>",
        "    </LogonTrigger>",
        "    <TimeTrigger>",
        "      <Repetition>",
        "        <Interval>PT1H</Interval>",
        "        <StopAtDurationEnd>false</StopAtDurationEnd>",
        "      </Repetition>",
        f"      <StartBoundary>{escape(start)}</StartBoundary>",
        "      <Enabled>true</Enabled>",
        "    </TimeTrigger>",
        "  </Triggers>",
        "  <Principals>",
        '    <Principal id="Author">',
        f"      <UserId>{escape(user)}</UserId>",
        "      <LogonType>InteractiveToken</LogonType>",
        "      <RunLevel>LeastPrivilege</RunLevel>",
        "    </Principal>",
        "  </Principals>",
        "  <Settings>",
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "    <AllowHardTerminate>true</AllowHardTerminate>",
        "    <StartWhenAvailable>true</StartWhenAvailable>",
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
        "    <IdleSettings>",
        "      <StopOnIdleEnd>false</StopOnIdleEnd>",
        "      <RestartOnIdle>false</RestartOnIdle>",
        "    </IdleSettings>",
        "    <AllowStartOnDemand>true</AllowStartOnDemand>",
        "    <Enabled>true</Enabled>",
        "    <Hidden>false</Hidden>",
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>",
        "    <WakeToRun>false</WakeToRun>",
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
        "    <Priority>7</Priority>",
        "    <RestartOnFailure>",
        "      <Interval>PT5M</Interval>",
        "      <Count>3</Count>",
        "    </RestartOnFailure>",
        "  </Settings>",
        '  <Actions Context="Author">',
        "    <Exec>",
        f"      <Command>{escape(_task_python(python))}</Command>",
        f"      <Arguments>{escape(arguments)}</Arguments>",
        f"      <WorkingDirectory>{escape(str(GATE_ROOT))}</WorkingDirectory>",
        "    </Exec>",
        "  </Actions>",
        "</Task>",
        "",
    ))


def schedule_task_command(campaign: str, xml_path: str | Path) -> str:
    """The schtasks line that registers the campaign's task from its XML definition."""
    return f'schtasks /Create /TN "{task_name(campaign)}" /XML "{xml_path}" /F'


def command_schedule(args: argparse.Namespace) -> int:
    """Print, and never run, the commands that keep a campaign going across reboots and across its launcher."""
    user = "\\".join(part for part in (os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")) if part) or "%USERNAME%"
    definition = schedule_task_xml(
        python=sys.executable, script=Path(__file__).resolve(), campaign=args.campaign, user=user,
        workspace_root=Path(args.workspace_root).resolve(), interactive_root=Path(args.interactive_root).resolve(),
        catalog_root=Path(args.catalog_root).resolve(), start=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    )
    if args.xml_out:
        # A file, not a task: schtasks reads it; registering it is the user's step.
        Path(args.xml_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.xml_out).write_text(definition, encoding="utf-16")
        xml_path = str(Path(args.xml_out).resolve())
    else:
        xml_path = "<the file you saved the definition above to, as UTF-16>"
        print(definition)
        print("# (schedule-command --xml-out FILE writes the definition as UTF-16 instead of printing it.)")
    task = task_name(args.campaign)
    print("# Persistent system configuration: the user decides and runs these. The runner runs none of them.")
    print("# LAUNCH THE RUNNER THROUGH THIS TASK, NOT FROM A CLAUDE SESSION. A process started from Claude Code or the")
    print("# Claude desktop app (Start-Process included) sits in the app's job object, which allows no breakaway, and")
    print("# the app is force-closed when it updates (it was on 2026-10-02 and 2026-10-06): a runner started there ends")
    print("# with it. The backend the runner starts is created through WMI, outside the runner's process tree and job,")
    print("# so ending the runner or its task leaves the backend and a running Console alone; the next runner reattaches.")
    print(schedule_task_command(args.campaign, xml_path))
    print("# The definition sets: no execution time limit (ExecutionTimeLimit PT0S; schtasks /SC alone leaves 72 hours),")
    print("# one instance (IgnoreNew), restart on failure every 5 minutes up to 3 times, and an hourly start besides the")
    print("# logon start, so a runner that ended is started again within the hour. It runs pythonw.exe (no window) and")
    print("# writes the runner's output to the campaign's logs\\runner.log. InteractiveToken: it runs while the user is")
    print("# logged on, with no password stored.")
    print("# Check what was registered (ExecutionTimeLimit must read PT0S), then start it now:")
    print(f'schtasks /Query /TN "{task}" /XML')
    print(f'schtasks /Run /TN "{task}"')
    print("# run --until-idle ends once every unit has ended or waits for disk. While a unit is held it keeps running,")
    print("# to make the held unit's step again every few hours; a request (recheck-held, skip) waits for a runner.")
    print("# A unit Interactive's disposition holds (disposition_held) keeps nothing running: recheck-held --unit KEY")
    print("# or --disposition-held lifts it.")
    print("# A backend that cannot be started 3 times in a row is not started again for an hour; the ledger keeps that")
    print("# pause, so the hourly start honours it. A runner on a campaign with no work left (every unit ended, no request")
    print("# waiting, no held raw data to look at again) exits before it locks the Catalog or starts a backend, and the")
    print("# task goes on starting it every hour until you end the task. When the campaign has ended, disable or delete it:")
    for line in task_end_commands(args.campaign):
        print(line)
    print("# The backend the last runner started is not stopped by either; stop it yourself once no job runs in it.")
    print("# Keep the machine awake while the campaign runs (AC power), for example:")
    print("powercfg /change standby-timeout-ac 0")
    print("powercfg /change hibernate-timeout-ac 0")
    return EXIT_OK


# ---- the command line -------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    top.add_argument("--workspace-root", type=Path, default=DEFAULT_WORKSPACE_ROOT)
    top.add_argument("--interactive-root", type=Path, default=DEFAULT_INTERACTIVE_ROOT)
    top.add_argument("--catalog-root", type=Path, default=DEFAULT_CATALOG_ROOT)
    commands = top.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="build the campaign manifest, read-only on the Catalog")
    plan.add_argument("--campaign", required=True)
    selection = plan.add_mutually_exclusive_group(required=True)
    selection.add_argument("--pool", choices=("declared", "acquisition_unknown"))
    selection.add_argument("--units", help="a pilot: plan exactly these analysis units, from both pools, each held to "
                                           "its own pool's rules; a file (a JSON list, or one id per line) or a comma list")
    plan.add_argument("--purpose", required=True, help="the analysis_purpose, in the person's words")
    plan.add_argument("--retention", required=True, choices=("keep", "delete_after_validated_output"))
    plan.add_argument("--catalog", help="the Catalog database (default: the Catalog's own)")
    plan.add_argument("--console", help="the MS-DIAL Console to pin")
    plan.add_argument("--extractor", help="the raw-metadata extractor to pin")
    plan.add_argument("--resources", help="the git-ignored map of library file names to locations")
    plan.add_argument("--profile", help=f"the shared answers ({'msdial-campaign-profile.v1'})")
    plan.add_argument("--policy", help="campaign policy overrides (JSON)")
    plan.add_argument("--out", help="a dry run: write the manifest into this folder instead of the campaign directory, "
                                     "or to this file where the name ends in .json")
    plan.add_argument("--replan-from", action="append", default=[], metavar="CAMPAIGN",
                      help="an earlier campaign, its approval revoked, whose units that did not end done are planned again")
    plan.set_defaults(handler=command_plan)

    approve = commands.add_parser("approve", help="record the person's approval of one manifest digest")
    approve.add_argument("--campaign", required=True)
    approve.add_argument("--digest", required=True)
    approve.add_argument("--approval-id", required=True, help="the approval id the person gave")
    approve.add_argument("--by", required=True)
    approve.add_argument("--statement", required=True, help="the person's words, verbatim")
    approve.add_argument("--covers", required=True, help="e.g. 1,3,4,5,split; never 2 or 6")
    approve.set_defaults(handler=command_approve)

    for name, handler, text in (
        ("run", command_run, "run the campaign"),
        ("verify-env", command_verify_env, "check the pins, the backend, the disk and the Catalog lock"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--campaign", required=True)
        command.add_argument("--resources", default=str(DEFAULT_RESOURCES))
        command.add_argument("--port", type=int, default=DEFAULT_PORT)
        command.add_argument("--no-backend", action="store_true", help="use a backend already listening on --port")
        if name == "run":
            command.add_argument("--until-idle", action="store_true",
                                 help="return once every unit has ended or waits for disk; a held unit keeps the runner "
                                      "going to recheck it every few hours")
            command.add_argument("--max-units", type=int)
            command.add_argument("--prefetch", type=int)
            command.add_argument("--allow-dirty", action="store_true")
            command.add_argument("--backend-log", action="store_true",
                                 help="keep the backend's own output in the campaign's logs folder (Interactive's stream, not redacted)")
            command.add_argument("--backend-launch", choices=BACKEND_LAUNCH_METHODS, default="auto",
                                 help="how the backend is started: wmi (outside the runner's process tree and job), child "
                                      "(the runner's own child, which a tree kill of the runner ends), or auto (wmi, else child)")
            command.add_argument("--log-file", help="append the runner's own output (stdout and stderr) to this file, "
                                                    "as the scheduled task does: pythonw.exe has no console to write to")
            command.add_argument("--backend-start-timeout", type=float, default=300.0,
                                 help="seconds a started backend has to answer (default 300)")
        command.add_argument("--backend-config-timeout", type=float, default=120.0,
                             help="seconds /api/config, which probes every Console candidate, has to answer (default 120)")
        command.set_defaults(handler=handler)

    status = commands.add_parser("status", help="counts by state, or one unit's history")
    status.add_argument("--campaign", required=True)
    status.add_argument("--unit")
    status.set_defaults(handler=command_status)

    export = commands.add_parser("export", help="per-unit status as JSON and TSV")
    export.add_argument("--campaign", required=True)
    export.add_argument("--out", required=True)
    export.set_defaults(handler=command_export)

    for name, handler in (("pause", command_pause), ("resume", command_resume)):
        command = commands.add_parser(name)
        command.add_argument("--campaign", required=True)
        command.add_argument("--reason", default="")
        command.set_defaults(handler=handler)

    for name, action, text in (
        ("skip", "skip", "ask the runner to skip one unit"),
        ("retry", "retry", "ask the runner to run one unit again from its Class decision"),
        ("release-held", "release_held", "ask the runner to delete the raw data an ended unit holds, as the rules say"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--campaign", required=True)
        command.add_argument("--unit", required=True)
        command.add_argument("--reason", required=True)
        command.add_argument("--by", default="")
        command.set_defaults(handler=command_request, action=action)

    recheck = commands.add_parser(
        "recheck-held", help="make the step of the held units again now: the gate, for a unit it gave no usable report for")
    recheck.add_argument("--campaign", required=True)
    recheck.add_argument("--unit", help="one held unit; every held unit when left out")
    recheck.add_argument("--disposition-held", action="store_true",
                         help="every unit Interactive's disposition holds (disposition_held, a multi-collision-energy "
                              "AIF unit waiting for a patched Console, MsdialWorkbench #825), in place of the gate- and "
                              "contract-held units; each is decided anew for the pinned Console")
    recheck.add_argument("--reason", default="operator recheck of a held unit")
    recheck.add_argument("--by", default="")
    recheck.set_defaults(handler=command_recheck_held)

    revoke = commands.add_parser("revoke", help="revoke the campaign's approval")
    revoke.add_argument("--campaign", required=True)
    revoke.add_argument("--by", required=True)
    revoke.add_argument("--reason", required=True)
    revoke.set_defaults(handler=command_revoke)

    schedule = commands.add_parser("schedule-command", help="print the Task Scheduler and power commands; runs nothing")
    schedule.add_argument("--campaign", required=True)
    schedule.add_argument("--xml-out", help="write the task definition to this file (UTF-16) instead of printing it")
    schedule.set_defaults(handler=command_schedule)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    _import_roots(args.interactive_root, args.catalog_root)
    if getattr(args, "log_file", None):
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        stream = open(args.log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
        print(f"--- {_now()} campaign-runner.py {' '.join(sys.argv[1:] if argv is None else argv)}")
    try:
        return int(args.handler(args))
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return EXIT_ENVIRONMENT


def _has_console() -> bool:
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes

    buffer = (wintypes.DWORD * 1)()
    return ctypes.WinDLL("kernel32").GetConsoleProcessList(buffer, 1) > 0


def run_in_windowless_console(command: list[str]) -> int:
    """Run the command in a console of its own with no window, wait for it, and return its exit code.

    The scheduled task starts the runner with pythonw.exe, so that no window sits on the desktop for weeks
    (closing it would end the runner). A process with no console makes Windows build a new console, with a
    window, for every console program it starts: the gate, and the extractor of the raw-header preflight the
    runner calls in-process. So a runner with no console runs itself again under python.exe with
    CREATE_NO_WINDOW, whose console those programs inherit, as the backend's do (ports.backend_creation_flags).
    """
    environment = dict(os.environ, MSDIAL_RUNNER_CONSOLE="1")
    process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    return process.wait()


if __name__ == "__main__":
    if os.name == "nt" and not os.environ.get("MSDIAL_RUNNER_CONSOLE") and not _has_console():
        raise SystemExit(run_in_windowless_console(
            [str(Path(sys.executable).with_name("python.exe")), str(Path(__file__).resolve()), *sys.argv[1:]]))
    raise SystemExit(main())
