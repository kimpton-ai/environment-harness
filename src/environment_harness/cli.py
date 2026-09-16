import argparse
import json
import os
import sys
from pathlib import Path

from .contracts import AgentSpec, ExperimentSpec, Principal, RunPolicy
from .errors import HarnessError
from .evaluation import compare, rollouts
from .fixtures import SyntheticAgent, SyntheticEnvironment
from .plugins import doctor, environment
from .runner import run
from .runtime import EnvironmentSession
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
            spec = ExperimentSpec(
                environment=env.spec,
                participants=tuple(
                    AgentSpec(id=p, implementation="synthetic-agent@1", policy_version="1", checkpoint=True)
                    for p in ("alice", "bob")
                ),
                purpose="training" if args.training else "evaluation",
                split="training" if args.training else "heldout",
                policy=RunPolicy(max_turns=max(args.turns + 10, 20)),
                scoring_versions=("synthetic-control@1",),
            )
            agents = {p.id: SyntheticAgent() for p in spec.participants}
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
        import uvicorn

        from .server import create_app

        credential = store.issue(who, 86400)
        path = store.root / "researcher-token"
        path.write_text(credential + "\n")
        os.chmod(path, 0o600)
        print(f"Viewer: http://127.0.0.1:{args.port}\nCredential file: {path}", flush=True)
        uvicorn.run(create_app(session), host="127.0.0.1", port=args.port, access_log=False)
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
    if args.command == "compare":
        result = compare(store, args.environments, who)
    elif args.command == "attach":
        result = session.get(args.environment, who)
    elif args.command == "branch":
        result = session.branch(args.environment, who, args.checkpoint, json.loads(args.interventions))
    else:
        lease = session.lease(args.environment, who, "cli", ttl=30)
        if args.command == "checkpoint":
            result = session.checkpoint(args.environment, who, lease)
        elif args.command == "resume":
            result = session.resume(args.environment, who, lease)
        else:
            from .operations import Operations

            result = session.control(args.environment, who, lease, "cancel")
            Operations(store).cancel_prepared(args.environment, who)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (HarnessError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
