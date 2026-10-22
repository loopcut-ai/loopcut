"""Motion results (motion_from_video, motion_from_text) for agents with plain Blender.

A motion job returns motion.bvh (SOMA's 77-joint skeleton, standing T-pose rest, cm, Y up, 30 fps) and
motion.json. The add-on retargets it onto the user's rig as it applies the result; here the same code
(addon/motion_retarget.py, vendored) runs in a headless Blender child on a saved .blend (apply_blend, which
`loopcut motion-apply` and the motion_apply tool call), or inside a Blender that is already open through
the snippet bpy_snippet() prints. The child is started the way pack.py starts its collector: factory
settings, script autoexecution off, a private config and temp folder, and no Loopcut key in its
environment.

motion_from_video also needs the clip's length before the quote (video_seconds): the quote holds the
captured seconds, and the capture is at most 60 s. media.py measures it without Blender.
"""

import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from . import media, pack

SCRIPT = Path(__file__).resolve().parent / "motion_blender.py"
APPLY_TIMEOUT = 600  # Seconds for one headless apply: open, retarget up to 60 s of motion, save.
MAX_CAPTURE_SECONDS = 60  # SPEC §3.2 Motion: motion_from_video captures at most this much per job.
VIDEO_TOOL = "motion_from_video"
TOOLS = {VIDEO_TOOL, "motion_from_text"}
LOG_TAIL = 3000
BVH_IMPORT = ("bpy.ops.import_anim.bvh(filepath={path}, global_scale=0.01, axis_forward='-Z', axis_up='Y', "
              "update_scene_fps=True, update_scene_duration=True)")


class MotionError(ValueError):
    pass


def capture_inputs(inputs, video, mime):
    """The motion_from_video inputs with video_seconds measured from the local clip, checked against the
    60-second capture limit. A length the caller gave is replaced by the measured one when there is one."""
    inputs = dict(inputs)
    measured = media.video_seconds(video, mime)
    if measured is not None:
        inputs["video_seconds"] = round(measured, 3)
    elif not isinstance(inputs.get("video_seconds"), (int, float)):
        raise MotionError(f"Could not measure how long {Path(video).name} is ({_kind(mime)} needs ffprobe, which "
                          "isn't installed). Pass video_seconds: the clip's length in seconds.")
    seconds = inputs["video_seconds"]
    start = inputs.get("start_s") or 0
    if isinstance(start, (int, float)) and start >= seconds:
        raise MotionError(f"start_s ({start:g}) is past the end of {Path(video).name}, which is {seconds:.1f} s long.")
    end = inputs.get("end_s")
    if isinstance(end, (int, float)) and end >= seconds:
        del inputs["end_s"]  # The capture ends with the clip anyway; video_seconds holds no more than that.
        end = None
    end = end if isinstance(end, (int, float)) else seconds
    if isinstance(start, (int, float)) and end - start > MAX_CAPTURE_SECONDS:
        raise MotionError(f"{Path(video).name} is {seconds:.1f} s long, and one job captures at most "
                          f"{MAX_CAPTURE_SECONDS} s. Choose the part to capture with start_s and end_s "
                          f"(end_s - start_s <= {MAX_CAPTURE_SECONDS}), or trim the clip.")
    return inputs


def _kind(mime):
    return {"video/webm": "WebM"}.get(mime, "this video type")


def find_bvh(path):
    """motion.bvh given itself or the folder it is in."""
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path = path / "motion.bvh"
    if not path.is_file():
        raise MotionError(f"No motion.bvh at {path}. Pass the motion.bvh a motion job saved (job_status lists it).")
    with path.open("rb") as handle:
        if not handle.read(64).lstrip().startswith(b"HIERARCHY"):
            raise MotionError(f"{path.name} is not a BVH file.")
    return path


def bpy_snippet(bvh, *, armature=None, in_place=False, title=None):
    """Python for a Blender that is already open: applies the motion to the open scene, as motion_apply does."""
    options = [f"armature={armature!r}", f"in_place={bool(in_place)!r}"]
    if title:
        options.append(f"title={title!r}")
    return (f"import runpy\n"
            f"apply_files = runpy.run_path({str(SCRIPT)!r})['apply_files']\n"
            f"print(apply_files({str(bvh)!r}, {', '.join(options)}))")


def cli_command(bvh, *, blend=None, armature=None, in_place=False):
    parts = ["loopcut", "motion-apply", "--blend", str(blend) if blend else "SCENE.blend", "--bvh", str(bvh)]
    if armature:
        parts += ["--armature", armature]
    if in_place:
        parts.append("--in-place")
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def read_meta(path):
    """The motion.json fields worth showing the agent."""
    try:
        meta = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(meta, dict):
        return {}
    keep = {key: meta[key] for key in ("source", "fps", "frames", "seconds", "fingers_animated") if key in meta}
    if isinstance(meta.get("warnings"), list):
        keep["warnings"] = [str(w)[:120] for w in meta["warnings"][:8]]
    return keep


