---
name: cloud-render
description: Render a Blender scene on Loopcut's cloud GPUs with the loopcut-render MCP tools (render_quote, render_start, render_status, render_fetch). Use when the user wants a final or high-quality Blender render, an animation or frame range, or a turntable, and the local machine is slow or has no usable GPU, or when Blender runs headless from the shell.
---

# Cloud render with Loopcut

The `loopcut-render` tools render a saved `.blend` on an RTX 4090 in the cloud and download the
PNG frames. Any Blender install works; Loopcut itself is not needed. It is paid from the user's
Loopcut credits: 1 credit = $0.01, render costs 5 credits per GPU-minute with a one-minute
minimum. Quotes, packing and the QA report cost nothing.

## Flow

1. `loopcut_login` once per machine. If it returns a `verification_url`, show it to the user and
   call it again after they approve.
2. Save the scene. The cloud renders the file on disk, not unsaved edits in an open Blender.
3. `render_quote` with the `.blend` path and settings. It packs the scene with a local headless
   Blender and uploads what the cloud lacks. While it returns `"status": "preparing"`, call it
   again with the same arguments.
4. Tell the user the price (`credits`, `usd`) and the frame count. Start only after they agree,
   or when the price is within a budget they already gave you.
5. `render_start` with the `quote_id`. It returns a `job_id`.
6. `render_status` with `wait_seconds` until `succeeded`, `failed` or `canceled`. The first job in
   a while can spend several minutes starting a GPU worker; that is normal.
7. `render_fetch` downloads the frames and `qa_report.json` into `loopcut_renders/<job_id>/` next
   to the `.blend` (or `out_dir`). Open a few frames, including any the QA report flags, before
   telling the user the render is done.

## Settings that matter

- Frames default to frame 1 only. For an animation pass `frame_start`/`frame_end` (the quote shows
  the scene's own range) and `frame_step` for previews.
- Match the scene's engine: `engine: "eevee"` for EEVEE scenes; Cycles is the default.
- Cheap checks first: before a long animation, render one frame with `resolution_pct: 25` and
  low `samples`, look at it, then quote the full job.
- `turntable: {}` orbits the whole scene across the frame range (120 frames if no end is given);
  `turntable: {"targets": ["Chair"]}` frames chosen objects. Good for product and asset shots.

## Things to check

- `warnings` in the quote lists missing files: textures that are not on disk render pink.
  Fix the paths or pack the images in Blender, save, and quote again.
- Python in the `.blend` does not run in the cloud: drivers with expressions, startup scripts and
  add-ons are off. Bake anything that depends on them first.
- Errors carry a `code`. `insufficient_credits`: the user adds credits or upgrades on their Loopcut
  account page. `age_confirmation_required`: they confirm once at `confirm_url`.
  `daily_limit_reached`/`rate_limited` include a `confirm_token`; pass it back only if the user
  approves going past their limit.
- Failed jobs are refunded. A retry is a new quote and a new start.
