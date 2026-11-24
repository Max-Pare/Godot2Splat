extends Node
## Scans a scene for the g2s pipeline: loads it, places cameras in the free space
## of its SplatCaptureVolume and saves a colour picture and a depth map per view.
##
## Output folder (read by src/g2s/stages/ingest.py):
##   capture.json      settings and one entry per view
##   images/NAME.png   colour as the game shows it
##   depth/NAME.bin    raw half-float RGB, see depth_pass.gdshader
##   sky/NAME.png      the environment alone, six views
##
## Add to the tree after setting `scene_path` and `out_dir`; `finished` reports
## the exit code. Needs a real renderer (Forward+ or Mobile), so not --headless.

signal finished(exit_code: int)

const Volume := preload("res://addons/godot2splat/capture_volume.gd")
const Planner := preload("res://addons/godot2splat/capture/view_planner.gd")
const DEPTH_SHADER := preload("res://addons/godot2splat/capture/depth_pass.gdshader")

## Render layer (1 to 20) that only the depth cameras see; the depth pass lives there.
const DEPTH_LAYER := 20
const PROBE_SIZE := 64
const FOV := 90.0
const FACE_NAMES := ["px", "nx", "py", "ny", "pz", "nz"]

var scene_path := ""
var out_dir := ""

var _volume: Volume
var _near := 0.05
var _color_view: SubViewport
var _color_camera: Camera3D
var _depth_view: SubViewport
var _depth_camera: Camera3D
var _probe_views: Array[SubViewport] = []
var _probe_cameras: Array[Camera3D] = []
var _face_bases: Array[Basis] = Planner.face_bases()
var _label: Label
var _notes: PackedStringArray = []


func _ready() -> void:
	process_mode = Node.PROCESS_MODE_ALWAYS
	_run.call_deferred()


func _run() -> void:
	var started := Time.get_ticks_msec()
	if RenderingServer.get_rendering_device() == null:
		_fail("no rendering device. The scan needs the Forward+ or Mobile renderer and a window (not --headless).")
		return
	var packed := load(scene_path) as PackedScene
	if packed == null:
		_fail("cannot load scene %s" % scene_path)
		return
	var scene := packed.instantiate()
	get_tree().root.add_child(scene)

	_volume = _find_volume(scene)
	if _volume == null:
		_fail("no SplatCaptureVolume node in %s. Add one inside the area to scan, with its origin in open air." % scene_path)
		return

	# Let the scene set itself up, then stop everything that moves: scripts,
	# physics, animations, particles and shader time.
	for i in _volume.settle_frames:
		await get_tree().process_frame
	scene.process_mode = Node.PROCESS_MODE_DISABLED
	Engine.time_scale = 0.0
	for path in _volume.exclude:
		var node := _volume.get_node_or_null(path)
		if node != null and "visible" in node:
			node.visible = false
		else:
			print("capture: WARNING exclude entry not found or not hideable: %s" % path)

	_show_progress_label()
	_build_views()
	await _frames(2)

	var positions := await _plan_positions()
	if positions.is_empty():
		return
	var ok := await _shoot(positions)
	if not ok:
		return
	print("capture: done in %.0f s" % ((Time.get_ticks_msec() - started) / 1000.0))
	finished.emit(0)


func _fail(message: String) -> void:
	print("capture: ERROR " + message)
	finished.emit(1)


func _find_volume(scene: Node) -> Volume:
	if scene is Volume:
		return scene
	for node in scene.find_children("*", "Node3D", true, false):
		if node is Volume:
			return node
	return null


func _show_progress_label() -> void:
	# The window only shows progress; the scan renders into its own viewports.
	get_tree().root.disable_3d = true
	var layer := CanvasLayer.new()
	_label = Label.new()
	_label.position = Vector2(16, 16)
	layer.add_child(_label)
	add_child(layer)


func _progress(text: String) -> void:
	_label.text = "godot2splat scan\n" + text


func _frames(count: int) -> void:
	for i in count:
		await RenderingServer.frame_post_draw


# --- viewports -------------------------------------------------------------

