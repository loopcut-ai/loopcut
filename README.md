# loopcut-render

Render `.blend` files on cloud RTX 4090s from Claude Code, Codex, Cursor, Claude Desktop or a
shell, and get the frames back. It's paid with your [Loopcut](https://loopcut.org/render) account's
credits. You don't need the Loopcut app; any Blender install packs the scene.

<!-- mcp-name: io.github.loopcut-ai/loopcut-render -->

It's one MCP server plus a CLI with the same verbs:

| Tool | CLI | What it does |
|---|---|---|
| `loopcut_login` | `login` | Signs you in with a browser link and stores the key where only you can read it |
| `loopcut_account` | `account` | Shows the plan, the credit balance and whether render is available |
| `render_quote` | `quote` | Packs the saved scene with headless Blender, uploads what the cloud lacks and prices the job. Spends nothing |
| `render_start` | `start` | Starts the quoted job, which spends credits. Calling it again returns the same job |
| `render_status` | `status` | Shows progress; `wait_seconds` waits for the job to finish |
| `render_fetch` | `fetch` | Downloads the PNG frames and `qa_report.json`; resumable, and every file is hash-checked |
| `render_cancel` | `cancel` | Cancels the job and refunds unspent credits |
| — | `render` | Quotes, starts, waits and fetches in one command (`--yes` or `--max-credits N`) |

**Price.** 1 credit = $0.01. Rendering costs 5 credits per GPU-minute on an RTX 4090, with a
one-minute minimum. The quote is the most a job can cost: you're charged for the GPU time the job
actually used, never more than the quote. Quotes, packing and the QA report are free. New
accounts start with 100 credits.

**Limits.** At most 2,000 frames per job, 4096×4096 pixels, 20 GiB per bundle and 200 MB per
file. The cloud renders with Blender 5.2, in Cycles or EEVEE.

## Install

You need [uv](https://docs.astral.sh/uv/) (it provides Python 3.10+), git and a Blender install.

**Claude Code** (plugin, which includes a skill that teaches the agent the workflow)

```
/plugin install loopcut-render --marketplace loopcut-ai/loopcut-render
```

On Claude Code before 2.1.275, run `/plugin marketplace add loopcut-ai/loopcut-render` and then
`/plugin install loopcut-render@loopcut`. Or add only the MCP server:

```sh
claude mcp add loopcut-render -- uvx --from git+https://github.com/loopcut-ai/loopcut-render loopcut-render mcp
```

**Codex**

```sh
codex mcp add loopcut-render -- uvx --from git+https://github.com/loopcut-ai/loopcut-render loopcut-render mcp
```

To add the skill, copy `skills/cloud-render` into `~/.codex/skills/`. Every tool call returns
within 50 s, under Codex's default 60 s tool timeout; long work carries on between calls.

**Cursor, VS Code, Claude Desktop and other MCP clients**

```json
{
  "mcpServers": {
    "loopcut-render": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/loopcut-ai/loopcut-render", "loopcut-render", "mcp"]
    }
  }
}
```

Apps opened from the Dock or Start menu may not see your shell's `PATH`. If the server doesn't
start, use the full path from `which uvx` as `command`.

**Shell, scripts and CI**

```sh
uvx --from git+https://github.com/loopcut-ai/loopcut-render loopcut-render login
uvx --from git+https://github.com/loopcut-ai/loopcut-render loopcut-render render shot.blend --frames 1-120 --samples 256 --max-credits 300
```

In CI, create a key on [loopcut.org/account](https://loopcut.org/account) and set
`LOOPCUT_API_KEY`.

## Settings

| Variable | Meaning |
|---|---|
| `LOOPCUT_API_KEY` | A Loopcut key (`lc_…`) to use instead of the stored sign-in, e.g. in CI |
| `LOOPCUT_BLENDER` | The Blender executable used for packing. Without it, the tool checks `BLENDER`, then `PATH`, then the usual install folders |
| `LOOPCUT_SERVICE_URL` | The service, `https://loopcut.org` by default |
| `LOOPCUT_RENDER_HOME` | Where the sign-in, quotes and jobs are kept. Default: `~/.config/loopcut-render`, or `%APPDATA%\loopcut-render` on Windows |

## How it works

1. **Pack.** A background Blender (`-b --factory-startup --disable-autoexec`) opens the saved
   `.blend` and runs the Loopcut add-on's own collector (`addon/bundles.py`, vendored). It copies
   every texture, library, cache and sequencer file into a staging folder with relative paths.
   That Blender gets a private config folder and none of your environment's keys.
2. **Upload.** Files are addressed by SHA-256, and only the ones the cloud doesn't already have
   are uploaded, straight to storage with presigned URLs. Big files go in parts, and each part is
   retried on its own. The bundle manifest goes last, and the job refers to it by hash.
3. **Quote and job.** This is the same job API the Loopcut add-on uses. The quote prices the
   exact request and holds that many credits; the job charges actual GPU time, and failed jobs are
   refunded.
4. **Fetch.** Frames and the QA report are checked against their hashes and moved into
   `loopcut_renders/<job_id>/` next to the `.blend`. The report flags black, white, flat and
   transparent frames, fireflies, duplicate frames and exposure jumps.

The cloud never runs Python from your file. Drivers with expressions, startup scripts and add-ons
are off, and the renderer has no internet access.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests       # MCP protocol and unit tests
LOOPCUT_TEST_BLENDER=/path/to/blender PYTHONPATH=src python3 -m unittest tests.test_blender_pack
```

The end-to-end tests run against Loopcut's contract mock, which lives in the service repository;
they're skipped here. `addon/VENDORED.md` explains how the add-on copies are kept in sync.

Released under the GPL-3.0-or-later. Loopcut is an independent project; Blender is a trademark of
the Blender Foundation.
