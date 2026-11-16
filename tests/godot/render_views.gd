extends SceneTree
## Renders a generated splat scene from chosen capture cameras, to compare what
## Godot shows with what the scan photographed.
##
##   godot --path <project> --resolution 640x640 --script <this file>
##         -- --scene res://splats/room/room_splat.tscn --capture <capture.json>
##            --views 0007_0,0015_3 --out <folder>
##
## Writes <out>/<view>.png. Needs a window (the splat is drawn by the renderer).

const WARMUP_FRAMES := 30   # the splat uploads and sorts over the first frames


func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	var options := {}
	for i in range(0, args.size() - 1, 2):
		options[args[i]] = args[i + 1]
	_render(options)


func _render(options: Dictionary) -> void:
	var capture: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(options["--capture"]))
	var wanted: PackedStringArray = options["--views"].split(",")
	var packed := load(options["--scene"]) as PackedScene
	if packed == null:
		print("render: ERROR cannot load %s" % options["--scene"])
		quit(1)
		return
	root.add_child(packed.instantiate())
	var size := Vector2i(capture["image_size"][0], capture["image_size"][1])
	root.size = size
	var camera := Camera3D.new()
	camera.fov = capture["fov_y"]
	camera.near = capture["near"]
	camera.far = capture["far"]
	root.add_child(camera)
	camera.make_current()
	DirAccess.make_dir_recursive_absolute(options["--out"])
	for view in capture["views"]:
		if not wanted.has(view["name"]):
			continue
		var t: Array = view["transform"]
		camera.global_transform = Transform3D(
				Basis(Vector3(t[0], t[1], t[2]), Vector3(t[3], t[4], t[5]), Vector3(t[6], t[7], t[8])),
				Vector3(t[9], t[10], t[11]))
		for i in WARMUP_FRAMES:
			await process_frame
		await RenderingServer.frame_post_draw
		var picture := root.get_texture().get_image()
		picture.save_png(String(options["--out"]).path_join("%s.png" % view["name"]))
		print("render: %s %dx%d" % [view["name"], picture.get_width(), picture.get_height()])
	quit(0)
