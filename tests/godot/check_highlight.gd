extends SceneTree
## Puts a SplatObject (ellipsoid) over the test room's ball in a generated splat scene
## and checks what it does on screen:
##
##   godot --path <project> --resolution 640x480 --script <this file>
##         -- --scene res://splats/room/room_splat.tscn [--save]
##
## Renders the ball from nearby with the highlight off, fully on, and with the
## object lifted, and counts the pixels that change inside and outside the ball's
## outline on screen. Then the camera stays near: the proximity glow must start.
## --save adds the SplatObject to the scene file (to check that a rebuild keeps it).
## Prints "highlight: RESULT {json}". Needs a window and the Compute renderer.

const OBJECT := "res://addons/godot2splat/objects/splat_object.gd"
const BALL := Vector3(-1.2, 1.05, -1.0)
const RADIUS := 0.3
const EYE := Vector3(0.2, 1.5, 0.3)
const WARMUP := 20


func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	var scene_path := args[args.find("--scene") + 1]
	var scene := (load(scene_path) as PackedScene).instantiate()
	var object: Node3D = load(OBJECT).new()
	object.name = "BallHighlight"
	object.set("shape", 1)   # ellipsoid: a box would take some of the table top too
	object.set("size", Vector3.ONE * (2 * RADIUS + 0.04))
	object.position = BALL
	object.set("glow_when_near", false)
	object.set("trigger_distance", 0.5)
	object.set("highlight_color", Color(0, 1, 0))
	scene.add_child(object)
	if args.has("--save"):
		object.owner = scene
		var packed := PackedScene.new()
		packed.pack(scene)
		ResourceSaver.save(packed, scene_path)
	root.add_child(scene)
	_check(object)


func _shot() -> Image:
	for i in WARMUP:
		await process_frame
	await RenderingServer.frame_post_draw
	return root.get_texture().get_image()


## Pixels that differ by more than `threshold` (0..1, any channel), split by
## whether they lie inside the ball's outline on screen (grown by `grow` px).
func _changed(a: Image, b: Image, camera: Camera3D, grow: float) -> Dictionary:
	var centre := camera.unproject_position(BALL)
	var edge := camera.unproject_position(BALL + camera.global_basis.x * RADIUS)
	var r := centre.distance_to(edge) + grow
	var inside := 0
	var outside := 0
	for y in a.get_height():
		for x in a.get_width():
			var d := a.get_pixel(x, y) - b.get_pixel(x, y)
			if maxf(absf(d.r), maxf(absf(d.g), absf(d.b))) < 0.1:
				continue
			if Vector2(x, y).distance_to(centre) <= r:
				inside += 1
			else:
				outside += 1
	return {"inside": inside, "outside": outside, "ball_px": int(PI * r * r)}


func _check(object: Node3D) -> void:
	await process_frame
	var camera := Camera3D.new()
	root.add_child(camera)
	camera.look_at_from_position(EYE, BALL)
	camera.make_current()
	var result := {}
	var plain := await _shot()
	object.set("highlight_strength", 1.0)
	result["highlight"] = _changed(plain, await _shot(), camera, 4.0)
	object.set("highlight_strength", 0.0)
	object.position = BALL + Vector3(0, 0.25, 0)
	var lifted := await _shot()
	result["lift"] = _changed(plain, lifted, camera, 4.0)
	object.position = BALL
	# Proximity: walk the camera next to the ball and let the glow fade in.
	object.set("glow_when_near", true)
	camera.look_at_from_position(BALL + Vector3(0.6, 0.2, 0.6), BALL)
	for i in 60:
		await process_frame
	result["near"] = object.get("player_near")
	result["glow"] = object.get("highlight_strength")
	print("highlight: RESULT " + JSON.stringify(result))
	quit(0)
