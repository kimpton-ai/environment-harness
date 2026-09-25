import argparse
import json
import sys
from pathlib import Path

from . import presentation
from .access import trusted_local
from .contracts import ExperimentSpec
from .errors import HarnessError
from .evaluation import compare, rollouts
from .fixtures import SyntheticEnvironment
from .plugins import doctor, environment
from .runner import run
from .runtime import _SessionRuntime
from .showcase import create_synthetic_review_demo
from .store import EvidenceStore, encode
from .training import TrainingRepository
from .trajectories import SourceRecord, SourceRegistration, SourceStatusUpdate, TrajectoryRepository


def main():
    parser = argparse.ArgumentParser(prog="environment-harness")
    parser.add_argument("--store", default=".environment-harness")
    parser.add_argument("--tenant", default="local")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    trajectory_list = sub.add_parser("trajectory-list")
    trajectory_list.add_argument("--limit", type=int, default=100)
    trajectory_list.add_argument("--cursor")
    trajectory_show = sub.add_parser("trajectory-show")
    trajectory_show.add_argument("trajectory")
    trajectory_records = sub.add_parser("trajectory-records")
    trajectory_records.add_argument("trajectory")
    trajectory_records.add_argument("--after", type=int, default=0)
    trajectory_records.add_argument("--limit", type=int, default=200)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("trajectory")
    snapshot_export = sub.add_parser("snapshot-export")
    snapshot_export.add_argument("snapshot")
    dataset_create = sub.add_parser("dataset-create")
    dataset_create.add_argument("name")
    dataset_create.add_argument("trajectories", nargs="+")
    dataset_show = sub.add_parser("dataset-show")
    dataset_show.add_argument("dataset")
    dataset_export = sub.add_parser("dataset-export")
    dataset_export.add_argument("dataset")
    source_register = sub.add_parser("source-register")
    source_register.add_argument("registration")
    source_ingest = sub.add_parser("source-ingest")
    source_ingest.add_argument("source")
    source_ingest.add_argument("records")
    source_status = sub.add_parser("source-status")
    source_status.add_argument("source")
    source_status.add_argument("status", nargs="?", help="Optional JSON status update; omit to inspect")
    train = sub.add_parser("train")
    train.add_argument("dataset")
    train.add_argument("integration")
    train.add_argument("--config", default="{}")
    quick = sub.add_parser("quickstart")
    quick.add_argument("--turns", type=int, default=10)
    quick.add_argument("--training", action="store_true")
    start = sub.add_parser("run")
    start.add_argument("manifest")
    start.add_argument("--turns", type=int, default=10)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--environment", default="synthetic-protocol")
    serve.add_argument("--open", action="store_true", help="Open the local browser viewer")
    for name in ("replay", "attach", "checkpoint", "resume", "cancel", "export"):
        command = sub.add_parser(name)
        command.add_argument("environment")
        if name == "export":
            command.add_argument("--training", action="store_true")
    branch = sub.add_parser("branch")
    branch.add_argument("environment")
    branch.add_argument("checkpoint")
    branch.add_argument("--interventions", default="{}")
    comparison = sub.add_parser("compare")
    comparison.add_argument("environments", nargs="+")
    comparison.add_argument(
        "--json", action="store_true", help="Print the comparison record instead of the summary"
    )
    listing = sub.add_parser("list")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--limit", type=int, default=100)
    for name in ("show", "timeline"):
        command = sub.add_parser(name)
        command.add_argument("environment")
        command.add_argument("--json", action="store_true")
    sub.choices["timeline"].add_argument(
        "--participant", help="Show only evidence visible to this participant"
    )
    sub.choices["timeline"].add_argument("--kind", help="Show only event kinds containing this text")
    sub.choices["timeline"].add_argument(
        "--verbose", "-v", action="store_true", help="Include recorded payloads"
    )
    token = sub.add_parser("token")
    token.add_argument("--session", help="Issue a participant credential bound to this environment session")
    token.add_argument("--participant", help="Participant bound to the issued credential")
    args = parser.parse_args()
    if args.command == "doctor":
        print(json.dumps(doctor(), indent=2))
        return
    store = EvidenceStore(args.store)
    # The in-process CLI is a trusted local interface and needs no credential.
    who = trusted_local(args.tenant, "local-cli")
    if args.command.startswith("trajectory-") or args.command in ("snapshot", "snapshot-export"):
        repository = TrajectoryRepository(store)
        if args.command == "trajectory-list":
            page, _ = repository.list_page(who, limit=args.limit, cursor=args.cursor)
            print(json.dumps([item.model_dump(mode="json") for item in page], indent=2))
        elif args.command == "trajectory-show":
            print(repository.get(args.trajectory, who).model_dump_json(by_alias=True, indent=2))
        elif args.command == "trajectory-records":
            print(
                repository.records_page(
                    args.trajectory, who, after=args.after, limit=args.limit
                ).model_dump_json(by_alias=True, indent=2)
            )
        elif args.command == "snapshot":
            print(repository.freeze(args.trajectory, who).model_dump_json(by_alias=True, indent=2))
        else:
            for row in repository.export_snapshot(args.snapshot, who):
                print(encode(row))
        return
    if args.command.startswith("dataset-") or args.command == "train":
        repository = TrainingRepository(store)
        if args.command == "dataset-create":
            result = repository.freeze_dataset(args.name, tuple(args.trajectories), who)
            print(result.model_dump_json(by_alias=True, indent=2))
        elif args.command == "dataset-show":
            print(repository.get_dataset(args.dataset, who).model_dump_json(by_alias=True, indent=2))
        elif args.command == "dataset-export":
            for row in repository.export_dataset(args.dataset, who):
                print(encode(row))
        else:
            from .plugins import training_integration

            integration = training_integration(args.integration)
            result = repository.run(args.dataset, integration, json.loads(args.config), who)
            print(result.model_dump_json(by_alias=True, indent=2))
        return
    if args.command.startswith("source-"):
        repository = TrajectoryRepository(store)
        if args.command == "source-register":
            registration = SourceRegistration.model_validate_json(Path(args.registration).read_text())
            result = repository.register_source(registration, who)
        elif args.command == "source-ingest":
            raw = Path(args.records).read_text().splitlines()
            records = tuple(SourceRecord.model_validate_json(line) for line in raw if line.strip())
            result = repository.ingest(args.source, records, who)
        elif args.status is not None:
            status = SourceStatusUpdate.model_validate_json(Path(args.status).read_text())
            result = repository.update_source_status(args.source, status, who)
        else:
            result = repository.source_status(args.source, who)
        print(result.model_dump_json(by_alias=True, indent=2))
        return
    if args.command in ("replay", "export"):
        source = (
            rollouts(store, args.environment, who)
            if getattr(args, "training", False)
            else store.replay(args.environment, who)
        )
        for row in source:
            print(encode(row))
        return
    if args.command in ("list", "show", "timeline", "compare"):
        inspect(store, who, args)
        return
    env = environment(args.environment) if args.command == "serve" else SyntheticEnvironment()
    if args.command not in ("quickstart", "serve", "token", "compare", "run"):
        with store.transaction() as db:
            row = store.environment(db, args.environment, who)
            spec = ExperimentSpec.model_validate_json(row["manifest"])
        env = environment(
            spec.environment.id,
            **({"mode": spec.environment.scheduling} if spec.environment.id == "synthetic-protocol" else {}),
        )
    session = _SessionRuntime(store, env)
    if args.command in ("quickstart", "run"):
        if args.command == "quickstart":
            result = create_synthetic_review_demo(store, who, turns=args.turns, training=args.training)
            print(json.dumps(result, indent=2))
            return
        else:
            from .adapters.programs import CommandAgent

            spec = ExperimentSpec.model_validate_json(Path(args.manifest).read_text())
            env = environment(spec.environment.id)
            session = _SessionRuntime(store, env)
            agents = {p.id: CommandAgent(p.config["command"], p.implementation) for p in spec.participants}
        created = session.create(spec, who)
        result = run(session, created["id"], who, agents, turns=args.turns)
        print(json.dumps(result, indent=2))
        return
    if args.command == "serve":
        import threading

        import uvicorn

        from .local_viewer import LocalViewerAccess, open_when_ready
        from .server import create_app

        credential = store.issue_viewer(args.tenant, ttl=86400)
        origin = f"http://127.0.0.1:{args.port}"
        print(f"Viewer: {origin}", flush=True)
        access = LocalViewerAccess(origin, credential)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(session, local_access=access),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
                proxy_headers=False,
            )
        )
        if args.open:
            threading.Thread(target=open_when_ready, args=(server, f"{origin}/home"), daemon=True).start()
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        return
    if args.command == "token":
        if bool(args.participant) != bool(args.session):
            raise ValueError("participant credentials require both --session and --participant")
        if args.participant:
            with store.transaction() as db:
                row = store.environment(db, args.session, who, "credential.participant.issue")
                member = json.loads(row["participants"])[args.participant]
            print(
                store.issue_participant(
                    args.tenant,
                    member["controller"],
                    session=args.session,
                    participant=args.participant,
                    generation=member["generation"],
                )
            )
        else:
            print(store.issue_management(args.tenant))
        return
    if args.command == "attach":
        result = session.get(args.environment, who)
    elif args.command == "branch":
        result = session.branch(args.environment, who, args.checkpoint, json.loads(args.interventions))
    elif args.command == "cancel":
        result = session.cancel(args.environment, who)
    else:
        lease = session.lease(args.environment, who, "cli", ttl=30)
        try:
            if args.command == "checkpoint":
                result = session.checkpoint(args.environment, who, lease)
            else:
                result = session.resume(args.environment, who, lease)
        finally:
            from contextlib import suppress

            from .errors import Conflict

            with suppress(Conflict):
                session.release(args.environment, who, lease)
    print(json.dumps(result, indent=2))


def inspect(store, who, args):
    """Read-only inspection. Human-readable by default; --json prints the underlying records."""
    session = _SessionRuntime(store, SyntheticEnvironment())
    if args.command == "list":
        rows = session.list(who, args.limit)
        print(json.dumps(rows, indent=2) if args.json else presentation.render_list(rows))
        return
    if args.command == "compare":
        result = compare(store, args.environments, who)
        print(json.dumps(result, indent=2) if args.json else presentation.render_comparison(result))
        return
    item = session.get(args.environment, who)
    perspective = getattr(args, "participant", None)
    events = presentation.filter_events(
        store.replay(args.environment, who), perspective, getattr(args, "kind", None)
    )
    turns = presentation.build_timeline(events, item["participants"])
    if args.command == "timeline":
        print(
            json.dumps(turns, indent=2)
            if args.json
            else presentation.render_timeline(turns, args.verbose, perspective)
        )
        return
    reports = store.reports(args.environment, who)
    print(
        json.dumps(item | {"reports": reports}, indent=2)
        if args.json
        else presentation.render_environment(item, reports, turns)
    )


if __name__ == "__main__":
    try:
        main()
    except (HarnessError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
