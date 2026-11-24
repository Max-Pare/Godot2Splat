extends RefCounted
## Where to put the capture cameras: the maths, without any rendering.
##
## Free space is found from rendered depth alone, so it works with anything that
## draws and needs no collision shapes. A "probe" is what one point sees in every
## direction: six square 90 degree depth views (one per cube face), each turned
## into the distance to the nearest surface per pixel (0 = nothing there).
## capture_runner.gd renders the probes and walks outward from the start points:
## a candidate position is reached when an accepted position sees it. A point
## inside a wall or a solid object is hidden from every point in open air, so it
## is never reached.


## Camera orientations of the six cube faces: looking along +X, -X, +Y, -Y, +Z, -Z.
static func face_bases() -> Array[Basis]:
	return [
		Basis.looking_at(Vector3.RIGHT, Vector3.UP),
		Basis.looking_at(Vector3.LEFT, Vector3.UP),
		Basis.looking_at(Vector3.UP, Vector3.BACK),
		Basis.looking_at(Vector3.DOWN, Vector3.FORWARD),
		Basis.looking_at(Vector3.BACK, Vector3.UP),
		Basis.looking_at(Vector3.FORWARD, Vector3.UP),
	]


## Candidate camera positions: one per cell of a grid, each moved a little at
## random (the same way every time) so that rows of cameras do not line up with
## the scene. Cells are found on demand, so an unbounded grid costs nothing until
## the walk reaches it. Same as FGrid in unreal/.../ViewPlanner.h.
class Grid:
	var xf: Transform3D
	var offset: Vector3
	var step: Vector3
	var bounded := false
	var count := Vector3i.ZERO

	## The box `size` centred on `box_xf`, cells `spacing` apart (rounded to fit).
	static func box(box_xf: Transform3D, size: Vector3, spacing: float) -> Grid:
		var g := Grid.new()
		g.xf = box_xf.orthonormalized()
		g.bounded = true
		g.count = Vector3i(maxi(1, roundi(size.x / spacing)), maxi(1, roundi(size.y / spacing)),
				maxi(1, roundi(size.z / spacing)))
		g.step = size / Vector3(g.count)
		g.offset = -size * 0.5
		return g

	## No end; cell 0 is centred on `origin_xf`'s origin (the start point).
	static func fill(origin_xf: Transform3D, spacing: float) -> Grid:
		var g := Grid.new()
		g.xf = origin_xf.orthonormalized()
		g.step = Vector3.ONE * spacing
		g.offset = -g.step * 0.5
		return g

	func contains(c: Vector3i) -> bool:
		return not bounded or (c.x >= 0 and c.y >= 0 and c.z >= 0
				and c.x < count.x and c.y < count.y and c.z < count.z)

	func position(c: Vector3i) -> Vector3:
		var rng := RandomNumberGenerator.new()
		rng.seed = hash(c) + 0x9e3779b9
		var jitter := Vector3(rng.randf_range(-0.3, 0.3), rng.randf_range(-0.3, 0.3), rng.randf_range(-0.3, 0.3))
		return xf * (offset + (Vector3(c) + Vector3(0.5, 0.5, 0.5) + jitter) * step)

	## The eight points at half the spacing inside cell `c`, each moved a little at
	## random like the cell's own point (half as far). Tried where the cell's own
	## point is too close to a surface, so gaps narrower than the grid get cameras.
	func fine_positions(c: Vector3i) -> PackedVector3Array:
		var out := PackedVector3Array()
		for i in 8:
			var sub := Vector3(0.75 if (i & 1) != 0 else 0.25, 0.75 if (i & 2) != 0 else 0.25,
					0.75 if (i & 4) != 0 else 0.25)
			var rng := RandomNumberGenerator.new()
			rng.seed = hash([c, i]) + 0x85ebca6b
			var jitter := Vector3(rng.randf_range(-0.15, 0.15), rng.randf_range(-0.15, 0.15), rng.randf_range(-0.15, 0.15))
			out.append(xf * (offset + (Vector3(c) + sub + jitter) * step))
		return out

	func cell_of(world: Vector3) -> Vector3i:
		var local := (xf.affine_inverse() * world - offset) / step
		return Vector3i(floori(local.x), floori(local.y), floori(local.z))

	func num_cells() -> int:
		return count.x * count.y * count.z if bounded else 0


