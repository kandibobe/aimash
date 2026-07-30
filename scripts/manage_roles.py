"""Admin CLI for operational RBAC assignments.

Examples:
  python -m scripts.manage_roles assign --actor 100 --user 200 --role approver --customer 7753643025
  python -m scripts.manage_roles revoke --actor 100 --user 200 --role approver --customer 7753643025

``--actor`` must already be an env/runtime admin or have ``manage_roles``. The CLI therefore
cannot bootstrap around the existing admin boundary.
"""

from __future__ import annotations

import argparse
import asyncio

from operations.governance import ROLES, assign_role, revoke_role


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(description="Manage Aimash operational roles")
    sub = out.add_subparsers(dest="command", required=True)
    for command in ("assign", "revoke"):
        item = sub.add_parser(command)
        item.add_argument("--actor", type=int, required=True)
        item.add_argument("--user", type=int, required=True)
        item.add_argument("--role", choices=sorted(ROLES), required=True)
        item.add_argument("--customer", default="*")
    return out


async def run(args: argparse.Namespace) -> int:
    if args.command == "assign":
        row = await assign_role(
            actor_user_id=args.actor,
            user_id=args.user,
            role=args.role,
            customer_id=args.customer,
        )
        print(f"assigned user={row.user_id} role={row.role} customer={row.customer_id}")
        return 0
    removed = await revoke_role(
        actor_user_id=args.actor,
        user_id=args.user,
        role=args.role,
        customer_id=args.customer,
    )
    print("revoked" if removed else "no active assignment")
    return 0 if removed else 1


def main() -> int:
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
