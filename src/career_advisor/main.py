"""Career Advisor CLI — invite management and web server entry point.

The product is the web app (see web.py); this CLI exists so the operator can
mint invitation links and run the server without touching the database by
hand.
"""

from __future__ import annotations

import argparse
import os

from . import db

# Where invite links point. Override per deployment with --base-url, or set
# CAREER_ADVISOR_BASE_URL; the local default is deliberately not a real host.
DEFAULT_BASE = "http://127.0.0.1:8611/career-advisor/"
PUBLIC_BASE = os.environ.get("CAREER_ADVISOR_BASE_URL", DEFAULT_BASE)


def cmd_create_invite(args: argparse.Namespace) -> None:
    conn = db.connect()
    try:
        code = db.create_invite(conn, args.name)
    finally:
        conn.close()
    print(f"Invite created for {args.name}:")
    print(f"  {args.base_url}?invite={code}")


def cmd_list_invites(args: argparse.Namespace) -> None:
    conn = db.connect()
    try:
        rows = db.list_invites(conn)
    finally:
        conn.close()
    if not rows:
        print("No invites yet. Create one with: career-advisor create-invite NAME")
        return
    for row in rows:
        state = "revoked" if row["revoked"] else "active"
        print(f"{row['code']}  {state:8}  {row['created_at']}  {row['name']}")


def cmd_revoke_invite(args: argparse.Namespace) -> None:
    conn = db.connect()
    try:
        found = db.revoke_invite(conn, args.code)
    finally:
        conn.close()
    print("Invite revoked." if found else "No such invite code.")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("career_advisor.web:app", host=args.host, port=args.port)


def cmd_review_session(args: argparse.Namespace) -> None:
    from . import review as review_mod

    conn = db.connect()
    try:
        sessions = review_mod.list_sessions(conn)
        if not sessions:
            print("No interview sessions yet.")
            return
        if args.session is None:
            if args.list:
                for row in sessions:
                    print(
                        f"{row['session_token'][:12]}…  {row['turns']:>4} turns"
                        f"  {row['name']}"
                    )
                return
            token = sessions[0]["session_token"]
        else:
            matches = [
                row["session_token"]
                for row in sessions
                if row["session_token"].startswith(args.session)
            ]
            if not matches:
                print(f"No session starting with {args.session!r}.")
                return
            token = matches[0]

        result = review_mod.review_session(conn, token)
        turns = db.list_interview_turns(conn, token) if args.transcript else None
        print(review_mod.format_review(result, args.transcript, turns))
    finally:
        conn.close()


def cmd_ask(args: argparse.Namespace) -> None:
    from . import llm

    print(llm.complete(args.prompt))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="career-advisor",
        description="Career Advisor — operator CLI (invites + server)",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-invite", help="Mint an invitation link")
    p.add_argument("name", help="Invitee display name")
    p.add_argument("--base-url", default=PUBLIC_BASE, help="Public app URL")
    p.set_defaults(func=cmd_create_invite)

    p = sub.add_parser("list-invites", help="List invitation codes")
    p.set_defaults(func=cmd_list_invites)

    p = sub.add_parser("revoke-invite", help="Revoke an invitation code")
    p.add_argument("code")
    p.set_defaults(func=cmd_revoke_invite)

    p = sub.add_parser("serve", help="Run the web server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8611)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "review-session",
        help="Read an interview back: repeats, engagement, coverage",
    )
    p.add_argument(
        "session",
        nargs="?",
        help="Session token prefix (default: the busiest session)",
    )
    p.add_argument("--list", action="store_true", help="List sessions and exit")
    p.add_argument(
        "--transcript", action="store_true", help="Include every Q and A"
    )
    p.set_defaults(func=cmd_review_session)

    p = sub.add_parser("ask", help="Smoke-test the LLM route (one prompt)")
    p.add_argument("prompt")
    p.set_defaults(func=cmd_ask)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
