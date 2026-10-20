"""Asset helpers for bpy scripts that Loopcut's hosted `render` tool builds and renders.

One script runs unchanged in two places:

- Claude's code sandbox (``pip install bpy==5.2.2 loopcut``), to check it and preview it. Each asset
  downloads from its public source when the sandbox's network allows it. When it doesn't, a
  stand-in takes its place (a plain material, a grey sky, a 1 m box), so the script still runs;
  ``standins()`` lists what was replaced.
- Loopcut's scene builder, which has no network at all. Every asset the script uses must be listed
  in the render call's ``assets``: Loopcut downloads them before the script runs.

Asset references:

- ``"ambientcg:<AssetId>"``: an ambientCG material or HDRI (CC0), for example ``"ambientcg:Bricks077"``
  or ``"ambientcg:DayEnvironmentHDRI001"``. ``res`` is ``"1K"``, ``"2K"`` (the default) or, for HDRIs,
  ``"4K"``.
- ``"job:<job_id>/<output name>"``: a file one of the user's own Loopcut jobs made, for example the
  GLB from image_to_3d. Only the builder can read these; the sandbox uses a stand-in.

Example::

    import bpy
    from loopcut import scene as lc

    lc.hdri("ambientcg:DayEnvironmentHDRI001", strength=1.2)
    bpy.ops.mesh.primitive_plane_add(size=10)
    bpy.context.object.data.materials.append(lc.material("ambientcg:Bricks077", scale=4))
"""

import getpass
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ASSETS_ENV = "LOOPCUT_ASSETS"
"""Set by Loopcut's builder: the path of the index of assets downloaded for this build."""

AMBIENTCG_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
JOB_REF = re.compile(r"^job:(job_[A-Za-z0-9_-]{1,64})/([^/\\\x00-\x1f]{1,200})$")
MATERIAL_RES = ("1K", "2K")
HDRI_RES = ("1K", "2K", "4K")
MAP = re.compile(r"_(Color|NormalGL|NormalDX|Roughness|Metalness|Displacement|AmbientOcclusion|Opacity|Emission)\.(jpe?g|png)$", re.I)
HDR = re.compile(r"_HDR\.(exr|hdr)$", re.I)
MAX_ZIP_BYTES = 150 * 1024 ** 2
MAX_MEMBER_BYTES = 200 * 1024 ** 2
DOWNLOAD_TIMEOUT = 30
AMBIENTCG_HOSTS = ("ambientcg.com", "acg-download.struffelproductions.com")

_standins = []


class AssetError(RuntimeError):
    """An asset reference the builder can't use."""


def standins():
    """The asset references this run replaced with stand-ins (always empty in Loopcut's builder)."""
    return list(_standins)


def ambientcg_kind(asset_id):
    """ambientCG names every HDRI ...HDRI<number> (DayEnvironmentHDRI001); everything else here is a material."""
    return "hdri" if re.search(r"HDRI\d*$", asset_id) else "material"


def ambientcg_url(asset_id, res, kind):
    """The ambientCG download for a material (JPG maps) or an HDRI."""
    return f"https://ambientcg.com/get?file={asset_id}_{res}{'-JPG' if kind == 'material' else ''}.zip"


def parse_ref(ref):
    """("ambientcg", asset_id) or ("job", (job_id, output name)); raises AssetError."""
    if not isinstance(ref, str):
        raise AssetError("An asset reference is a string such as 'ambientcg:Bricks077'.")
    if ref.startswith("ambientcg:") and AMBIENTCG_ID.match(ref[10:]):
        return "ambientcg", ref[10:]
    match = JOB_REF.match(ref)
    if match:
        return "job", (match.group(1), match.group(2))
    raise AssetError(f"Unknown asset reference {ref!r}: use 'ambientcg:<AssetId>' or 'job:<job_id>/<output name>'.")


def index_key(ref, res=None):
    """The key an asset has in the builder's index."""
    return f"{ref}@{res}" if res else ref