func _build_views() -> void:
	_near = minf(0.05, _volume.min_clearance * 0.25)
	var world := get_tree().root.world_3d
	var depth_bit := 1 << (DEPTH_LAYER - 1)

	# Colour: the scene as the game shows it.
	_color_view = SubViewport.new()
	_color_view.size = Vector2i(_volume.image_size, _volume.image_size)
	_color_view.msaa_3d = Viewport.MSAA_4X
	_color_view.use_taa = false
	_color_view.screen_space_aa = Viewport.SCREEN_SPACE_AA_DISABLED
	_color_view.mesh_lod_threshold = 0.0
	_color_view.render_target_update_mode = SubViewport.UPDATE_DISABLED
	add_child(_color_view)
	_color_camera = _new_camera(_color_view, 0xFFFFF & ~depth_bit)
	_color_camera.environment = _capture_environment(world)
	_color_camera.attributes = _capture_attributes(world)

	# Depth: the same view through depth_pass.gdshader, at half size.
	var depth_size: int = maxi(64, _volume.image_size / 2)
	_depth_view = _new_depth_view(depth_size)
	_depth_camera = _new_camera(_depth_view, 0xFFFFF)
	_neutralise(_depth_camera)
	for i in 6:
		var view := _new_depth_view(PROBE_SIZE)
		var camera := _new_camera(view, 0xFFFFF)
		_neutralise(camera)
		_probe_views.append(view)
		_probe_cameras.append(camera)

	# One full-screen quad serves every depth camera: its shader ignores where it is.
	var material := ShaderMaterial.new()
	material.shader = DEPTH_SHADER
	material.set_shader_parameter("far", _volume.far)
	var quad := QuadMesh.new()
	quad.size = Vector2(2, 2)
	var pass_mesh := MeshInstance3D.new()
	pass_mesh.mesh = quad
	pass_mesh.material_override = material
	pass_mesh.layers = depth_bit
	pass_mesh.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	pass_mesh.gi_mode = GeometryInstance3D.GI_MODE_DISABLED
	pass_mesh.extra_cull_margin = 16384.0
	pass_mesh.ignore_occlusion_culling = true
	pass_mesh.custom_aabb = AABB(Vector3.ONE * -1e6, Vector3.ONE * 2e6)
	add_child(pass_mesh)


func _new_depth_view(pixels: int) -> SubViewport:
	var view := SubViewport.new()
	view.size = Vector2i(pixels, pixels)
	view.use_hdr_2d = true   # half-float output: the depth values must arrive unchanged
	view.msaa_3d = Viewport.MSAA_DISABLED
	view.use_taa = false
	view.use_debanding = false
	view.screen_space_aa = Viewport.SCREEN_SPACE_AA_DISABLED
	view.mesh_lod_threshold = 0.0
	view.render_target_update_mode = SubViewport.UPDATE_DISABLED
	add_child(view)
	return view


func _new_camera(view: SubViewport, cull_mask: int) -> Camera3D:
	var camera := Camera3D.new()
	camera.fov = FOV
	camera.near = _near
	camera.far = _volume.far
	camera.cull_mask = cull_mask
	view.add_child(camera)
	camera.make_current()
	return camera


## Depth cameras must not tonemap, fog or expose what the depth pass writes.
func _neutralise(camera: Camera3D) -> void:
	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = Color.BLACK
	env.tonemap_mode = Environment.TONE_MAPPER_LINEAR
	camera.environment = env
	camera.attributes = CameraAttributesPractical.new()


