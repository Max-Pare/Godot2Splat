"""Builds the test room (tests/fixture_room.py) as the Unreal map /Game/Room.

Runs inside the Unreal editor's Python (the editor, not a commandlet: commandlets
cannot place actors), with the room as JSON in G2S_ROOM_JSON, and quits the editor:
    UnrealEditor-Cmd G2SFixture.uproject -ExecutePythonScript=<this file>

The room is given in Godot's axes and metres. Unreal's are X forward, Y right, Z up,
in centimetres; the capture plugin maps Unreal's X to Godot's -Z, Y to X and Z to Y,
so a Godot point (x, y, z) is the Unreal point (-z, x, y) * 100.
"""
import json
import os

import unreal

MAP = "/Game/Room"
MATERIALS = "/Game/RoomMaterials"


def to_unreal(p):
    x, y, z = p
    return unreal.Vector(-z * 100.0, x * 100.0, y * 100.0)


def size_to_unreal(s):
    x, y, z = s
    return unreal.Vector(z, x, y)


def material(name, color, cache={}):
    if name in cache:
        return cache[name]
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    path = f"{MATERIALS}/MI_{name}"
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        unreal.EditorAssetLibrary.delete_asset(path)
    mi = tools.create_asset(f"MI_{name}", MATERIALS, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    mi.set_editor_property("parent", unreal.load_asset("/Engine/BasicShapes/BasicShapeMaterial"))
    unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(mi, "Color", unreal.LinearColor(*color, 1.0))
    unreal.EditorAssetLibrary.save_loaded_asset(mi)
    cache[name] = mi
    return mi


def main():
    room = json.loads(open(os.environ["G2S_ROOM_JSON"]).read())
    levels = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    if unreal.EditorAssetLibrary.does_asset_exist(MAP):
        unreal.EditorAssetLibrary.delete_asset(MAP)
    if not levels.new_level(MAP):
        raise RuntimeError(f"cannot create {MAP}")

    cube = unreal.load_asset("/Engine/BasicShapes/Cube")         # 100 cm, centred
    sphere = unreal.load_asset("/Engine/BasicShapes/Sphere")     # 100 cm across, centred
    for b in room["boxes"]:
        a = actors.spawn_actor_from_object(cube, to_unreal(b["centre"]), unreal.Rotator(0, 0, 0))
        a.set_actor_label(b["name"])
        a.set_actor_scale3d(size_to_unreal(b["size"]))
        a.static_mesh_component.set_material(0, material(b["name"], b["color"]))
    s = room["sphere"]
    a = actors.spawn_actor_from_object(sphere, to_unreal(s["centre"]), unreal.Rotator(0, 0, 0))
    a.set_actor_label(s["name"])
    a.set_actor_scale3d(unreal.Vector(1, 1, 1) * (2 * s["radius"]))
    a.static_mesh_component.set_material(0, material(s["name"], s["color"]))

    d = room["sun_direction"]
    sun = actors.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 500), unreal.Rotator(0, 0, 0))
    sun.set_actor_label("Sun")
    sun.set_actor_rotation(unreal.MathLibrary.make_rot_from_x(unreal.Vector(-d[2], d[0], d[1])), False)
    sun.light_component.set_editor_property("atmosphere_sun_light", True)
    sun.light_component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    actors.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
    sky = actors.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0, 0, 300), unreal.Rotator(0, 0, 0))
    sky.light_component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    sky.light_component.set_editor_property("real_time_capture", True)
    lamp = actors.spawn_actor_from_class(unreal.PointLight, to_unreal(room["lamp"]), unreal.Rotator(0, 0, 0))
    lamp.set_actor_label("Lamp")
    lamp.point_light_component.set_editor_property("attenuation_radius", 900.0)
    lamp.point_light_component.set_editor_property("light_color", unreal.Color(r=255, g=237, b=204, a=255))

    v = room["volume"]
    vol = actors.spawn_actor_from_class(unreal.SplatCaptureVolume, to_unreal(v["position"]), unreal.Rotator(0, 0, 0))
    vol.set_actor_label("SplatCaptureVolume")
    vol.get_editor_property("box").set_box_extent(size_to_unreal(v["size"]) * 50.0)
    vol.set_editor_property("image_size", v["image_size"])
    vol.set_editor_property("test_every", v["test_every"])
    if os.environ.get("G2S_GODOT_PROJECT"):
        vol.set_editor_property("godot_project", unreal.DirectoryPath(path=os.environ["G2S_GODOT_PROJECT"]))

    if not levels.save_current_level():
        raise RuntimeError(f"cannot save {MAP}")
    unreal.log(f"build_room: saved {MAP}")


try:
    main()
except Exception as err:
    unreal.log_error(f"build_room: ERROR {err!r}")
    raise
finally:
    unreal.SystemLibrary.quit_editor()
