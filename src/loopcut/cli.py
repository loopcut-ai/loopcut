"""loopcut: the same calls as the MCP tools, for shells, scripts and CI.

  loopcut login | logout | account | tools
  loopcut settings [--private-mode on|off] [--no-generation on|off]
  loopcut quote TOOL [--input JSON] [--file FIELD=PATH …]          price a request; spends nothing
  loopcut run TOOL [--input JSON] [--file FIELD=PATH …] [--yes | --max-credits N] [--out DIR]
  loopcut start QUOTE_ID | status JOB_ID [--wait] | fetch JOB_ID [--out DIR] | cancel JOB_ID
  loopcut render scene.blend [--frames 1-120] [--samples 256] … [--yes | --max-credits N]
  loopcut mcp                                                       stdio MCP server

TOOL is a name from `loopcut tools`, e.g. asset_search, image_to_3d, remesh, sound_effect,
voice_line; render takes --blend. Results print as JSON on stdout; progress and prompts go to stderr.
"""

import argparse
import contextlib
import json
import re
import sys

from . import __version__, mcp_server
from . import catalog as catalogs
from .service import RENDER, TERMINAL, LoopcutError, Service


def _frames(value):
    match = re.fullmatch(r"(-?\d+)(?:-(-?\d+))?(?::(\d+))?", value)
    if not match:
        raise argparse.ArgumentTypeError("use N, START-END or START-END:STEP")
    start = int(match[1])
    result = {"frame_start": start, "frame_end": int(match[2]) if match[2] else start}
    if match[3]:
        result["frame_step"] = int(match[3])
    return result


def _json_object(value):
    try:
        parsed = json.loads(value)
    except ValueError as ex:
        raise argparse.ArgumentTypeError(f"not JSON: {ex}") from None
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object, e.g. '{\"prompt\": \"door slam\"}'")
    return parsed


def _file(value):
    field, sep, path = value.partition("=")
    if not sep or not field or not path:
        raise argparse.ArgumentTypeError("use FIELD=PATH, e.g. image=photo.png")
    return field, path


def _switch(value):
    if value not in {"on", "off"}:
        raise argparse.ArgumentTypeError("use on or off")
    return value == "on"


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


def _tool_options(parser):
    parser.add_argument("tool", help="tool name from `loopcut tools`")
    parser.add_argument("--input", type=_json_object, default={}, help="the tool's inputs as a JSON object")
    parser.add_argument("--file", type=_file, action="append", default=[], metavar="FIELD=PATH",
                        help="a file input as a local path, e.g. image=photo.png")
    parser.add_argument("--blend", help="render only: the saved .blend file")
    parser.add_argument("--blender", help="render only: Blender executable to pack with")
    parser.add_argument("--confirm-token", help="token from an approved daily_limit_reached or rate_limited refusal")


def _approve(parser):
    approve = parser.add_mutually_exclusive_group()
    approve.add_argument("--yes", action="store_true", help="start without asking")
    approve.add_argument("--max-credits", type=int, help="start without asking if the quote is at most this")