## The scene's environment without the effects that differ from view to view.
## Null leaves the scene's own environment in charge.
func _capture_environment(world: World3D) -> Environment:
	var env := world.environment
	if env == null:
		return null
	if env.sdfgi_enabled:
		_note("SDFGI is on: its lighting settles over many frames and moves with the camera. Raise warmup_frames to 30 or more, or bake the lighting.")
	if env.volumetric_fog_enabled:
		_note("volumetric fog is on: it settles over several frames. Raise warmup_frames if the pictures look noisy.")
	if _volume.keep_screen_space_effects:
		return null
	var copy: Environment = env.duplicate()
	var off: PackedStringArray = []
	if copy.ssr_enabled:
		copy.ssr_enabled = false
		off.append("screen-space reflections")
	if copy.ssao_enabled:
		copy.ssao_enabled = false
		off.append("ambient occlusion (SSAO)")
	if copy.ssil_enabled:
		copy.ssil_enabled = false
		off.append("indirect lighting (SSIL)")
	if copy.glow_enabled:
		copy.glow_enabled = false
		off.append("glow")
	if not off.is_empty():
		_note("switched off for the scan (they differ from view to view): %s. Set keep_screen_space_effects on the volume to keep them." % ", ".join(off))
	return copy


func _capture_attributes(world: World3D) -> CameraAttributes:
	var attributes := world.camera_attributes
	if attributes == null or _volume.keep_screen_space_effects:
		return null
	var copy: CameraAttributes = attributes.duplicate()
	var off: PackedStringArray = []
	if copy.auto_exposure_enabled:
		copy.auto_exposure_enabled = false
		off.append("auto exposure")
	if copy is CameraAttributesPractical and (copy.dof_blur_far_enabled or copy.dof_blur_near_enabled):
		copy.dof_blur_far_enabled = false
		copy.dof_blur_near_enabled = false
		off.append("depth of field")
	if not off.is_empty():
		_note("switched off for the scan: %s." % ", ".join(off))
	return copy


func _note(text: String) -> void:
	_notes.append(text)
	print("capture: NOTE " + text)


# --- camera placement ------------------------------------------------------

## What the point `at` sees in every direction (see view_planner.gd).
func _probe(at: Vector3) -> Array[PackedFloat32Array]:
	for i in 6:
		_probe_cameras[i].global_transform = Transform3D(_face_bases[i], at)
	await _frames(2)
	var probe: Array[PackedFloat32Array] = []
	for i in 6:
		probe.append(Planner.face_distances(_probe_views[i].get_texture().get_image(), _volume.far))
	return probe


