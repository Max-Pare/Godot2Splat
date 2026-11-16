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


## Candidate camera positions: a grid with `spacing` inside the box `size` centred
## on `xf`, each point moved a little at random so that rows of cameras do not
## line up with the scene.
static func candidates(xf: Transform3D, size: Vector3, spacing: float,
		rng: RandomNumberGenerator) -> PackedVector3Array:
	var counts := Vector3i(maxi(1, roundi(size.x / spacing)), maxi(1, roundi(size.y / spacing)),
			maxi(1, roundi(size.z / spacing)))
	var step := size / Vector3(counts)
	var out := PackedVector3Array()
	for ix in counts.x:
		for iy in counts.y:
			for iz in counts.z:
				var jitter := Vector3(rng.randf_range(-0.3, 0.3), rng.randf_range(-0.3, 0.3),
						rng.randf_range(-0.3, 0.3))
				var local := -size * 0.5 + (Vector3(ix, iy, iz) + Vector3(0.5, 0.5, 0.5) + jitter) * step
				out.append(xf * local)
	return out


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
