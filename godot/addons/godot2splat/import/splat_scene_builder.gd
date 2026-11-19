@tool
extends RefCounted
## Builds the scene that shows a scanned splat, from the manifest g2s writes next
## to the .ply (NAME_splat.json):
##
##   <Name>Splat (Node3D)
##     SplatPivot (Node3D)              half a turn about Z, at the splat's centre
##       Splat (GaussianSplatNode)      half a turn about Z
##     SplatEnvironment (WorldEnvironment)
##     Collision (Node3D)               copies of the source scene's static bodies
##     Player (player.tscn)             first-person camera; made on the first build only
##
## Everything else under the root (SplatObject highlight volumes, lights, your own
## nodes, the Player) is yours: a rebuild keeps it. So is what the gdgs panels add
## to Splat (CollisionBody, CollisionSeed, baked lighting, the panels' settings):
## a rebuild moves it to the new Splat. Changes to the other nodes above are replaced.
##
## Why two half turns: the gdgs importer centres the Gaussians on their mean, so
## the node has to sit there, and GaussianSplatNode turns itself by half a turn
## about Z when it enters the tree with an unrotated transform (it assumes a
## Y-down file; ours is Y-up). A node that is already rotated is left alone, and
## the pivot above it cancels the turn exactly.
##
## The environment uses linear tonemapping because the splat's colours are the
## scanned scene's final, tonemapped colours; gdgs draws them before tonemapping.

const SPLAT_NODE := "res://addons/gdgs/runtime/nodes/gaussian_splat_node.gd"
const COMPOSITOR_EFFECT := "res://addons/gdgs/runtime/compositor/gaussian_compositor_effect.gd"
const SKY_SHADER := "res://addons/godot2splat/import/panorama_sky.gdshader"
const PLAYER_SCENE := "res://addons/godot2splat/player/player.tscn"
## Children of the root that the builder makes; every other child is yours and is kept.
const GENERATED: Array[StringName] = [&"SplatPivot", &"SplatEnvironment", &"Collision"]
const HALF_TURN := Basis(Vector3(-1, 0, 0), Vector3(0, -1, 0), Vector3(0, 0, 1))


## Returns {ok, message, scene, bodies, kept}.
static func build(manifest_path: String) -> Dictionary:
	var m: Variant = JSON.parse_string(FileAccess.get_file_as_string(manifest_path))
	if typeof(m) != TYPE_DICTIONARY:
		return _error("cannot read %s" % manifest_path)
	for script_path in [SPLAT_NODE, COMPOSITOR_EFFECT]:
		if not ResourceLoader.exists(script_path):
			return _error("the gdgs addon is missing (%s). Run `g2s install <project>`." % script_path)
	if not ResourceLoader.exists(m["splat"]):
		return _error("%s is not imported yet. Enable the gdgs plugin and let the editor import it." % m["splat"])
	var gaussian: Resource = load(m["splat"])
	if gaussian == null or not ("point_count" in gaussian):
		return _error("%s was not imported as a Gaussian splat. Is the gdgs plugin enabled?" % m["splat"])

	var scene_path: String = m["scene"]
	var root := Node3D.new()
	root.name = scene_path.get_file().get_basename().to_pascal_case()

	var c: Array = m["center"]
	var pivot := Node3D.new()
	pivot.name = "SplatPivot"
	pivot.transform = Transform3D(HALF_TURN, Vector3(c[0], c[1], c[2]))
	root.add_child(pivot)
	var splat: Node3D = load(SPLAT_NODE).new()
	splat.name = "Splat"
	splat.transform = Transform3D(HALF_TURN, Vector3.ZERO)
	splat.set("gaussian", gaussian)
	pivot.add_child(splat)

	var world_env := WorldEnvironment.new()
	world_env.name = "SplatEnvironment"
	world_env.environment = _environment(m)
	var compositor := Compositor.new()
	compositor.compositor_effects = [load(COMPOSITOR_EFFECT).new()]
	world_env.compositor = compositor
	root.add_child(world_env)

	var collision := Node3D.new()
	collision.name = "Collision"
	root.add_child(collision)
	var bodies := _copy_static_bodies(m.get("source_scene", ""), collision)

	for node in root.find_children("*", "", true, false):
		node.owner = root
	var kept := _keep_added_nodes(scene_path, root)
	if root.get_node_or_null("Player") == null:
		_add_player(root, m, bodies == 0)
	var packed := PackedScene.new()
	var error := packed.pack(root)
	if error == OK:
		error = ResourceSaver.save(packed, scene_path)
	root.free()
	if error != OK:
		return _error("cannot save %s (error %d)" % [scene_path, error])
	return {"ok": true, "message": "", "scene": scene_path, "bodies": bodies, "kept": kept}


static func _error(message: String) -> Dictionary:
	return {"ok": false, "message": message, "scene": "", "bodies": 0, "kept": 0}


## Moves what you added to an earlier build of the scene (SplatObjects, lights,
## your own nodes: every child of its root that the builder does not make) into
## the new one, so a rebuild keeps them. Returns how many were kept.
static func _keep_added_nodes(scene_path: String, root: Node) -> int:
	if not ResourceLoader.exists(scene_path):
		return 0
	var packed := ResourceLoader.load(scene_path, "", ResourceLoader.CACHE_MODE_IGNORE) as PackedScene
	if packed == null:
		return 0
	var old := packed.instantiate()
	var count := _keep_splat_additions(old, root)
	for child in old.get_children():
		if child.name in GENERATED:
			continue
		_move_owned(child, old, root, root)
		count += 1
	old.free()
	return count