## Walks outward from the start points through everything they can see.
## Returns the camera positions, or nothing after reporting an error.
func _plan_positions() -> PackedVector3Array:
	for view in _probe_views:
		view.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	# A grid cell is reached when an accepted position sees it. Box: only cells inside
	# the box. Fill: any cell within fill_reach, except that cells which see mostly
	# sky (outdoors) are not taken and the walk does not go on from them. Both: a cell
	# too close to a surface has its finer points tried instead (tight_spots).
	var spacing := _volume.camera_spacing
	var fill := _volume.placement == Volume.Placement.FILL
	var grid: Planner.Grid = Planner.Grid.fill(_volume.global_transform, spacing) if fill \
			else Planner.Grid.box(_volume.global_transform, _volume.size, spacing)
	var seen := {}
	var outdoors := 0
	var reach := ceili(2.5 * spacing / minf(grid.step.x, minf(grid.step.y, grid.step.z))) + 1

	var queue := PackedVector3Array([_volume.global_position])
	for child in _volume.get_children():
		if child is Marker3D:
			queue.append(child.global_position)
	var seeds := queue.size()
	var inverse_bases: Array[Basis] = []
	for basis in _face_bases:
		inverse_bases.append(basis.inverse())

	# Per queue entry: the accepted position that reached it (-1: a start point), and
	# whether it is a finer point tried in a tight spot (those are not refined again).
	var queue_parent := PackedInt32Array()
	var queue_fine := PackedByteArray()
	for i in seeds:
		queue_parent.append(-1)
		queue_fine.append(0)
	var fine_tried := 0
	var fine_taken := 0
	var fine_open := 0
	var cached_parent := -1
	var parent_probe: Array[PackedFloat32Array] = []

	var accepted := PackedVector3Array()
	var too_close := 0
	var next := 0
	while next < queue.size():
		var index := next
		var at := queue[index]
		next += 1
		var probe := await _probe(at)
		var room := Planner.clearance(probe)
		# Finer points stand in gaps: half the clearance, or most gaps would have no
		# room for them (a 0.85 m gap leaves 25 cm at 30 cm from both sides).
		if room < _volume.min_clearance * (0.5 if queue_fine[index] == 1 else 1.0):
			if index < seeds:
				_fail("start point %s is %.2f m from a surface (min_clearance is %.2f m). Move the SplatCaptureVolume or its Marker3D into open air." % [at, room, _volume.min_clearance])
				return PackedVector3Array()
			too_close += 1
			var parent := queue_parent[index]
			if _volume.tight_spots and queue_fine[index] == 0 and parent >= 0:
				# Try the eight points at half the spacing in this cell, so a gap
				# narrower than the grid (behind a couch, between furniture) gets
				# cameras. Only points the position that reached this cell sees
				# directly, so none ends up inside furniture or behind a wall.
				var from := accepted[parent]
				if cached_parent != parent:
					parent_probe = await _probe(from)
					cached_parent = parent
				for to in grid.fine_positions(grid.cell_of(at)):
					if from.distance_to(to) > spacing * 2.5 \
							or (fill and _volume.global_position.distance_to(to) > _volume.fill_reach) \
							or not Planner.sees(parent_probe, inverse_bases, PROBE_SIZE, from, to, 0.05):
						continue
					queue.append(to)
					queue_parent.append(parent)
					queue_fine.append(1)
					fine_tried += 1
			continue
		if queue_fine[index] == 1 and Planner.gap_width(probe) >= spacing + 2.0 * _volume.min_clearance:
			# Next to one surface only, such as a plain wall: the grid's own points see
			# it. A finer point is for gaps the grid can step over.
			fine_open += 1
			continue
		if fill and index >= seeds and Planner.sky_share(probe) > _volume.outdoor_sky_share:
			outdoors += 1
			continue
		accepted.append(at)
		var me := accepted.size() - 1
		fine_taken += queue_fine[index]
		var here := grid.cell_of(at)
		for dx in range(-reach, reach + 1):
			for dy in range(-reach, reach + 1):
				for dz in range(-reach, reach + 1):
					var c := here + Vector3i(dx, dy, dz)
					if not grid.contains(c) or seen.has(c):
						continue
					var to := grid.position(c)
					if at.distance_to(to) > spacing * 2.5 \
							or (fill and _volume.global_position.distance_to(to) > _volume.fill_reach) \
							or not Planner.sees(probe, inverse_bases, PROBE_SIZE, at, to, 0.05):
						continue
					seen[c] = true
					queue.append(to)
					queue_parent.append(me)
					queue_fine.append(0)
		if next % 10 == 0:
			_progress("finding free space: %d camera positions" % accepted.size())
	for view in _probe_views:
		view.render_target_update_mode = SubViewport.UPDATE_DISABLED

	if fill:
		var span := AABB(accepted[0], Vector3.ZERO)
		for p in accepted:
			span = span.expand(p)
		print("capture: the open space reachable from the start point spans %.1f x %.1f x %.1f m: %d positions %.2f m apart (%d too close to a surface, %d outdoors)" % [
				span.size.x, span.size.y, span.size.z, accepted.size(), spacing, too_close, outdoors])
	else:
		print("capture: %d of %d grid points are in reachable open space (%d too close to a surface)" % [
				accepted.size() - seeds, grid.num_cells(), too_close])
	if _volume.tight_spots:
		print("capture: tight spots: %d of the positions are finer points in gaps narrower than %.2f m (%d tried, %d beside one surface only)" % [
				fine_taken, spacing + 2.0 * _volume.min_clearance, fine_tried, fine_open])
	var max_positions := _volume.max_positions if _volume.max_positions > 0 \
			else Planner.auto_max_positions(_volume.image_size)
	if accepted.size() > max_positions:
		print("capture: thinned evenly to %d positions (%s)." % [max_positions,
				"max_positions" if _volume.max_positions > 0 else "what training can hold at this image_size"])
		var chosen := Planner.spread(accepted, max_positions, seeds)
		var thinned := PackedVector3Array()
		for i in chosen:
			thinned.append(accepted[i])
		accepted = thinned
	return accepted


