"""loopcut-render: the same calls as the MCP tools, for shells, scripts and CI.

  loopcut-render login | logout | account
  loopcut-render quote scene.blend [--frames 1-120] [--samples 256] …
  loopcut-render start QUOTE_ID | status JOB_ID [--wait] | fetch JOB_ID [--out DIR] | cancel JOB_ID
  loopcut-render render scene.blend [--frames …] [--yes | --max-credits N]   quote, start, wait, fetch
  loopcut-render mcp                                                          stdio MCP server

Results print as JSON on stdout; progress and prompts go to stderr.
"""

import argparse
import json
import re
import sys

from . import __version__, mcp_server
from .service import TERMINAL, RenderError, Service


def _frames(value):
    match = re.fullmatch(r"(-?\d+)(?:-(-?\d+))?(?::(\d+))?", value)
    if not match:
        raise argparse.ArgumentTypeError("use N, START-END or START-END:STEP")
    start = int(match[1])
    result = {"frame_start": start, "frame_end": int(match[2]) if match[2] else start}
    if match[3]:
        result["frame_step"] = int(match[3])
    return result


def _settings(args):
    settings = dict(args.frames or {})
    for name in ("engine", "samples", "resolution_pct", "camera"):
        if getattr(args, name) is not None:
            settings[name] = getattr(args, name)
    if args.no_denoise:
        settings["denoise"] = False
    if args.turntable is not None:
        settings["turntable"] = {"targets": args.turntable} if args.turntable else {}
    return settings


def _render_options(parser):
    parser.add_argument("blend", help="saved .blend file")
    parser.add_argument("--frames", type=_frames, help="N, START-END or START-END:STEP (default: frame 1)")
    parser.add_argument("--engine", choices=("cycles", "eevee"))
    parser.add_argument("--samples", type=int)
    parser.add_argument("--resolution-pct", type=int, dest="resolution_pct")
    parser.add_argument("--camera")
    parser.add_argument("--no-denoise", action="store_true")
    parser.add_argument("--turntable", nargs="*", metavar="OBJECT", help="360° turntable of these objects (none: whole scene)")
    parser.add_argument("--blender", help="Blender executable to pack with")
    parser.add_argument("--confirm-token", help="token from an approved daily_limit_reached or rate_limited refusal")


def _parser():
    parser = argparse.ArgumentParser(prog="loopcut-render", description="Render .blend files on Loopcut's cloud GPUs.")
    parser.add_argument("--version", action="version", version=f"loopcut-render {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="sign in to Loopcut in the browser")
    commands.add_parser("logout", help="forget the stored Loopcut key")
    commands.add_parser("account", help="plan, credits and render availability")
    _render_options(commands.add_parser("quote", help="pack, upload and price a render"))
    start = commands.add_parser("start", help="start a quoted render (spends credits)")
    start.add_argument("quote_id")
    start.add_argument("--confirm-token")
    status = commands.add_parser("status", help="job status")
    status.add_argument("job_id")
    status.add_argument("--wait", action="store_true", help="wait until the job finishes")
    fetch = commands.add_parser("fetch", help="download frames and the QA report")
    fetch.add_argument("job_id")
    fetch.add_argument("--out", help="folder to save into (default: loopcut_renders/<job> next to the .blend)")
    cancel = commands.add_parser("cancel", help="cancel a render; unspent credits are refunded")
    cancel.add_argument("job_id")
    render = commands.add_parser("render", help="quote, start, wait and fetch in one go")
    _render_options(render)
    render.add_argument("--out", help="folder to save into")
    approve = render.add_mutually_exclusive_group()
    approve.add_argument("--yes", action="store_true", help="start without asking")
    approve.add_argument("--max-credits", type=int, help="start without asking if the quote is at most this")
    commands.add_parser("mcp", help="run the stdio MCP server (for Claude Code, Codex, Cursor, Claude Desktop)")
    return parser


def _say(message):
    print(message, file=sys.stderr, flush=True)


def _approved(quote, args):
    if args.yes or (args.max_credits is not None and quote["credits"] <= args.max_credits):
        return True
    if args.max_credits is not None or not sys.stdin.isatty():
        _say(f"Quote {quote['credits']} credits is over the limit or needs approval; not started.")
        return False
    answer = input(f"Render {quote['frames']} frame(s) for about {quote['credits']} credits (${quote['usd']:.2f})? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def _render(service, args):
    quote = service.quote(args.blend, _settings(args), blender=args.blender, confirm_token=args.confirm_token)
    for warning in quote["warnings"]:
        _say("warning: " + warning)
    if not _approved(quote, args):
        return quote
    job = service.start(quote["quote_id"], confirm_token=args.confirm_token)
    while job["status"] not in TERMINAL:
        progress = job.get("progress") or {}
        _say(f"{job['status']} {progress.get('pct', 0)}% {progress.get('message', '')}".rstrip())
        job = service.status(job["job_id"], wait=30)
    if job["status"] != "succeeded":
        return job
    return service.fetch(job["job_id"], args.out)


def _run(service, args):
    if args.command == "login":
        result = service.login(wait=0)
        while not result.get("signed_in"):
            _say(f"Open {result['verification_url']} and approve code {result['user_code']}. Waiting…")
            result = service.login(wait=60)
        return result
    if args.command == "logout":
        return service.logout()
    if args.command == "account":
        return service.account()
    if args.command == "quote":
        return service.quote(args.blend, _settings(args), blender=args.blender, confirm_token=args.confirm_token)
    if args.command == "start":
        return service.start(args.quote_id, confirm_token=args.confirm_token)
    if args.command == "status":
        if not args.wait:
            return service.status(args.job_id)
        job = service.status(args.job_id)
        while job["status"] not in TERMINAL:
            job = service.status(args.job_id, wait=30)
        return job
    if args.command == "fetch":
        return service.fetch(args.job_id, args.out)
    if args.command == "cancel":
        return service.cancel(args.job_id)
    if args.command == "render":
        return _render(service, args)
    raise AssertionError(args.command)


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "mcp":
        return mcp_server.main()
    try:
        result = _run(Service(), args)
    except RenderError as ex:
        print(json.dumps(ex.as_dict(), indent=2, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0
