"""
Manage the GitHub webhook for this service at runtime.

The public URL is only known once a tunnel (e.g. `cloudflared tunnel --url
http://localhost:8000`) is up, so registration cannot be baked into config.

    uv run python scripts/webhook.py register https://xyz.trycloudflare.com
    uv run python scripts/webhook.py list
    uv run python scripts/webhook.py delete <hook_id>
    uv run python scripts/webhook.py delete --all-service   # every hook whose URL ends in /webhooks/github

Reads GITHUB_TOKEN / GITHUB_REPO / GITHUB_WEBHOOK_SECRET from the environment (.env).
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from app import github  # noqa: E402

WEBHOOK_PATH = "/webhooks/github"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    reg = sub.add_parser("register")
    reg.add_argument("base_url", help="public https base URL of the API (tunnel URL)")
    sub.add_parser("list")
    dele = sub.add_parser("delete")
    dele.add_argument("hook_id", nargs="?", type=int)
    dele.add_argument("--all-service", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "register":
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
        if not secret:
            print("GITHUB_WEBHOOK_SECRET is not set", file=sys.stderr)
            return 2
        hook = github.create_webhook(args.base_url.rstrip("/") + WEBHOOK_PATH, secret)
        print(json.dumps({"id": hook["id"], "url": hook["config"]["url"], "events": hook["events"]}))
        return 0

    if args.cmd == "list":
        for h in github.list_webhooks():
            print(json.dumps({"id": h["id"], "url": h["config"].get("url"), "active": h["active"]}))
        return 0

    if args.cmd == "delete":
        ids = [args.hook_id] if args.hook_id else []
        if args.all_service:
            ids += [h["id"] for h in github.list_webhooks() if h["config"].get("url", "").endswith(WEBHOOK_PATH)]
        for hid in ids:
            github.delete_webhook(hid)
            print(f"deleted hook {hid}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