def extract_maps(archive, dest):
    """Unpack an ambientCG zip's texture maps and HDR image (nothing else: no .blend, .usdc or
    .mtlx), each under its own base name. Returns {"maps": {name: path}} or {"file": path}."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    maps, hdr = {}, None
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = os.path.basename(info.filename)
            if info.is_dir() or not name or name.startswith(".") or info.file_size > MAX_MEMBER_BYTES:
                continue
            kind = MAP.search(name)
            if not kind and not HDR.search(name):
                continue
            target = dest / name
            with zf.open(info) as src, open(target, "wb") as out:
                left = MAX_MEMBER_BYTES
                while chunk := src.read(1 << 20):
                    left -= len(chunk)
                    if left < 0:
                        raise AssetError(f"{name} is larger than an asset map may be.")
                    out.write(chunk)
            if kind:
                maps.setdefault(kind.group(1).lower(), str(target))
            elif hdr is None or name.lower().endswith(".exr"):
                hdr = str(target)
    if hdr:
        return {"file": hdr}
    if not maps:
        raise AssetError("The asset download held no texture maps or HDR image.")
    return {"maps": maps}


def _index():
    path = os.environ.get(ASSETS_ENV)
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["assets"]


def _cache_dir():
    """A cache only this user can write: another local user can't plant entries in it."""
    root = Path(os.environ.get("LOOPCUT_SCENE_CACHE") or Path(tempfile.gettempdir()) / f"loopcut-scene-cache-{getpass.getuser()}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if hasattr(os, "getuid") and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o022):
        raise AssetError(f"{root} isn't a private cache folder.")
    return root


class _AmbientcgRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to ambientCG's own hosts, over https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.port or target.hostname not in AMBIENTCG_HOSTS:
            raise AssetError("The asset download redirected somewhere unexpected.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, path):
    request = urllib.request.Request(url, headers={"User-Agent": "loopcut-scene/1"})
    opener = urllib.request.build_opener(_AmbientcgRedirects)
    with opener.open(request, timeout=DOWNLOAD_TIMEOUT) as response, open(path, "wb") as out:
        left = MAX_ZIP_BYTES
        while chunk := response.read(1 << 20):
            left -= len(chunk)
            if left < 0:
                raise AssetError("The asset download is too large.")
            out.write(chunk)


def _resolve(ref, res, kind):
    """The asset's local files, or None for a stand-in (sandbox only)."""
    source, ident = parse_ref(ref)
    index = _index()
    if index is not None:
        entry = index.get(index_key(ref, res if source == "ambientcg" else None))
        if entry is None:
            raise AssetError(f"{ref}{f' at {res}' if source == 'ambientcg' else ''} is not in this build: "
                             "list it in the render call's assets.")
        return entry
    if source == "job":
        _standins.append(ref)
        return None
    folder = _cache_dir() / f"{ident}_{res}"
    try:
        if not folder.exists():
            archive = _cache_dir() / f"{ident}_{res}.zip"
            _download(ambientcg_url(ident, res, kind), archive)
            entry = extract_maps(archive, folder)
            archive.unlink(missing_ok=True)
            (folder / "entry.json").write_text(json.dumps(entry))
        entry = json.loads((folder / "entry.json").read_text())
        paths = list(entry.get("maps", {}).values()) + ([entry["file"]] if "file" in entry else [])
        if not paths or any(Path(path).resolve().parent != folder.resolve() for path in paths):
            raise AssetError("The cached asset points outside its folder.")
        return entry
    except Exception as error:  # noqa: BLE001 - any network or archive failure means a stand-in
        print(f"loopcut.scene: {ref} at {res} is a stand-in here ({type(error).__name__}); Loopcut's builder uses the real asset.")
        _standins.append(ref)
        return None


def _check_res(res, allowed):
    if res not in allowed:
        raise AssetError(f"res must be one of {', '.join(allowed)}.")


def _image(nodes, path, data, location):
    import bpy

    node = nodes.new("ShaderNodeTexImage")
    node.image = bpy.data.images.load(path, check_existing=True)
    if data:
        node.image.colorspace_settings.name = "Non-Color"
    node.location = location
    return node


def _node_tree(owner):
    if owner.node_tree is None:
        owner.use_nodes = True
    return owner.node_tree


def material(ref, res="2K", name=None, scale=1.0, displacement=False):
    """A Principled BSDF material from an ambientCG material: colour, roughness, normal, metalness and
    opacity maps, tiled `scale` times. With displacement=True the height map becomes bump."""
    import bpy

    _check_res(res, MATERIAL_RES)
    source, ident = parse_ref(ref)
    if source != "ambientcg":
        raise AssetError("material() takes an ambientCG reference, e.g. 'ambientcg:Bricks077'.")
    entry = _resolve(ref, res, "material")
    mat = bpy.data.materials.new(name or ident)
    tree = _node_tree(mat)
    nodes, links = tree.nodes, tree.links
    bsdf = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")
    if entry is None:
        mat.name = name or f"Stand-in {ident}"
        bsdf.inputs["Base Color"].default_value = (0.5, 0.5, 0.5, 1)
        return mat
    if "maps" not in entry:
        raise AssetError(f"{ref} is an HDRI, not a material: use hdri().")
    maps = entry["maps"]
    coords = nodes.new("ShaderNodeTexCoord")
    coords.location = (-1100, 0)
    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (-900, 0)
    mapping.inputs["Scale"].default_value = (scale, scale, scale)
    links.new(coords.outputs["UV"], mapping.inputs["Vector"])
    y = 300

    def texture(key, data):
        nonlocal y
        node = _image(nodes, maps[key], data, (-600, y))
        links.new(mapping.outputs["Vector"], node.inputs["Vector"])
        y -= 280
        return node

    if "color" in maps:
        links.new(texture("color", False).outputs["Color"], bsdf.inputs["Base Color"])
    if "roughness" in maps:
        links.new(texture("roughness", True).outputs["Color"], bsdf.inputs["Roughness"])
    if "metalness" in maps:
        links.new(texture("metalness", True).outputs["Color"], bsdf.inputs["Metallic"])
    if "opacity" in maps:
        links.new(texture("opacity", True).outputs["Color"], bsdf.inputs["Alpha"])
    normal_key = "normalgl" if "normalgl" in maps else None
    if normal_key:
        normal = nodes.new("ShaderNodeNormalMap")
        normal.location = (-250, -400)
        links.new(texture(normal_key, True).outputs["Color"], normal.inputs["Color"])
        links.new(normal.outputs["Normal"], bsdf.inputs["Normal"])
    if displacement and "displacement" in maps:
        output = next(node for node in nodes if node.type == "OUTPUT_MATERIAL")
        disp = nodes.new("ShaderNodeDisplacement")
        disp.location = (-250, -700)
        disp.inputs["Scale"].default_value = 0.05
        links.new(texture("displacement", True).outputs["Color"], disp.inputs["Height"])
        links.new(disp.outputs["Displacement"], output.inputs["Displacement"])
    return mat


def hdri(ref, res="2K", strength=1.0, rotation=0.0):
    """Light the scene with an ambientCG HDRI as the world; rotation turns it about Z, in degrees."""
    import math

    import bpy

    _check_res(res, HDRI_RES)
    source, ident = parse_ref(ref)
    if source != "ambientcg":
        raise AssetError("hdri() takes an ambientCG reference, e.g. 'ambientcg:DayEnvironmentHDRI001'.")
    entry = _resolve(ref, res, "hdri")
    scene = bpy.context.scene
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    tree = _node_tree(world)
    nodes, links = tree.nodes, tree.links
    background = next((node for node in nodes if node.type == "BACKGROUND"), None) or nodes.new("ShaderNodeBackground")
    output = next((node for node in nodes if node.type == "OUTPUT_WORLD"), None) or nodes.new("ShaderNodeOutputWorld")
    links.new(background.outputs["Background"], output.inputs["Surface"])
    background.inputs["Strength"].default_value = strength
    if entry is None:
        background.inputs["Color"].default_value = (0.5, 0.55, 0.6, 1)
        return world
    if "file" not in entry:
        raise AssetError(f"{ref} is a material, not an HDRI: use material().")
    coords = nodes.new("ShaderNodeTexCoord")
    mapping = nodes.new("ShaderNodeMapping")
    mapping.inputs["Rotation"].default_value = (0, 0, math.radians(rotation))
    env = nodes.new("ShaderNodeTexEnvironment")
    env.image = bpy.data.images.load(entry["file"], check_existing=True)
    links.new(coords.outputs["Generated"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    links.new(env.outputs["Color"], background.inputs["Color"])
    return world


IMPORTERS = {".glb": "gltf", ".gltf": "gltf", ".fbx": "fbx", ".obj": "obj", ".stl": "stl", ".ply": "ply",
             ".usd": "usd", ".usda": "usd", ".usdc": "usd", ".usdz": "usd"}


def import_model(ref):
    """Import a model file from one of the user's Loopcut jobs (GLB, glTF, FBX, OBJ, STL, PLY or USD);
    returns the new objects. A stand-in is one 1 m cube."""
    import bpy

    source, ident = parse_ref(ref)
    if source != "job":
        raise AssetError("import_model() takes a job output reference, e.g. 'job:job_abc/model.glb'.")
    entry = _resolve(ref, None, "file")
    before = set(bpy.data.objects)
    if entry is None:
        bpy.ops.mesh.primitive_cube_add(size=1)
        bpy.context.object.name = f"Stand-in {ident[1]}"
        return [bpy.context.object]
    path = entry["file"]
    kind = IMPORTERS.get(Path(path).suffix.lower())
    if kind == "gltf":
        bpy.ops.import_scene.gltf(filepath=path)
    elif kind == "fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif kind == "obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif kind == "stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif kind == "ply":
        bpy.ops.wm.ply_import(filepath=path)
    elif kind == "usd":
        bpy.ops.wm.usd_import(filepath=path)
    else:
        raise AssetError(f"import_model() can't import {Path(path).name}.")
    return [obj for obj in bpy.data.objects if obj not in before]


def file(ref):
    """The local path of a job output (an image, a sound), or None where it's a stand-in."""
    source, _ = parse_ref(ref)
    if source != "job":
        raise AssetError("file() takes a job output reference, e.g. 'job:job_abc/image.png'.")
    entry = _resolve(ref, None, "file")
    return entry["file"] if entry else None
