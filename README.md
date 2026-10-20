# loopcut

Loopcut's cloud tools for Claude Code, Codex, Cursor, Claude Desktop and the shell. Render `.blend` files on
cloud RTX 4090s, search free asset libraries, turn images into 3D models, remesh meshes, make sound
effects and voice lines, and turn a video or a sentence into character animation on your own rig. Results
come back as files the agent imports with Blender's Python. Everything is
paid from your [Loopcut](https://loopcut.org/render) account's credits. You don't need the Loopcut app;
any Blender install packs the scene.

<!-- mcp-name: io.github.loopcut-ai/loopcut -->

It's one MCP server plus a CLI with the same verbs. The tools come from Loopcut's catalog, so a cloud tool
that opens on Loopcut shows up without a new release.

| Tool | What it does | Result |
|---|---|---|
| `loopcut_login` | Signs in or switches account. Any tool does it by itself the first time: a browser opens, you approve, and the call carries on | |
| `loopcut_account` | Plan, credit balance, Private mode and no-generation settings, and which tools you can use | |
| `render_quote` | Packs the saved scene with headless Blender, uploads what the cloud lacks and prices the render | PNG frames and a QA report |
| `asset_search` | Searches Poly Haven, ambientCG and Physically Based. Free, and it runs at once | Matches with licence and download details |
| `image_to_3d` | Makes a textured model from a prompt, an image or both | GLB |
| `remesh` | Rebuilds a GLB mesh with clean quads or triangles and bakes its textures onto it | FBX (quads) or GLB (triangles) |
| `sound_effect` | Makes a sound effect from a description | WAV |
| `voice_line` | Speaks a line with a chosen voice and direction | WAV |
| `job_start` | Starts a quoted job, which spends credits. Calling it again returns the same job | |
| `job_status` | Waits for a job and downloads its results, hash-checked and resumable | |
| `job_cancel` | Cancels a job and refunds unspent credits | |

Image generation and image editing appear as `image_generate` and `image_edit` once they open.

### Motion

Two motion tools appear once they open on Loopcut, with a free local tool to use their results:

| Tool | What it does | Price | Result |
|---|---|---|---|
| `motion_from_video` | Captures a person's whole-body motion, hands included, from a local MP4, MOV or WebM (NVIDIA GEM-X). One person, whole body in view, a mostly still camera; at most 60 s per job, chosen with `start_s` and `end_s`, from a file up to 200 MB | 3 credits per second captured, 2-second minimum | `motion.bvh` + `motion.json` |
| `motion_from_text` | Makes 2–10 s of body animation from a description of one action (NVIDIA Kimodo, **Built with Meta Llama 3**). The fingers stay still; it isn't for faces, props or finger work | 5 credits per second of motion | `motion.bvh` + `motion.json` |
| `motion_apply` | Retargets a result onto the humanoid rig in a saved `.blend` with your Blender, headless, and saves the file. Mixamo, Rigify, Auto-Rig Pro, Unreal mannequin, or any humanoid by bone names; with no rig, SOMA's own skeleton with a simple body. Free and local | free | the animated `.blend` |

`motion.bvh` is a rig-neutral SOMA 77-joint skeleton (standing T-pose rest, centimetres, Y up, 30 fps).
The connector measures the clip's length itself (MP4 and MOV from the file header, WebM with `ffprobe`
when it's installed) because the quote holds the seconds captured. `target_armature` and `in_place` on a
motion tool are kept on your machine for `motion_apply` and never sent to Loopcut. `job_status` returns
the result with three ways to use it: `motion_apply` (or `loopcut motion-apply`) for a saved `.blend`,
a short Python snippet for a Blender that's already open (for example through another Blender MCP),
and Blender's plain BVH import. The retargeting is the Loopcut add-on's own, so results match the app.
With no-generation on, `motion_from_video` stays available (it records the user's own performance) and
`motion_from_text` is hidden.

**Price.** 1 credit = $0.01. Every paid tool quotes first, for free, and the quote is the most a job can
cost. Rendering costs 5 credits per GPU-minute on an RTX 4090 with a one-minute minimum; the other prices
are on [loopcut.org/pricing](https://loopcut.org/pricing). Asset search, quotes, packing and the render QA
report are free. Failed jobs are refunded. New accounts start with 100 credits.

## Install

You need [uv](https://docs.astral.sh/uv/) (it provides Python 3.10+), and Blender if you render.

**Claude Code** (plugin, which includes a skill that teaches the agent the tools and the habits that save
credits)

```
/plugin install loopcut --marketplace loopcut-ai/loopcut
```

On Claude Code before 2.1.275, run `/plugin marketplace add loopcut-ai/loopcut` and then
`/plugin install loopcut@loopcut`. Or add only the MCP server:

```sh
claude mcp add loopcut -- uvx loopcut mcp
```

**Codex** (plugin, with the same skill)

```sh
codex plugin marketplace add loopcut-ai/loopcut
codex plugin add loopcut@loopcut
```

Or add only the MCP server:

```sh
codex mcp add loopcut -- uvx loopcut mcp
```

Every tool call returns within 50 s, under Codex's default 60 s tool timeout; long work carries on between
calls. Both plugins start the server with `uvx loopcut==<release> mcp`, pinned to the release they ship with; the
package has no dependencies beyond Python.

**Cursor, VS Code, Claude Desktop and other MCP clients**

```json
{
  "mcpServers": {
    "loopcut": {
      "command": "uvx",
      "args": ["loopcut", "mcp"]
    }
  }
}
```

Apps opened from the Dock or Start menu may not see your shell's `PATH`. If the server doesn't start, use
the full path from `which uvx` as `command`.

The first call opens a browser to sign in, and the key is kept where only you can read it. Without a
browser (over SSH, say), the call returns the link for you to open. `LOOPCUT_API_KEY` skips all of this.

## CLI

```sh
loopcut login                                         # once per machine
loopcut account                                       # plan, credits, settings
loopcut tools                                         # the open tools and their inputs
loopcut render shot.blend --frames 1-120 --samples 256 --max-credits 300
loopcut run asset_search --input '{"query": "oak floor", "kind": "material"}'
loopcut run image_to_3d --input '{"prompt": "a brass desk lamp", "quality": "draft"}' --file image=lamp.png --max-credits 30
loopcut quote sound_effect --input '{"prompt": "a heavy wooden door slams", "duration_seconds": 2}'
loopcut run motion_from_video --file video=take.mp4 --input '{"static_camera": true}' --blend shot.blend --max-credits 60
loopcut run motion_from_text --input '{"prompt": "a person jumps over a puddle", "duration_s": 4}' --yes
loopcut motion-apply --blend shot.blend --bvh loopcut_outputs/JOB_ID/motion.bvh --armature Hero --in-place
loopcut motion-apply --bvh loopcut_outputs/JOB_ID --bpy        # Python for a Blender that's already open
loopcut start QUOTE_ID | status JOB_ID --wait | fetch JOB_ID --out DIR | cancel JOB_ID
loopcut settings --private-mode off                   # or on; also --no-generation on|off
```

Install the command with `uv tool install loopcut`, or run any command without installing as `uvx loopcut …`.
`run` and `render` quote, then start only with `--yes`, under `--max-credits N`, or after you answer the
prompt. `run` of a motion tool with `--blend` also puts the motion on that scene's rig; `motion-apply`
saves over `--blend` (Blender keeps the previous file as `.blend1`) unless you give `--out`. Results print as JSON on stdout; progress goes to stderr. In CI, create a key on
[loopcut.org/account](https://loopcut.org/account) and set `LOOPCUT_API_KEY`.

## Settings

| Variable | Meaning |
|---|---|
| `LOOPCUT_API_KEY` | A Loopcut key (`lc_…`) to use instead of the stored sign-in, e.g. in CI |
| `LOOPCUT_BLENDER` | The Blender executable used for packing and for `motion_apply`. Without it, the tool checks `BLENDER`, then `PATH`, then the usual install folders |
| `LOOPCUT_SERVICE_URL` | The service, `https://loopcut.org` by default |
| `LOOPCUT_CONNECTOR_HOME` | Where the sign-in, quotes, jobs and the last tool catalog are kept. Default: `~/.config/loopcut-connector`, or `%APPDATA%\loopcut-connector` on Windows. It is separate from the Loopcut app's own files |

**Private mode and no-generation.** These account settings apply here too. Private mode (on by default on
paid plans) refuses tools whose provider may train on what it receives; today that is sound effects.
No-generation hides and refuses the generative tools. Change them in the Loopcut app or with
`loopcut settings`.

## How it works

1. **Quote.** Every tool goes through the same job API the Loopcut add-on uses. A quote prices the exact
   request and lasts ten minutes; an expired one is renewed at start, and the job goes ahead only if the
   price didn't change. Starting the same quote twice returns the same job.
2. **Files in.** File inputs are local paths. They're checked (type and size), addressed by SHA-256, and
   only uploaded if the cloud doesn't already have them, straight to storage with presigned URLs. Big
   files go in parts, each retried on its own.
3. **Render packing.** A background Blender (`-b --factory-startup --disable-autoexec`) opens the saved
   `.blend` and runs the Loopcut add-on's own collector (`addon/bundles.py`, vendored). It copies every
   texture, library, cache and sequencer file into a bundle with relative paths. That Blender gets a
   private config folder and none of your environment's keys. The cloud never runs Python from your file,
   and the renderer has no internet access.
4. **Files out.** Results are checked against their hashes and saved into `loopcut_renders/<job_id>/`
   next to the `.blend` for a render, and `loopcut_outputs/<job_id>/` in the working folder for everything
   else (or `out_dir`). The working folder is the client's workspace root when it shares one; Codex starts
   plugin servers in the plugin's own folder, so there results go under your home folder instead. A
   render's QA report flags black, white, flat and transparent frames, fireflies, duplicate frames and
   exposure jumps.
5. **Motion onto a rig.** `motion_apply` starts the same kind of background Blender on your saved `.blend`
   and runs the add-on's retargeter (`addon/motion_retarget.py`, vendored) through
   `loopcut/motion_blender.py`: one whole-body rest alignment, limb-only direction alignment for A-pose
   rigs, the pelvis path scaled by leg length, and keys written parent first. A previous action on the rig
   is kept in a muted NLA track. Nothing leaves your machine and it costs nothing.

If the catalog can't be read at startup, the server uses the last catalog it read from that service, then
the snapshot in `src/loopcut/catalog.json`.

**What it runs and connects to.** `uv`, which provides Python (and downloads it if needed); your Blender,
headless, only to pack a scene for `render_quote` and to apply a motion with `motion_apply`; `ffprobe`, if
installed, only to measure a WebM clip; and your browser, only to open the Loopcut sign-in
page. It connects to `loopcut.org` (the catalog, sign-in and job API) and to the presigned storage URLs
that API hands out for uploads and downloads. It sends no analytics.

## Privacy Policy

Loopcut's privacy policy is at [loopcut.org/privacy](https://loopcut.org/privacy). For this connector:

- **What it sends.** The Loopcut key on every request, with the client's name and version. For a render,
  the packed scene: the saved `.blend` and every texture, library and cache it uses. For other tools, the
  inputs you or the agent give (prompts, settings) and the files named as inputs (images, meshes, videos).
  It reads nothing else from your machine: no other files, no environment keys besides `LOOPCUT_API_KEY`,
  no open Blender session. `motion_apply` opens and saves the `.blend` you name on your machine only, and
  `target_armature` and `in_place` stay local too.
- **Where it's stored.** Loopcut's database (Supabase, US) holds your account, quotes and jobs; uploaded
  files and results are kept in Cloudflare R2.
- **Third parties.** Each job goes only to the provider that runs it: today OpenAI, fal and the model
  developers it runs (Deemos for Rodin, Tripo), Google Cloud, ElevenLabs and RunPod. Providers' own
  retention terms are on the privacy page. Private mode, on by default for paid plans, refuses tools whose
  provider may train on what it receives (today, sound effects).
- **Retention.** Uploaded files are deleted 7 days after their last use and results 30 days after theirs.
  Account and billing records are kept while the account exists. Results you download are ordinary files
  on your machine.
- **On your machine.** The key, quotes, jobs and the last tool catalog are kept in
  `~/.config/loopcut-connector`, readable only by you. `loopcut logout` removes the key.
- **Contact.** hello@loopcut.org, also for exporting or deleting your account data.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests       # MCP protocol and unit tests
LOOPCUT_TEST_BLENDER=/path/to/blender PYTHONPATH=src python3 -m unittest tests.test_blender_pack
python3 scripts/snapshot_catalog.py                         # refresh the fallback catalog before a release
```

The end-to-end tests run against Loopcut's contract mock, which lives in the service repository; they're
skipped here. `addon/VENDORED.md` explains how the add-on copies are kept in sync.

Released under the GPL-3.0-or-later. Loopcut is an independent project; Blender is a trademark of the
Blender Foundation.