# --- pictures --------------------------------------------------------------

func _shoot(positions: PackedVector3Array) -> bool:
	for folder in ["images", "depth", "sky"]:
		DirAccess.make_dir_recursive_absolute(out_dir.path_join(folder))
	_color_view.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	_depth_view.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	var rng := RandomNumberGenerator.new()
	rng.seed = 2
	var views: Array[Dictionary] = []
	var test_every := _volume.test_every
	for p in positions.size():
		# The six faces of a cube see every direction; turning the cube at random
		# keeps the picture edges from lining up between positions.
		var turn := Basis(Quaternion(rng.randfn(), rng.randfn(), rng.randfn(), rng.randfn()).normalized())
		var split := "test" if test_every > 0 and p % test_every == test_every - 1 else "train"
		for f in 6:
			var xf := Transform3D(turn * _face_bases[f], positions[p])
			_color_camera.global_transform = xf
			_depth_camera.global_transform = xf
			await _frames(_volume.warmup_frames)
			var view_name := "%04d_%d" % [p, f]
			var picture := _color_view.get_texture().get_image()
			picture.convert(Image.FORMAT_RGB8)
			var error := picture.save_png(out_dir.path_join("images/%s.png" % view_name))
			var depth := _depth_view.get_texture().get_image()
			depth.convert(Image.FORMAT_RGBH)
			var file := FileAccess.open(out_dir.path_join("depth/%s.bin" % view_name), FileAccess.WRITE)
			if error != OK or file == null:
				_fail("cannot write to %s" % out_dir)
				return false
			file.store_buffer(depth.get_data())
			file.close()
			views.append({"name": view_name, "split": split, "transform": _numbers(xf)})
		_progress("taking pictures: position %d of %d" % [p + 1, positions.size()])
		if (p + 1) % 10 == 0 or p + 1 == positions.size():
			print("capture: position %d of %d" % [p + 1, positions.size()])

	# The environment alone, for the sky shown behind the splat.
	_depth_view.render_target_update_mode = SubViewport.UPDATE_DISABLED
	_color_camera.cull_mask = 0
	var sky: Array[Dictionary] = []
	for f in 6:
		var xf := Transform3D(_face_bases[f], _volume.global_position)
		_color_camera.global_transform = xf
		await _frames(_volume.warmup_frames)
		var picture := _color_view.get_texture().get_image()
		picture.convert(Image.FORMAT_RGB8)
		picture.save_png(out_dir.path_join("sky/%s.png" % FACE_NAMES[f]))
		sky.append({"name": FACE_NAMES[f], "transform": _numbers(xf)})

	var output_dir := _volume.output_dir
	if output_dir.is_empty():
		output_dir = "res://splats/%s/" % scene_path.get_file().get_basename()
	var manifest := {
		"version": 1,
		"scene": scene_path,
		"output_dir": output_dir,
		"image_size": [_color_view.size.x, _color_view.size.y],
		"depth_size": [_depth_view.size.x, _depth_view.size.y],
		"fov_y": FOV,
		"near": _near,
		"far": _volume.far,
		"positions": positions.size(),
		"volume": {"transform": _numbers(_volume.global_transform),
				"size": [_volume.size.x, _volume.size.y, _volume.size.z]},
		"training": {"steps": _volume.training_steps, "max_gaussians": _volume.max_gaussians},
		"notes": _notes,
		"views": views,
		"sky": sky,
	}
	var file := FileAccess.open(out_dir.path_join("capture.json"), FileAccess.WRITE)
	if file == null:
		_fail("cannot write capture.json to %s" % out_dir)
		return false
	file.store_string(JSON.stringify(manifest, "  "))
	file.close()
	return true


## A transform as 12 numbers: the basis vectors x, y, z, then the origin.
func _numbers(xf: Transform3D) -> Array[float]:
	var b := xf.basis
	var o := xf.origin
	return [b.x.x, b.x.y, b.x.z, b.y.x, b.y.y, b.y.z, b.z.x, b.z.y, b.z.z, o.x, o.y, o.z]
