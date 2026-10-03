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
        Every request is acted on by the runner that holds the campaign; with none running, by the next run.

A UNIT STOPS, NEVER THE RUNNER (the user's rule of 2026-10-02). A gate verdict, a failure or a hold stops
one unit's analysis, and the runner goes on with the others. A unit is held, unrun and uncounted with its
raw data kept, when the before-production gate gives no usable report (gate_held), or when Interactive
gives a reply or a record for it that the runner cannot read or act on (contract_held); a deletion
Interactive cannot make as called leaves that unit's raw data held. What pauses the whole campaign is what
every unit would meet alike: a pin change, a short disk, a repository outage, and a backend that does not
answer (looked at again hourly). Each lifts by itself once its cause has gone. Only an operator's own pause
waits for an operator's resume, as does a "contract" pause a runner before 2026-10-02 left in the ledger.

RUN --UNTIL-IDLE returns once every unit has ended or waits for disk. A held unit is not idle: the runner
stays, polling, and makes the held unit's step again every few hours, so a unit that stays held keeps it
running until a recheck gives what was missing or an operator skips the unit.

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
  and the user's to set up; `schedule-command` prints the commands and runs nothing.

Exit codes: 0 ok, 2 refused (digest mismatch, missing or revoked approval, a manifest that cannot be
approved), 3 unusable environment (a checkout, the Console, the extractor, a library or the backend), 5 the
campaign lock is held by another runner.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
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
        config = environment.backend.config()
        report["backend"] = {"running": config is not None,
                             "problems": environment.backend.check(config) if config else ["not running"]}
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

def command_run(args: argparse.Namespace) -> int:
    from campaign import ledger, machine, policy, ports

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
                print(f"The campaign backend is not usable: {started.get('detail')}", file=sys.stderr)
                return EXIT_ENVIRONMENT
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
    with _open(args) as book:
        resumed = book.resume(_now(), kinds=["operator", "contract", "disk", "fault"], detail={"by": "operator", "reason": args.reason})
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
    usable report (gate_held) or because Interactive's reply or record could not be read (contract_held)."""
    from campaign import ledger

    with _open(args) as book:
        units = [args.unit] if args.unit else [unit["unit_key"] for unit in book.units(ledger.HELD_STATES)]
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


def schedule_task_command(python: str | Path, script: str | Path, campaign: str) -> str:
    """The schtasks line for a campaign. /TR is one argument, so the quotes inside it are escaped as \\";
    a path with a space (the Python under the user profile) then stays one word when the task runs."""
    run = f'\\"{python}\\" \\"{script}\\" run --campaign {campaign} --until-idle'
    return (f'schtasks /Create /TN "MSDIAL-campaign-{campaign}" /SC ONLOGON /RU "%USERNAME%" /RL LIMITED '
            f'/TR "{run}" /F')


def command_schedule(args: argparse.Namespace) -> int:
    """Print, and never run, the commands that keep a campaign going across reboots."""
    print("# Persistent system configuration: the user decides and runs these. The runner runs none of them.")
    print(schedule_task_command(sys.executable, Path(__file__).resolve(), args.campaign))
    print("# ONLOGON runs the task when the user logs on, with no password stored. For ONSTART, before anyone")
    print("# logs on, the task needs the account's password: add /RP and type it at schtasks' own prompt.")
    print(f'# In Task Scheduler, set the task to restart on failure and "Do not start a new instance" if one runs.')
    print("# run --until-idle ends once every unit has ended or waits for disk. While a unit is held it keeps running,")
    print("# to make the held unit's step again every few hours; a request (recheck-held, skip) waits for a runner.")
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
    schedule.set_defaults(handler=command_schedule)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    _import_roots(args.interactive_root, args.catalog_root)
    try:
        return int(args.handler(args))
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return EXIT_ENVIRONMENT


if __name__ == "__main__":
    raise SystemExit(main())
