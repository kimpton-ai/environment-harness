import argparse
import json
import sys
from pathlib import Path

from . import presentation
from .contracts import ExperimentSpec, Principal
from .errors import HarnessError
from .evaluation import compare, rollouts
from .fixtures import SyntheticEnvironment
from .plugins import doctor, environment
from .runner import run
from .runtime import EnvironmentSession
from .showcase import create_synthetic_review_demo
from .store import EvidenceStore, encode


def main():
    parser = argparse.ArgumentParser(prog="environment-harness")
    parser.add_argument("--store", default=".environment-harness")
    parser.add_argument("--tenant", default="local")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
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
    token.add_argument("--environment")
    token.add_argument("--participant")
    args = parser.parse_args()
    if args.command == "doctor":
        print(json.dumps(doctor(), indent=2))
        return
    store = EvidenceStore(args.store)
    who = Principal(tenant=args.tenant, subject="local-researcher", role="researcher")
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
    session = EnvironmentSession(store, env)
    if args.command in ("quickstart", "run"):
        if args.command == "quickstart":
            result = create_synthetic_review_demo(store, who, turns=args.turns, training=args.training)
            print(json.dumps(result, indent=2))
            return
        else:
            from .adapters.programs import CommandAgent

            spec = ExperimentSpec.model_validate_json(Path(args.manifest).read_text())
            env = environment(spec.environment.id)
            session = EnvironmentSession(store, env)
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

        credential = store.issue(who, 86400)
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
        if args.participant:
            with store.transaction() as db:
                row = store.environment(db, args.environment, who)
                member = json.loads(row["participants"])[args.participant]
            who = Principal(
                tenant=args.tenant,
                subject=member["controller"],
                role="agent",
                environment=args.environment,
                participant=args.participant,
                generation=member["generation"],
            )
        elif args.environment:
            who = who.model_copy(update={"environment": args.environment})
        print(store.issue(who))
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
    session = EnvironmentSession(store, SyntheticEnvironment())
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
