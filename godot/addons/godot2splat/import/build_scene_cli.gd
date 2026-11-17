extends SceneTree
## Command line entry of the scene builder. Started by `g2s build` as:
##   godot --headless --path <project> --script res://addons/godot2splat/import/build_scene_cli.gd
##         -- res://splats/room/room_splat.json

const Builder := preload("res://addons/godot2splat/import/splat_scene_builder.gd")


func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 1:
		print("build: ERROR usage: -- <res://.../NAME_splat.json>")
		quit(2)
		return
	var result := Builder.build(args[0])
	if result["ok"]:
		print("export: built %s (%d collision bodies copied, %d of your nodes kept)"
				% [result["scene"], result["bodies"], result["kept"]])
		quit(0)
	else:
		print("export: ERROR " + result["message"])
		quit(1)
