#!/usr/bin/env python3
"""Mint an API key without the console.

The console is the normal path — this exists for bootstrapping (there is no key
before the first sign-in) and for scripted smoke tests. It talks to Postgres
directly, so run it where the database is reachable:

    kubectl -n voxtell exec -it deploy/voxtell-api -- \
        python -m scripts.mint_key --email you@example.com --name "smoke test"

Without --email it uses the single existing user when there is exactly one,
which is the common case on a fresh deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select

sys.path.insert(0, "/srv")  # container layout; harmless elsewhere

from api.auth import generate_api_key  # noqa: E402
from api.db import SessionLocal  # noqa: E402
from api.models import ApiKey, User, utcnow  # noqa: E402


async def amain(args) -> int:
    async with SessionLocal() as session:
        if args.email:
            result = await session.execute(select(User).where(User.email == args.email))
            user = result.scalar_one_or_none()
            if user is None:
                print(f"no user with email {args.email}. Sign in to the console "
                      "once — users are provisioned on first authentication.",
                      file=sys.stderr)
                return 1
        elif args.user_id:
            user = await session.get(User, uuid.UUID(args.user_id))
            if user is None:
                print(f"no user {args.user_id}", file=sys.stderr)
                return 1
        else:
            users = (await session.execute(select(User))).scalars().all()
            if len(users) != 1:
                print(f"{len(users)} users exist — pass --email or --user-id:",
                      file=sys.stderr)
                for u in users:
                    print(f"  {u.id}  {u.email or u.username}", file=sys.stderr)
                return 1
            user = users[0]

        token, prefix, token_hash = generate_api_key()
        session.add(
            ApiKey(user_id=user.id, name=args.name, prefix=prefix, token_hash=token_hash)
        )
        await session.commit()

    print(f"user  : {user.email or user.username} ({user.id})")
    print(f"name  : {args.name}")
    print(f"created: {utcnow().isoformat()}")
    print(f"\n{token}\n")
    print("Store it now — only the SHA-256 hash is kept, so it cannot be shown again.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--email", help="Owner's email (as seen in the Keycloak token)")
    p.add_argument("--user-id", help="Owner's VoxTell user UUID")
    p.add_argument("--name", default="cli", help="Label shown in the console")
    return asyncio.run(amain(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