## Share of a probe's pixels that see nothing within the far plane: sky.
static func sky_share(probe: Array[PackedFloat32Array]) -> float:
	var sky := 0
	var all := 0
	for face in probe:
		for d in face:
			if d <= 0.0:
				sky += 1
		all += face.size()
	return float(sky) / maxi(1, all)


## Camera positions affordable for training: all pictures stay in memory, so the
## budget is in pixels (1.5 gigapixels, about 6 GB of RAM with depth): 390
## positions at 800 px, 610 at 640 px.
static func auto_max_positions(image_size: int) -> int:
	return maxi(30, int(1.5e9 / (6.0 * image_size * image_size)))


## Distance to the nearest surface for every pixel of a probe face.
## `img` is a depth_pass.gdshader picture: its blue channel is depth / far, where
## depth is measured along the view axis.
static func face_distances(img: Image, far: float) -> PackedFloat32Array:
	img.convert(Image.FORMAT_RGBF)
	var data := img.get_data().to_float32_array()
	var n := img.get_width()
	var out := PackedFloat32Array()
	out.resize(n * n)
	for y in n:
		var ty := (y + 0.5) / n * 2.0 - 1.0
		for x in n:
			var tx := (x + 0.5) / n * 2.0 - 1.0
			out[y * n + x] = data[(y * n + x) * 3 + 2] * far * sqrt(1.0 + tx * tx + ty * ty)
	return out


## Distance from the probe point to the nearest surface in any direction.
## Width of the narrowest gap the probe stands in: over each pair of opposite faces
## (face_bases order: +x/-x, +y/-y, +z/-z), the nearest surface in the one plus the
## nearest in the other. INF when no pair has surfaces on both sides.
static func gap_width(probe: Array[PackedFloat32Array]) -> float:
	var width := INF
	for i in range(0, probe.size() - 1, 2):
		var a := INF
		var b := INF
		for d in probe[i]:
			if d > 0.0 and d < a:
				a = d
		for d in probe[i + 1]:
			if d > 0.0 and d < b:
				b = d
		if a < INF and b < INF:
			width = minf(width, a + b)
	return width


static func clearance(probe: Array[PackedFloat32Array]) -> float:
	var nearest := INF
	for face in probe:
		for d in face:
			if d > 0.0 and d < nearest:
				nearest = d
	return nearest


## True when the point `to` is in view from the probe taken at `from`, with
## `margin` metres to spare before the surface behind it. `inverse_bases` are the
## inverted face orientations, `n` the probe's width in pixels.
static func sees(probe: Array[PackedFloat32Array], inverse_bases: Array[Basis], n: int,
		from: Vector3, to: Vector3, margin: float) -> bool:
	var offset := to - from
	var dist := offset.length()
	if dist < 1e-6:
		return true
	# The face the direction falls on is the one it points along most.
	var face := 0
	var forward := -INF
	var cam := Vector3.ZERO
	for i in 6:
		var c := inverse_bases[i] * offset
		if -c.z > forward:
			forward = -c.z
			face = i
			cam = c
	var px := int(floor((cam.x / forward * 0.5 + 0.5) * n))
	var py := int(floor((-cam.y / forward * 0.5 + 0.5) * n))
	# The neighbouring pixels count too: a thin obstacle may fall between two rays.
	for dy in [-1, 0, 1]:
		for dx in [-1, 0, 1]:
			var x := clampi(px + dx, 0, n - 1)
			var y := clampi(py + dy, 0, n - 1)
			var d := probe[face][y * n + x]
			if d > 0.0 and d < dist + margin:
				return false
	return true


## At most `count` of `points`, spread as evenly as possible. The first `keep`
## points (the start points) always stay. Returns the chosen indices in order.
static func spread(points: PackedVector3Array, count: int, keep: int) -> PackedInt32Array:
	var chosen := PackedInt32Array()
	var total := points.size()
	if total <= count:
		for i in total:
			chosen.append(i)
		return chosen
	# Farthest point first: each new point is the one farthest from all chosen so far.
	var nearest := PackedFloat32Array()
	nearest.resize(total)
	nearest.fill(INF)
	var next := 0
	while chosen.size() < count:
		chosen.append(next)
		var p := points[next]
		var best := -1.0
		var following := chosen.size() if chosen.size() < keep else -1
		for i in total:
			var d := p.distance_squared_to(points[i])
			if d < nearest[i]:
				nearest[i] = d
			if nearest[i] > best:
				best = nearest[i]
				next = i
		if following >= 0:
			next = following
	chosen.sort()
	return chosen
