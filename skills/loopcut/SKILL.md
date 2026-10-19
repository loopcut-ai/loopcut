---
name: loopcut
description: Use Loopcut's cloud tools (the loopcut MCP server) for Blender work - render a .blend on cloud GPUs (render_quote), find free models, materials, HDRIs and textures (asset_search), make a 3D model from a prompt or image (image_to_3d), remesh a mesh (remesh), make sound effects (sound_effect) or voice lines (voice_line), then job_start and job_status. Use when the user wants a final or animated render, a turntable, real-world assets, a new prop, cleaner topology, or audio for a scene, and when the local machine is slow or Blender runs headless.
---

# Loopcut cloud tools

The `loopcut` tools run on Loopcut's cloud and bring results back as files. They're paid from the user's
Loopcut credits (1 credit = $0.01). The first call opens a browser to sign in, and carries on once the
user approves. If it returns a `verification_url` instead, show it to the user and call the same tool again
after they approve. `loopcut_login` signs in or switches accounts on purpose.

## Which tool

- **Real assets: `asset_search` first.** Free and instant: CC0 models, materials, HDRIs and textures
  from Poly Haven, ambientCG and Physically Based. Prefer a found asset over a generated one.
- **A new prop or concept mesh: `image_to_3d`** (prompt, a local reference image, or both). Use
  `quality: "draft"` while exploring; a final costs about three times as much.
- **Clean topology: `remesh`.** Export the object to `.glb` first
  (`bpy.ops.export_scene.gltf(filepath=..., export_format='GLB', use_selection=True)`) and pass the path
  as `model`. Pick `target_faces` by purpose: 2000-5000 props, 5000-10000 editing base, 10000-20000 hero.
- **Audio: `sound_effect`** for impacts, ambience and foley; **`voice_line`** for speech.
- **Final frames: `render_quote`.** Save the scene first: the cloud renders the file on disk.
- `image_generate` and `image_edit` appear when they open.

## Flow

1. Call the tool. Paid tools return a quote (`credits`, `usd`) and spend nothing. `asset_search` runs at
   once and returns its matches.
2. Tell the user the price. Call `job_start` only after they agree, or when the price is within a budget
   they already gave you.
3. `job_start` waits and downloads the result if it's quick; otherwise call `job_status` with
   `wait_seconds` until the result lists the files. A first render can spend minutes starting a GPU.
4. Results land in `loopcut_outputs/<job_id>/` in the working folder, or `loopcut_renders/<job_id>/` next
   to the `.blend`. Import them with bpy: `bpy.ops.import_scene.gltf` (GLB), `bpy.ops.import_scene.fbx`
   (quad remesh), a sound strip in the sequencer (WAV), `bpy.data.images.load` (PNG, textures, HDRIs).
5. Look at what came back (open a few frames, check the model) before telling the user it's done.

## Saving credits

- Before a long render, quote and run one frame at `resolution_pct: 25` with low `samples`, look at it,
  then quote the full job.
- Animations: frames default to frame 1 only. Pass `frame_start`/`frame_end` (the quote shows the scene's
  range) and `frame_step` for previews. Match the engine: `engine: "eevee"` for EEVEE scenes. Cycles
  needs a Pro or Max plan; on the free plan, ask before rendering a Cycles scene with EEVEE.
- `turntable: {}` orbits the whole scene; `turntable: {"targets": ["Chair"]}` frames chosen objects.
- Check the render quote's `warnings`: missing textures render pink. Fix them and quote again.
- Asset search results carry licence and credit lines; show `show_credit` with them.

## Errors

Errors carry a `code` and a message that says what to do. `insufficient_credits`: the user gets a plan,
or tops up on Pro or Max. `plan_required`: Cycles needs Pro or Max; offer EEVEE or the `upgrade` link. `age_confirmation_required`: they confirm once at `confirm_url`. `daily_limit_reached` or
`rate_limited` with a `confirm_token`: pass it back only if the user approves. `private_mode`: the
account's Private mode blocks that tool (today, sound effects); only the user can turn it off.
`generation_disabled`: generation is off for the account. Python in a `.blend` (drivers, startup scripts,
add-ons) doesn't run in the cloud. Failed jobs are refunded; a retry is a new quote.
