---
name: loopcut
description: Use Loopcut's cloud tools (the loopcut MCP server) for Blender work - render a .blend on cloud GPUs (render_quote), find free models, materials, HDRIs and textures (asset_search), make a 3D model from a prompt or image (image_to_3d), remesh a mesh (remesh), make sound effects (sound_effect) or voice lines (voice_line), capture character animation from a video (motion_from_video) or a description (motion_from_text) and put it on a rig (motion_apply), then job_start and job_status. Without a local Blender (claude.ai), render a scene from a bpy script with the hosted render tool. Use when the user wants a final or animated render, a turntable, real-world assets, a new prop, cleaner topology, audio for a scene, or a character animated from their own performance or a sentence, and when the local machine is slow or Blender runs headless.
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
- **Character animation: `motion_from_video` or `motion_from_text`, then `motion_apply`** (below).
- `image_generate` and `image_edit` appear when they open, and so do the motion tools.

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

## Motion: from a video or a sentence onto the user's rig

- **`motion_from_video`** when the user has a clip of the performance (their own take, a reference). Pass
  the local path as `video` (MP4, MOV or WebM, up to 200 MB). It works best with one person, the whole
  body in view, a mostly still camera (`static_camera: true` for a tripod) and good light; first-person
  footage, hidden hands and fast spins track poorly. One job captures at most 60 s: pick the part with
  `start_s`/`end_s`. Price: 3 credits per second captured, 2-second minimum. Leave `video_seconds` out:
  the connector measures the clip (it asks only for a WebM it can't measure). Hands are captured.
- **`motion_from_text`** when there's no footage: one simple physical action per prompt, in plain words
  about what the body does ("a person does a high kick with the right leg"), `duration_s` 2-10. Price: 2
  credits per second of motion. Good at grounded motion (walks, dances, kicks, gestures, fighting in place);
  weak at acrobatics (flips, spins in the air, big jumps come out small or missing), so say so up front.
  Each sentence becomes its own segment sharing the time, so a sequence is one short sentence per action
  (or separate calls) and never a trailing sentence of style words. The fingers stay still, and it doesn't
  do faces, props or touching objects: say so if the user needs them. Built with Meta Llama 3 (NVIDIA
  Kimodo's text encoder).
- With no-generation on, `motion_from_video` stays (it's the user's own performance) and
  `motion_from_text` is hidden and refused.
- Pass `target_armature` (the rig object, or the mesh it deforms) and `in_place` (no root travel, for loops
  and game clips) on the motion tool when you know them. They stay on this machine.
- The result is `motion.bvh` (a rig-neutral SOMA 77-joint skeleton, T-pose rest, cm, Y up, 30 fps) and
  `motion.json`. `job_status` returns `apply` with three ways to use it:
  - `motion_apply` with the saved `.blend` and `apply.motion_apply`'s arguments. It retargets onto Mixamo,
    Rigify, Auto-Rig Pro, Unreal mannequin or any humanoid in a headless Blender and saves the file (the
    old one stays as `.blend1`; `out` saves elsewhere). Free. Call it again while it says `applying`.
  - `apply.bpy`: Python for a Blender that is already open with the scene (for example another Blender
    MCP's execute-code tool). Use it instead of `motion_apply` when the user has the file open, then save.
  - `apply.plain_import`: Blender's own BVH import of the SOMA skeleton, when there's no rig.
- Check a few frames afterwards (feet on the ground, arms not crossing the body) and tell the user what the
  motion actually does, not what was asked. If it misses the brief, run `motion_from_text` again with a
  simpler prompt (charged again) or say what it made. Never fake missing body motion by keyframing the
  root, hips or whole body over the result; a small local touch-up (a hand pose, a held end pose) is fine
  if you say so.

## No local Blender (claude.ai, the Code tab): `render` from a script

When the hosted `loopcut-cloud` tools are connected (claude.ai chat, the Code tab, Cowork), or there's no
saved `.blend`, use the hosted `render` tool. It builds the scene from a bpy script on Loopcut's side, in an
isolated VM with no network, so nothing is uploaded.

1. Write one bpy script that builds the whole scene from an empty factory scene: geometry, materials,
   lights, `scene.camera`, `scene.render.resolution_x/y`, and keyframes for animation.
2. Load assets only through `from loopcut import scene as lc`:
   `lc.material("ambientcg:Bricks077", res="2K", scale=4)`,
   `lc.hdri("ambientcg:DayEnvironmentHDRI001", strength=1.0)`, and
   `lc.import_model("job:<job_id>/model.glb")` for a model `image_to_3d` made. Find ambientCG ids with
   `asset_search` (a result's `id`). List every asset in the call's `assets`.
3. If you have a code sandbox, run the script there first: `pip install bpy==5.2.2 loopcut`, then run it
   with `python`. Assets the sandbox can't download become stand-ins (`lc.standins()` lists them); a small
   Cycles CPU render checks the composition.
4. Call `render` with the script, `assets`, frames and engine. The quote comes back with a preview of the
   scene Loopcut built: look at it, show the user the price, then `job_start`.
5. A finished render attaches its first and last frames when they're small; otherwise use the links.

Limits: 64 KB of script, 25 s to run it, no scripted (Python expression) drivers, OSL or linked `.blend`
libraries.

## Saving credits

- Before a long render, quote and run one frame at `resolution_pct: 25` with low `samples`, look at it,
  then quote the full job.
- Animations: frames default to frame 1 only. Pass `frame_start`/`frame_end` (the quote shows the scene's
  range) and `frame_step` for previews. Match the engine: `engine: "eevee"` for EEVEE scenes. Cycles
  and EEVEE both run on any plan's credits.
- `turntable: {}` orbits the whole scene; `turntable: {"targets": ["Chair"]}` frames chosen objects.
- Check the render quote's `warnings`: missing textures render pink. Fix them and quote again.
- Asset search results carry licence and credit lines; show `show_credit` with them.

## Errors

Errors carry a `code` and a message that says what to do. `insufficient_credits`: the user gets a plan,
or tops up on Pro or Max. `age_confirmation_required`: they confirm once at `confirm_url`. `daily_limit_reached` or
`rate_limited` with a `confirm_token`: pass it back only if the user approves. `private_mode`: the
account's Private mode blocks that tool (today, sound effects); only the user can turn it off.
`generation_disabled`: generation is off for the account. A motion job that fails with `invalid_input`
(no person found, an unreadable video) is refunded: try a clearer clip. Python in a `.blend` (drivers, startup scripts,
add-ons) doesn't run in the cloud. Failed jobs are refunded; a retry is a new quote.
