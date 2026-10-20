"""Run in Blender: save a .blend with a Mixamo-named T-pose rig and a simple skinned body, for motion tests.

    blender -b --factory-startup --python tests/make_rig.py -- OUT.blend

The rig is built here from joint positions (never Adobe's Y Bot): mixamorig: names, facing -Y, 1.8 m tall,
with a capsule body bound by vertex groups and a ground plane, camera and light for a quick look. Another
armature-free object stays active, so the apply has to find the rig by itself.
"""

import sys

import bmesh
import bpy
from mathutils import Matrix, Vector

P = "mixamorig:"
CENTER = [("Hips", None, (0, 0, 1.00), (0, 0, 1.10)), ("Spine", "Hips", (0, 0, 1.10), (0, 0, 1.22)),
          ("Spine1", "Spine", (0, 0, 1.22), (0, 0, 1.34)), ("Spine2", "Spine1", (0, 0, 1.34), (0, 0, 1.45)),
          ("Neck", "Spine2", (0, 0, 1.45), (0, 0, 1.55)), ("Head", "Neck", (0, 0, 1.55), (0, 0, 1.78))]


def sided(side, x):
    s = 1 if side == "Left" else -1
    return [(f"{side}Shoulder", "Spine2", (s * 0.04, 0, 1.42), (s * 0.17, 0, 1.43)),
            (f"{side}Arm", f"{side}Shoulder", (s * 0.17, 0, 1.43), (s * 0.45, 0, 1.43)),
            (f"{side}ForeArm", f"{side}Arm", (s * 0.45, 0, 1.43), (s * 0.70, 0, 1.43)),
            (f"{side}Hand", f"{side}ForeArm", (s * 0.70, 0, 1.43), (s * 0.80, 0, 1.43)),
            (f"{side}HandIndex1", f"{side}Hand", (s * 0.80, 0, 1.43), (s * 0.84, 0, 1.43)),
            (f"{side}HandIndex2", f"{side}HandIndex1", (s * 0.84, 0, 1.43), (s * 0.87, 0, 1.43)),
            (f"{side}UpLeg", "Hips", (s * x, 0, 0.98), (s * x, 0, 0.53)),
            (f"{side}Leg", f"{side}UpLeg", (s * x, 0, 0.53), (s * x, 0.02, 0.09)),
            (f"{side}Foot", f"{side}Leg", (s * x, 0.02, 0.09), (s * x, -0.12, 0.02)),
            (f"{side}ToeBase", f"{side}Foot", (s * x, -0.12, 0.02), (s * x, -0.20, 0.02))]


BONES = CENTER + sided("Left", 0.10) + sided("Right", 0.10)
RADIUS = {"Hips": 0.14, "Spine": 0.13, "Spine1": 0.14, "Spine2": 0.15, "Neck": 0.05, "Head": 0.10,
          "Shoulder": 0.05, "Arm": 0.045, "ForeArm": 0.04, "Hand": 0.035, "HandIndex1": 0.012, "HandIndex2": 0.01,
          "UpLeg": 0.07, "Leg": 0.05, "Foot": 0.04, "ToeBase": 0.03}


def radius(name):
    for side in ("Left", "Right"):
        if name.startswith(side):
            return RADIUS[name[len(side):]]
    return RADIUS[name]


def build():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    data = bpy.data.armatures.new("Hero")
    rig = bpy.data.objects.new("Hero", data)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    for name, parent, head, tail in BONES:
        bone = data.edit_bones.new(P + name)
        bone.head, bone.tail = Vector(head), Vector(tail)
        if parent:
            bone.parent = data.edit_bones[P + parent]
            bone.use_connect = (bone.parent.tail - bone.head).length < 1e-4
    bpy.ops.object.mode_set(mode="OBJECT")

    mesh = bpy.data.meshes.new("Hero body")
    body = bpy.data.objects.new("Hero body", mesh)
    scene.collection.objects.link(body)
    built = bmesh.new()
    groups = []
    for name, _parent, head, tail in BONES:
        head, tail = Vector(head), Vector(tail)
        axis = tail - head
        r = radius(name)
        rotation = axis.normalized().to_track_quat("Z", "Y").to_matrix().to_4x4()
        made = bmesh.ops.create_cone(built, cap_ends=True, segments=10, radius1=r, radius2=r * 0.85,
                                     depth=max(axis.length, 0.01), matrix=Matrix.Translation((head + tail) / 2) @ rotation)
        groups.append((P + name, made["verts"]))
    made = bmesh.ops.create_uvsphere(built, u_segments=12, v_segments=8, radius=0.11,
                                     matrix=Matrix.Translation((0, 0, 1.70)))
    groups.append((P + "Head", made["verts"]))
    built.verts.index_update()
    groups = [(name, [vert.index for vert in verts]) for name, verts in groups]
    built.to_mesh(mesh)
    built.free()
    for name, indices in groups:
        group = body.vertex_groups.get(name) or body.vertex_groups.new(name=name)
        group.add(list(indices), 1.0, "REPLACE")
    body.parent = rig
    body.modifiers.new("Armature", "ARMATURE").object = rig
    material = bpy.data.materials.new("Body")
    material.diffuse_color = (0.85, 0.45, 0.2, 1)
    mesh.materials.append(material)

    bpy.ops.mesh.primitive_plane_add(size=12)
    ground = bpy.context.active_object
    ground.name = "Ground"
    camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    scene.collection.objects.link(camera)
    camera.location = (5.5, -7.5, 2.6)
    camera.rotation_euler = (1.37, 0, 0.63)
    scene.camera = camera
    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", "SUN"))
    sun.rotation_euler = (0.7, 0.2, 0.5)
    scene.collection.objects.link(sun)
    scene.render.fps = 30
    scene.frame_start, scene.frame_end = 1, 60
    bpy.context.view_layer.objects.active = ground  # Not the rig: the apply has to find it.
    for ob in scene.objects:
        ob.select_set(False)
    return rig


if __name__ == "__main__":
    out = sys.argv[sys.argv.index("--") + 1]
    build()
    bpy.ops.wm.save_as_mainfile(filepath=out)
    print("saved", out)