## What the gdgs panels add to the Splat node: CollisionBody and CollisionSeed
## children (and any other child you gave it), the baked lighting and relight
## settings, and the panels' settings, kept as metadata. Children keep their place
## in the world when the new splat is centred elsewhere. Returns how many children
## were kept. gdgs ignores baked lighting that does not match the splat's count.
static func _keep_splat_additions(old: Node, root: Node) -> int:
	var old_splat := old.get_node_or_null("SplatPivot/Splat") as Node3D
	var new_splat := root.get_node_or_null("SplatPivot/Splat") as Node3D
	if old_splat == null or new_splat == null:
		return 0
	var script: Script = old_splat.get_script()
	if script != null:
		for p in script.get_script_property_list():
			if p["usage"] & PROPERTY_USAGE_STORAGE and p["name"] != "gaussian":
				new_splat.set(p["name"], old_splat.get(p["name"]))
	for key in old_splat.get_meta_list():
		new_splat.set_meta(key, old_splat.get_meta(key))
	var old_xf := _transform_in_scene(old_splat, old)
	var new_xf := _transform_in_scene(new_splat, root)
	var count := 0
	for child in old_splat.get_children():
		if child.owner != old:
			continue   # made by the node itself at run time, not saved
		if child is Node3D:
			(child as Node3D).transform = new_xf.affine_inverse() * old_xf * (child as Node3D).transform
		_move_owned(child, old, new_splat, root)
		count += 1
	return count


## Moves `child` under `parent`, with the nodes `old_root` saved below it (the
## insides of instanced scenes stay theirs) now owned by `new_root`.
static func _move_owned(child: Node, old_root: Node, parent: Node, new_root: Node) -> void:
	var owned: Array[Node] = [child]
	for node in child.find_children("*", "", true, false):
		if node.owner == old_root:
			owned.append(node)
	# Owners are set again below; left on the old root, moving warns of an inconsistent owner.
	for node in owned:
		node.owner = null
	child.get_parent().remove_child(child)
	parent.add_child(child)
	for node in owned:
		node.owner = new_root


## The first-person player, with its camera at the scan's start point (open air by
## construction). Made on the first build only; after that it is yours, like any
## node you add, so moving it or changing its settings survives a rebuild.
## Without collision there is nothing to walk on, so it starts flying.
static func _add_player(root: Node3D, m: Dictionary, flying: bool) -> void:
	var player: Node3D = load(PLAYER_SCENE).instantiate()
	player.name = "Player"
	var spawn: Array = m.get("spawn", [0.0, 0.0, 0.0])
	var eye: float = (player.get_node("Camera") as Node3D).position.y
	player.position = Vector3(spawn[0], spawn[1] - eye, spawn[2])
	player.set("start_flying", flying)
	root.add_child(player)
	player.owner = root


static func _environment(m: Dictionary) -> Environment:
	var env := Environment.new()
	env.tonemap_mode = Environment.TONE_MAPPER_LINEAR
	var sky_path: String = m.get("sky", "")
	if sky_path != "" and ResourceLoader.exists(sky_path):
		var material := ShaderMaterial.new()
		material.shader = load(SKY_SHADER)
		material.set_shader_parameter("panorama", load(sky_path))
		var sky := Sky.new()
		sky.sky_material = material
		env.background_mode = Environment.BG_SKY
		env.sky = sky
	else:
		# Nothing was seen behind the scene: show its average colour instead of black.
		var b: Array = m.get("background", [0.0, 0.0, 0.0])
		env.background_mode = Environment.BG_COLOR
		env.background_color = Color(b[0], b[1], b[2])
	return env


## Copies every StaticBody3D of the source scene, with its CollisionShape3D
## children, under `parent`. The source is read without entering the tree, so its
## scripts do not run; bodies they would create at run time are not found.
static func _copy_static_bodies(source_scene: String, parent: Node3D) -> int:
	if source_scene == "" or not ResourceLoader.exists(source_scene):
		return 0
	var packed := load(source_scene) as PackedScene
	if packed == null:
		return 0
	var source := packed.instantiate()
	var found: Array[Node] = source.find_children("*", "StaticBody3D", true, false)
	if source is StaticBody3D:
		found.push_front(source)
	var count := 0
	for node in found:
		var original := node as StaticBody3D
		var body := StaticBody3D.new()
		body.collision_layer = original.collision_layer
		body.collision_mask = original.collision_mask
		body.physics_material_override = original.physics_material_override
		for child in original.get_children():
			var shape := child as CollisionShape3D
			if shape == null or shape.shape == null or shape.disabled:
				continue
			var copy := CollisionShape3D.new()
			copy.name = shape.name
			copy.transform = shape.transform
			# A copy of the shape: the new scene must not depend on the source scene's file.
			copy.shape = shape.shape.duplicate()
			body.add_child(copy)
		if body.get_child_count() == 0:
			body.free()
			continue
		body.name = "%s_%d" % [original.name, count]
		body.transform = _transform_in_scene(original, source)
		parent.add_child(body)
		count += 1
	source.free()
	return count


## Transform of `node` relative to the scene's origin, for a scene outside the tree.
static func _transform_in_scene(node: Node, scene_root: Node) -> Transform3D:
	var xf := Transform3D.IDENTITY
	var at := node
	while at != null:
		if at is Node3D:
			xf = (at as Node3D).transform * xf
		if at == scene_root:
			break
		at = at.get_parent()
	return xf
