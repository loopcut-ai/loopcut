"""Put a Loopcut motion result (motion.bvh + motion.json) on a humanoid armature. Runs inside Blender.

Headless, as `loopcut motion-apply` and the motion_apply tool run it:

    blender -b --factory-startup --disable-autoexec --python-exit-code 1 --python motion_blender.py -- REQUEST REPLY

REQUEST is a JSON file {blend, bvh, meta?, armature?, in_place?, frame_start?, title?, out}: the .blend is
opened, the motion applied, the file saved to out (Blender keeps the previous version as .blend1), and
REPLY gets {ok, result, armature, picked, frames, saved} or {ok: false, error}.

In a Blender that is already running (another Blender MCP, the Python console), with the scene open:

    import runpy
    apply_files = runpy.run_path(r"/path/to/loopcut/motion_blender.py")["apply_files"]
    print(apply_files(r"/path/to/motion.bvh", armature="Armature", in_place=False))

The retargeting is the add-on's own apply_motion (addon/motion_retarget.py, a verbatim copy): Mixamo,
Rigify deform bones, Auto-Rig Pro, Unreal mannequin, or any humanoid matched by bone names. Without an
armature named, it uses the active or only selected humanoid armature, then the only humanoid armature in
the scene; with none at all it imports SOMA's own skeleton with a simple body.
"""

import importlib.util
import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_META_BYTES = 1024 * 1024


def retarget_module():
    """addon/motion_retarget.py, loaded from beside this file (it needs no package)."""
    name = "loopcut_connector_motion_retarget"
    spec = importlib.util.spec_from_file_location(name, HERE / "addon" / "motion_retarget.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _read(path, limit):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError(f"{path.name} is larger than {limit // (1024 * 1024)} MB")
    return path.read_text(encoding="utf-8")


def find_target(mr, name=None):
    """(armature, how it was picked), or (None, why) for a new SOMA skeleton."""
    import bpy
    if name:
        armature = mr.armature_of(bpy.data.objects.get(name))
        if armature is None:
            raise mr.MotionError(f"{name!r} is not an armature, or a mesh deformed by one, in this file")
        if mr.rig_of(armature) is None:
            raise mr.MotionError(f"{armature.name!r} does not look like a humanoid rig (hips, legs, arms and head)")
        return armature, "named"
    view_layer = bpy.context.view_layer
    active = mr.armature_of(view_layer.objects.active)
    if active is not None and mr.rig_of(active) is not None:
        return active, "active object"
    selected = {mr.armature_of(ob) for ob in view_layer.objects if ob.select_get()} - {None}
    if len(selected) == 1:
        only = selected.pop()
        if mr.rig_of(only) is not None:
            return only, "selected object"
    humanoids = [ob for ob in bpy.context.scene.objects if ob.type == "ARMATURE" and mr.rig_of(ob) is not None]
    if len(humanoids) == 1:
        return humanoids[0], "only humanoid armature in the scene"
    if humanoids:
        names = ", ".join(sorted(repr(ob.name) for ob in humanoids)[:8])
        raise mr.MotionError(f"The scene has several humanoid armatures ({names}) and none is active; name one")
    return None, "no humanoid armature in the scene"


def apply_files(bvh, meta=None, *, armature=None, in_place=False, frame_start=None, title=None):
    """Apply motion.bvh (or a folder holding it) to the open scene; motion.json beside it is read too."""
    import bpy
    mr = retarget_module()
    bvh = Path(bvh).expanduser()
    if bvh.is_dir():
        bvh = bvh / "motion.bvh"
    meta_path = Path(meta).expanduser() if meta else bvh.with_suffix(".json")
    meta_text = _read(meta_path, MAX_META_BYTES) if meta_path.is_file() else ""
    target, picked = find_target(mr, armature)
    before = set(bpy.data.objects)
    line = mr.apply(_read(bvh, mr.MAX_BVH_BYTES), mr.read_meta(meta_text), target=target, title=title or "motion",
                    in_place=bool(in_place), frame_start=frame_start)
    if target is None:
        target = next((ob for ob in bpy.data.objects if ob not in before and ob.type == "ARMATURE"), None)
    scene = bpy.context.scene
    return {"result": line, "armature": target.name if target else None, "picked": picked,
            "scene_frames": [scene.frame_start, scene.frame_end], "fps": scene.render.fps / scene.render.fps_base}


def _main(argv):
    request_path, reply_path = Path(argv[0]), Path(argv[1])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        import bpy
        bpy.ops.wm.open_mainfile(filepath=request["blend"], load_ui=True)
        result = apply_files(request["bvh"], request.get("meta"), armature=request.get("armature"),
                             in_place=request.get("in_place") is True, frame_start=request.get("frame_start"),
                             title=request.get("title"))
        bpy.ops.wm.save_as_mainfile(filepath=request["out"], check_existing=False)
        reply = {"ok": True, **result, "saved": request["out"]}
    except Exception as ex:  # Reported to the connector, which words it for the agent.
        traceback.print_exc()
        reply = {"ok": False, "error": str(ex) or type(ex).__name__, "type": type(ex).__name__}
    reply_path.write_text(json.dumps(reply), encoding="utf-8")


if __name__ == "__main__":
    _main(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