def _parser():
    parser = argparse.ArgumentParser(prog="loopcut", description="Loopcut's cloud tools for Blender: render, asset "
                                     "search, image to 3D, remesh, sound and voice, paid with Loopcut credits.")
    parser.add_argument("--version", action="version", version=f"loopcut {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="sign in to Loopcut in the browser")
    commands.add_parser("logout", help="forget the stored Loopcut key")
    commands.add_parser("account", help="plan, credits, settings and which tools the account can use")
    commands.add_parser("tools", help="the open cloud tools and their inputs")
    settings = commands.add_parser("settings", help="show or change Private mode and no-generation")
    settings.add_argument("--private-mode", type=_switch, metavar="on|off")
    settings.add_argument("--no-generation", type=_switch, metavar="on|off")
    _tool_options(commands.add_parser("quote", help="price a request (spends nothing)"))
    run = commands.add_parser("run", help="quote, start, wait and download in one go")
    _tool_options(run)
    run.add_argument("--out", help="folder to save into")
    _approve(run)
    start = commands.add_parser("start", help="start a quoted job (spends credits)")
    start.add_argument("quote_id")
    start.add_argument("--confirm-token")
    status = commands.add_parser("status", help="job status")
    status.add_argument("job_id")
    status.add_argument("--wait", action="store_true", help="wait until the job finishes")
    fetch = commands.add_parser("fetch", help="download a finished job's results")
    fetch.add_argument("job_id")
    fetch.add_argument("--out", help="folder to save into (default: loopcut_outputs/<job>, renders next to the .blend)")
    cancel = commands.add_parser("cancel", help="cancel a job; unspent credits are refunded")
    cancel.add_argument("job_id")
    render = commands.add_parser("render", help="render a .blend: quote, start, wait and download")
    _render_options(render)
    render.add_argument("--out", help="folder to save into")
    _approve(render)
    commands.add_parser("mcp", help="run the stdio MCP server (for Claude Code, Codex, Cursor, Claude Desktop)")
    return parser


def _say(message):
    print(message, file=sys.stderr, flush=True)


def _approved(quote, args):
    if quote["credits"] == 0 or args.yes or (args.max_credits is not None and quote["credits"] <= args.max_credits):
        return True
    if args.max_credits is not None or not sys.stdin.isatty():
        _say(f"Quote {quote['credits']} credits is over the limit or needs approval; not started.")
        return False
    what = f"{quote['frames']} frame(s)" if "frames" in quote else quote["tool"]
    answer = input(f"Run {what} for at most {quote['credits']} credits (${quote['usd']:.2f})? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def _quote(service, args):
    if catalogs.agent_name(args.tool) == catalogs.agent_name(RENDER) or args.tool == RENDER:
        if not args.blend:
            raise LoopcutError("render needs --blend PATH (or use: loopcut render PATH).", "invalid_input", {"field": "blend"})
        return service.quote(args.blend, args.input, blender=args.blender, confirm_token=args.confirm_token)
    return service.quote_tool(args.tool, args.input, dict(args.file), confirm_token=args.confirm_token, wait=None)


def _until_done(service, job, out):
    while job["status"] not in TERMINAL:
        progress = job.get("progress") or {}
        _say(f"{job['status']} {progress.get('pct', 0)}% {progress.get('message', '')}".rstrip())
        job = service.status(job["job_id"], wait=30)
    if job["status"] != "succeeded":
        return job
    return {**job, **service.fetch(job["job_id"], out)}


def _run_quote(service, quote, args):
    for warning in quote.get("warnings") or []:
        _say("warning: " + warning)
    if quote.get("status") != "quoted" or not _approved(quote, args):
        return quote
    job = service.start(quote["quote_id"], confirm_token=args.confirm_token, wait=0, out_dir=args.out, download=False)
    return _until_done(service, job, args.out)


def _tools(service):
    rows = []
    for descriptor in service.catalog().visible():
        schema = descriptor["input_schema"]
        required = set(schema.get("required") or [])
        rows.append({"tool": catalogs.agent_name(descriptor["name"]), "title": descriptor.get("title"),
                     "inputs": [name + ("*" if name in required else "") for name in schema.get("properties") or {}],
                     "files": [spec["field"] + ("*" if spec.get("required") else "")
                               for spec in descriptor.get("file_inputs") or []],
                     "usual_credits": (descriptor.get("cost") or {}).get("credits_estimate")})
    return {"tools": rows, "catalog": service.catalog().source, "note": "* required. Quotes give the exact price."}


def _sign_in(service):
    result = service.login(wait=0)
    while not result.get("signed_in"):
        _say(f"Open {result['verification_url']} and approve code {result['user_code']}. Waiting…")
        result = service.login(wait=60)
    return result


def _run_signed_in(service, args):
    """Run the command; signed out at a terminal (and without LOOPCUT_API_KEY), sign in first and go on."""
    try:
        return _run(service, args)
    except LoopcutError as ex:
        if ex.code not in {"sign_in_required", "invalid_api_key"} or args.command == "login" \
                or not service.can_sign_in() or not sys.stdin.isatty():
            raise
    _say("Not signed in to Loopcut yet; opening the sign-in page.")
    _sign_in(service)
    return _run(service, args)


def _run(service, args):
    command = args.command
    if command == "login":
        return _sign_in(service)
    if command == "logout":
        return service.logout()
    if command == "account":
        return service.account()
    if command == "tools":
        return _tools(service)
    if command == "settings":
        return service.settings(private_mode=args.private_mode, no_generation=args.no_generation)
    if command == "quote":
        return _quote(service, args)
    if command == "run":
        quote = _quote(service, args)
        if quote.get("status") != "quoted":
            return quote  # A free tool already ran through.
        return _run_quote(service, quote, args)
    if command == "start":
        return service.start(args.quote_id, confirm_token=args.confirm_token, wait=0, download=False)
    if command == "status":
        if not args.wait:
            return service.status(args.job_id)
        job = service.status(args.job_id)
        while job["status"] not in TERMINAL:
            job = service.status(args.job_id, wait=30)
        return job
    if command == "fetch":
        return service.fetch(args.job_id, args.out)
    if command == "cancel":
        return service.cancel(args.job_id)
    if command == "render":
        quote = service.quote(args.blend, _settings(args), blender=args.blender, confirm_token=args.confirm_token)
        return _run_quote(service, quote, args)
    raise AssertionError(command)


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "mcp":
        return mcp_server.main()
    output = sys.stdout
    try:
        with contextlib.redirect_stdout(sys.stderr):  # Only the JSON result goes to stdout.
            result = _run_signed_in(Service(), args)
    except LoopcutError as ex:
        print(json.dumps(ex.as_dict(), indent=2, ensure_ascii=False), file=output)
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, indent=2, ensure_ascii=False), file=output)
    return 0
