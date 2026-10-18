"""Run inside Blender: save a small scene that reads one external texture and one missing one.

blender -b --factory-startup --python make_scene.py -- OUT_DIR
"""

import sys
from pathlib import Path

import bpy

out = Path(sys.argv[sys.argv.index("--") + 1])
(out / "tex").mkdir(parents=True, exist_ok=True)
image = bpy.data.images.new("checker", 64, 64)
image.generated_type = "COLOR_GRID"
image.filepath_raw = str(out / "tex" / "checker.png")
image.file_format = "PNG"
image.save()
material = bpy.data.materials.new("Checker")
material.use_nodes = True
nodes = material.node_tree.nodes
texture = nodes.new("ShaderNodeTexImage")
texture.image = bpy.data.images.load(str(out / "tex" / "checker.png"))
material.node_tree.links.new(texture.outputs["Color"], nodes["Principled BSDF"].inputs["Base Color"])
missing = nodes.new("ShaderNodeTexImage")
missing.image = bpy.data.images.new("gone", 4, 4)
missing.image.source = "FILE"
missing.image.filepath_raw = "//tex/gone.png"
bpy.ops.mesh.primitive_plane_add(size=2)
bpy.context.object.data.materials.append(material)
bpy.data.images.remove(image)
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 24
bpy.ops.wm.save_as_mainfile(filepath=str(out / "shot.blend"), relative_remap=True)
