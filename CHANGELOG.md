# Changelog

## 0.2.2

- **Relaxed hands.** `motion_apply` (and the `apply.bpy` snippet) give the rig's fingers a relaxed pose when the
  motion doesn't animate them, as `motion_from_text` never does: each finger curls a little toward the palm, more
  toward the pinky, and the thumb turns slightly in, instead of the rig's flat rest. The curl is read from each
  finger's own bones, so Mixamo, Rigify, Unreal-style, Auto-Rig Pro and other humanoid rigs all bend the right
  way. Fingers the scene already keys are left as they are; fingers a video motion animates follow the video.
- **Motion guidance.** The tool descriptions, the skill and `motion_apply`'s next step say: one simple action per
  prompt, describe what the motion actually does, re-prompt or say what it lacks, and never keyframe the root,
  hips or whole body over it.
- **Retargeting fixes from the add-on** (vendored `motion_retarget.py`): the generated skeleton's jaw and eye
  bones are short bones in the head; the hips no longer run away on rigs whose travel bone doesn't carry the
  thighs; a bake that moves no deforming bone is undone and refused with what to do, and limbs left behind are
  named.
- **Renders of rigged characters** (vendored `bundles.py`): drivers with simple expressions pack as they are. A
  driver that needs Python, or keys on a driver's own settings, is refused before anything is uploaded, named,
  with what to do.

## 0.2.1

- `motion_from_text` at 2 credits a second. Offline catalog snapshot; the free-plan skill copy and the
  `image_to_3d` prompt limit.

## 0.2.0

- Motion tools: `motion_from_video` (the video measured and uploaded) and `motion_from_text`, client-only
  `apply_args`, and `motion_apply` / `loopcut motion-apply` with the add-on's retargeter.

Earlier releases: see the repository history.
