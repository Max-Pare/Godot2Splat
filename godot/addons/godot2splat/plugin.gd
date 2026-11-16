@tool
extends EditorPlugin
## Editor side of godot2splat. For now it only draws the SplatCaptureVolume box;
## the "Scan to Splat" button is not written yet. Scan with `g2s build`.

const Gizmo := preload("res://addons/godot2splat/capture_volume_gizmo.gd")

var _gizmo: EditorNode3DGizmoPlugin


func _enter_tree() -> void:
	_gizmo = Gizmo.new()
	add_node_3d_gizmo_plugin(_gizmo)


func _exit_tree() -> void:
	if _gizmo != null:
		remove_node_3d_gizmo_plugin(_gizmo)
		_gizmo = null
