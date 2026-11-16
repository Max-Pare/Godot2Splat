extends SceneTree
## Command line entry of the scan. Started by `g2s build` as:
##   godot --path <project> --script res://addons/godot2splat/capture/capture_cli.gd
##         -- --scene res://room.tscn --out /absolute/output/folder
## Needs a window: Godot's headless mode cannot render.

const Runner := preload("res://addons/godot2splat/capture/capture_runner.gd")


func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	var options := {}
	for i in range(0, args.size() - 1, 2):
		options[args[i]] = args[i + 1]
	if not options.has("--scene") or not options.has("--out"):
		print("capture: ERROR usage: -- --scene <res://scene.tscn> --out <folder>")
		quit(2)
		return
	DisplayServer.window_set_title("godot2splat scan")
	var runner := Runner.new()
	runner.scene_path = options["--scene"]
	runner.out_dir = options["--out"]
	runner.finished.connect(func(code: int) -> void: quit(code))
	root.add_child(runner)
