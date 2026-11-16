@tool
class_name SplatCaptureVolume
extends Node3D
## Marks the part of a scene that gets scanned into a Gaussian splat.
##
## The box (`size`, centred on this node) limits where cameras are placed.
## Scene content outside the box is still captured when it is visible from inside.
## This node's position is the start point of the camera placement and has to be
## in open air, away from walls and furniture. Add Marker3D children for more
## start points, for example in a room behind a closed door.
##
## Scan with the "Scan to Splat" button while this node is selected, or with
## `g2s build <project folder> <scene>`.

## Box in which cameras are placed, in metres, centred on this node.
@export var size := Vector3(10.0, 4.0, 10.0):
	set(value):
		size = value.max(Vector3(0.1, 0.1, 0.1))
		update_gizmos()

## Distance between camera positions. Each position gives 6 pictures.
@export_range(0.25, 5.0, 0.05, "suffix:m") var camera_spacing := 1.0
## Cameras keep at least this distance from every surface.
@export_range(0.05, 2.0, 0.05, "suffix:m") var min_clearance := 0.3
## Upper limit on camera positions; more than this are thinned out evenly.
@export_range(5, 1000, 1) var max_positions := 120
## Width and height of each picture. Training time follows the pixel count.
@export_range(128, 2048, 16, "suffix:px") var image_size := 800
## Every n-th camera position is kept out of training and used to score the
## result. 0 holds nothing out.
@export_range(0, 50, 1) var test_every := 10

@export_group("Rendering")
## Frames rendered at each view before the picture is taken. Raise this (30 or
## more) when the scene uses SDFGI or volumetric fog, which settle over time.
@export_range(1, 120, 1) var warmup_frames := 2
## Frames the scene runs before it is frozen, for scenes that build themselves
## in the first frames.
@export_range(0, 600, 1) var settle_frames := 5
## Far plane of the capture cameras.
@export_range(10.0, 10000.0, 1.0, "suffix:m") var far := 500.0
## Nodes hidden during the scan (a player model, moving objects).
@export var exclude: Array[NodePath] = []
## Leave screen-space reflections, ambient occlusion, indirect lighting, glow,
## depth of field and auto exposure on. They differ from view to view, which
## blurs the splat, so they are switched off for the scan by default.
@export var keep_screen_space_effects := false

@export_group("Training")
## Upper limit on Gaussians. 0 sizes it to the scan.
@export_range(0, 6000000, 1000) var max_gaussians := 0
## Training steps. 0 sizes it to the scan.
@export_range(0, 100000, 100) var training_steps := 0

@export_group("Output")
## Folder for the splat and the generated scene. Empty: res://splats/<scene name>/
@export_dir var output_dir := ""