def result_for(files, apply_args=None, title=None):
    """What job_status adds for a finished motion job: the files, the clip facts and how to use them."""
    bvh = next((Path(path) for path in files if Path(path).suffix.lower() == ".bvh"), None)
    if bvh is None:
        return {"next": "The motion result has no motion.bvh; call job_status again."}
    sidecar = next((Path(path) for path in files if Path(path).suffix.lower() == ".json"), None)
    args = {key: value for key, value in (apply_args or {}).items() if key in {"target_armature", "in_place"}}
    armature, in_place = args.get("target_armature"), args.get("in_place") is True
    motion = {"bvh": str(bvh), **({"json": str(sidecar)} if sidecar else {}), **(read_meta(sidecar) if sidecar else {})}
    apply = {
        "motion_apply": {"bvh": str(bvh), **args},
        "cli": cli_command(bvh, armature=armature, in_place=in_place),
        "bpy": bpy_snippet(bvh, armature=armature, in_place=in_place, title=title),
        "plain_import": BVH_IMPORT.format(path=repr(str(bvh))),
    }
    fingers = (" The fingers don't move in this motion (text to motion animates the body only); motion_apply and "
               "apply.bpy give the rig's fingers a relaxed pose instead of its rest.") \
        if motion.get("fingers_animated") is False else ""
    return {"motion": motion, "apply": apply, "next": (
        "Done. To put the motion on the user's character in a saved .blend, call motion_apply with blend and "
        "apply.motion_apply's arguments: it retargets onto a Mixamo, Rigify, Auto-Rig Pro, Unreal or other humanoid "
        "rig in a headless Blender and saves the file (the previous version stays as .blend1). If that .blend is open "
        "in Blender (for example through another Blender MCP), run apply.bpy in that session instead and save there. "
        "apply.plain_import only brings in SOMA's own skeleton, 1 unit = 1 m." + fingers)}


def apply_blend(blend, bvh, *, armature=None, in_place=False, frame_start=None, title=None, out=None, blender=None,
                progress=None):
    """Run motion_blender.py in a headless Blender on a saved .blend and save it (to out, else in place)."""
    blend = Path(blend).expanduser().resolve()
    if not blend.is_file() or blend.suffix.lower() != ".blend":
        raise MotionError(f"Not a saved .blend file: {blend}")
    bvh = find_bvh(bvh)
    target = Path(out).expanduser().resolve() if out else blend
    if target.suffix.lower() != ".blend":
        raise MotionError(f"out must be a .blend path: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        executable = pack.find_blender(blender)
    except pack.PackError as ex:
        raise MotionError(str(ex)) from None
    with tempfile.TemporaryDirectory(prefix="loopcut-motion-") as temporary:
        control = Path(temporary)
        for name in ("tmp", "config", "scripts"):
            (control / name).mkdir()
        env = {name: os.environ[name] for name in pack.KEEP_ENV if os.environ.get(name)}
        env.update(TMPDIR=str(control / "tmp"), TEMP=str(control / "tmp"), TMP=str(control / "tmp"),
                   BLENDER_USER_CONFIG=str(control / "config"), BLENDER_USER_SCRIPTS=str(control / "scripts"))
        request, reply, log = control / "request.json", control / "reply.json", control / "blender.log"
        request.write_text(json.dumps({"blend": str(blend), "bvh": str(bvh), "armature": armature or None,
                                       "in_place": bool(in_place), "frame_start": frame_start, "title": title,
                                       "out": str(target)}), encoding="utf-8")
        command = [executable, "-b", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1",
                   "--python", str(SCRIPT), "--", str(request), str(reply)]
        if progress:
            progress(f"Applying {bvh.name} to {blend.name} in Blender")
        try:
            with log.open("wb") as output:
                completed = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                           env=env, timeout=APPLY_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise MotionError(f"Blender took longer than {APPLY_TIMEOUT} s to apply the motion") from None
        except OSError as ex:
            raise MotionError(f"Could not run Blender at {executable}: {ex.strerror or ex}") from None
        answer = json.loads(reply.read_text(encoding="utf-8")) if reply.is_file() else None
        if not isinstance(answer, dict):
            raise MotionError(f"Blender stopped before applying the motion (exit {completed.returncode}):\n" + _tail(log))
        if not answer.get("ok"):
            raise MotionError(f"The motion was not applied: {answer.get('error')}")
    backup = target.with_name(target.name + "1")
    return {"status": "applied", "blend": str(target), "result": answer.get("result"), "armature": answer.get("armature"),
            "picked": answer.get("picked"), "scene_frames": answer.get("scene_frames"), "blender": executable,
            **({"previous_version": str(backup)} if target == blend and backup.is_file() else {}),
            "next": ("Open the .blend (or revert it if Blender has it open) and play the animation; look at a few "
                     "frames before telling the user it is done, and describe what the motion actually does, not "
                     "what was asked. If a motion_from_text result misses the brief, run it again with a simpler prompt "
                     "(charged again); otherwise say what it lacks. Never keyframe the root, hips or whole body over it "
                     "to fake missing motion.")}


def _tail(log):
    try:
        return pack._tail(log)[-LOG_TAIL:]
    except OSError:
        return ""
