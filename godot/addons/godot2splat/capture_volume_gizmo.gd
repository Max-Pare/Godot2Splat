@tool
extends EditorNode3DGizmoPlugin
## Editor gizmo for SplatCaptureVolume: the box as a wireframe and a cross at the
## start point.

const Volume := preload("res://addons/godot2splat/capture_volume.gd")
const COLOR := Color(0.35, 0.9, 0.55)


func _init() -> void:
	create_material("box", COLOR)


func _get_gizmo_name() -> String: return "SplatCaptureVolume"
func _get_priority() -> int: return 0
func _has_gizmo(node: Node3D) -> bool: return node is Volume


func _redraw(gizmo: EditorNode3DGizmo) -> void:
	gizmo.clear()
	var volume := gizmo.get_node_3d() as Volume
	if volume == null:
		return
	var box := AABB(-volume.size * 0.5, volume.size)
	var lines := PackedVector3Array()
	# The 12 box edges: corners joined by an edge differ in one bit of their index.
	for i in range(8):
		for bit in [1, 2, 4]:
			if i & bit == 0:
				lines.append(box.get_endpoint(i))
				lines.append(box.get_endpoint(i | bit))
	gizmo.add_collision_segments(lines)
	for axis in [Vector3.RIGHT, Vector3.UP, Vector3.BACK]:
		lines.append(-axis * 0.25)
		lines.append(axis * 0.25)
	gizmo.add_lines(lines, get_material("box", gizmo))
