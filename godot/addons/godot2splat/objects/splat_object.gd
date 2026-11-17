@tool
@icon("res://addons/gdgs/editor/icons/gaussian_splat_volume.svg")
class_name SplatObject
extends GaussianSplatVolume
## A piece of the scan that reacts to the player. Ported from Real2Virtu.
##
## Add one to a splat scene made by g2s (res://splats/<scene>/<scene>_splat.tscn,
## or a scene that instances it), then move and resize it in the editor until the
## tinted Gaussians are the object you want (drag the dots on its faces, or set
## Shape and Size in the inspector). In the game it glows when the camera comes
## near and can move; `player_entered` and `player_exited` are there for your own
## scripts. A rebuild of the splat scene keeps the SplatObjects you added to it.
##
## Nothing is cut out of the splat: the object is whatever lies inside the shape
## when the game starts (see GaussianSplatVolume). Moving this node from a script
## or an AnimationPlayer moves the object too, as long as Motion is None (a motion
## sets the node's transform every frame). Compute renderer only, at most 32.
##
## The scan has nothing behind the object, so moving it far leaves a hole.
##
## Collision: the generated scene's collision bodies (copies of the source scene's
## StaticBody3D nodes) whose origin lies inside the shape when the game starts
## belong to the object (see `collision`).

signal player_entered
signal player_exited

enum Motion { NONE, BOB, WOBBLE, SPIN }
enum Collision {
	FOLLOWS,  ## The object's collision bodies move with it.
	STAYS,    ## They keep blocking where the object was scanned.
	NONE,     ## They are switched off: the player walks through the object.
}

@export_group("Near the player")
## How close the camera must come to the shape, in metres.
@export_range(0.0, 20.0, 0.05, "or_greater") var trigger_distance := 1.5
## Pulse the highlight colour while the player is near.
@export var glow_when_near := true
## Strongest tint of the pulse; 1 paints the object in the highlight colour.
@export_range(0.0, 1.0, 0.01) var glow_strength := 0.45
## Pulses per second.
@export_range(0.1, 10.0, 0.1) var glow_speed := 1.0

@export_group("Motion")
@export var motion: Motion = Motion.NONE
## Move only while the player is near; otherwise all the time.
@export var move_only_when_near := true
## Bob: height in metres. Wobble: tilt in degrees. Spin: turns per second.
@export var motion_amount := 0.05
## Bob and wobble cycles per second.
@export_range(0.05, 10.0, 0.05) var motion_speed := 0.5

@export_group("Collision")
## What happens to the collision bodies inside the shape. Read once, when the
## game starts.
@export var collision: Collision = Collision.FOLLOWS

## The collision bodies that belong to the object (set when the game starts).
var collision_bodies: Array[CollisionObject3D] = []

## True while the camera is within `trigger_distance`.
var player_near := false

var _near_amount := 0.0  # 0..1, eased, so glow and motion fade in and out
var _phase := 0.0
var _spin := 0.0

const FADE_TIME := 0.35


func _ready() -> void:
	super()
	if not Engine.is_editor_hint():
		_adopt_collision.call_deferred()


## Takes the static bodies whose origin is inside the shape at the rest pose.
## Bodies that are children of another SplatObject's shape stay with the first
## object in the tree, as the Gaussians do.
func _adopt_collision() -> void:
	if collision == Collision.STAYS:
		return
	var to_unit := get_rest_to_unit()
	var scene := get_tree().current_scene if get_tree().current_scene != null else get_tree().root
	for node in scene.find_children("*", "StaticBody3D", true, false):
		var body := node as StaticBody3D
		if body.is_ancestor_of(self) or body.has_meta(&"splat_object"):
			continue
		if not unit_contains(shape, to_unit * body.global_position):
			continue
		body.set_meta(&"splat_object", self)
		collision_bodies.append(body)
		if collision == Collision.NONE:
			body.process_mode = Node.PROCESS_MODE_DISABLED
		else:
			# Keeps its place in the world; from now on it moves with this node.
			body.reparent(self, true)


func _process(delta: float) -> void:
	if Engine.is_editor_hint():
		return
	var camera := get_viewport().get_camera_3d()
	var near := camera != null and distance_to(camera.global_position) <= trigger_distance
	if near != player_near:
		player_near = near
		(player_entered if near else player_exited).emit()
	_near_amount = move_toward(_near_amount, 1.0 if near else 0.0, delta / FADE_TIME)
	_phase += delta

	if glow_when_near:
		var pulse := 0.5 - 0.5 * cos(_phase * glow_speed * TAU)
		highlight_strength = glow_strength * _near_amount * lerpf(0.35, 1.0, pulse)

	if motion != Motion.NONE:
		var drive := _near_amount if move_only_when_near else 1.0
		global_transform = get_rest_transform() * _motion_offset(delta, drive)


## Where the object is relative to its rest pose. `drive` scales the motion
## between 0 (at rest) and 1.
func _motion_offset(delta: float, drive: float) -> Transform3D:
	var wave := sin(_phase * motion_speed * TAU)
	match motion:
		Motion.BOB:
			# Rises off its rest height and never dips below it.
			return Transform3D(Basis.IDENTITY, Vector3.UP * motion_amount * drive * (0.5 + 0.5 * wave))
		Motion.WOBBLE:
			return Transform3D(Basis(Vector3.FORWARD, deg_to_rad(motion_amount) * drive * wave), Vector3.ZERO)
		Motion.SPIN:
			_spin = fposmod(_spin + delta * motion_amount * drive * TAU, TAU)
			return Transform3D(Basis(Vector3.UP, _spin), Vector3.ZERO)
	return Transform3D.IDENTITY
