@tool
@icon("res://addons/gdgs/editor/icons/gaussian_resource.svg")
extends Resource
class_name GaussianResource

# GPU-ready std430 layout.
# Each splat occupies 60 float32 values (240 bytes).
@export var point_count: int = 0
## The splats as stored and uploaded to the GPU.
@export var point_data_byte: PackedByteArray
@export var xyz: PackedVector3Array
@export var aabb: AABB = AABB()

## The same data as floats, made from point_data_byte on every read (a copy: read
## it once). Not stored: it used to be, which doubled the file and the memory.
## Files imported before keep loading; what they stored here is taken as the bytes.
var point_data_float: PackedFloat32Array:
	get:
		return point_data_byte.to_float32_array()
	set(value):
		point_data_byte = value.to_byte_array()
