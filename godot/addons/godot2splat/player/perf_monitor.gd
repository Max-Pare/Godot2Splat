extends CanvasLayer
## Performance overlay for play mode, part of the Player scene. F3 shows or hides it.
##
##   frame   time between frames as the player sees them: average and worst over the
##           last second, and frames per second
##   CPU     main thread (scripts, physics) and the render thread's work on this view
##   GPU     the GPU's time on this view, the splat included (gdgs draws it as a
##           compositor effect of the view)
##   VRAM    video memory Godot has allocated (buffers, the splat's included, and
##           textures); not what other programs use
##   RAM     Godot's own allocations (debug builds only) and the whole system's use
##   graph   frame time (light) and GPU time (orange) of the last frames, with lines
##           at 60 and 30 frames per second
##
## The numbers update four times a second; the graph every frame.

const HISTORY := 240
const UPDATE_EVERY := 0.25
const GRAPH_SIZE := Vector2(360, 80)
const FRAME_COLOUR := Color(0.85, 0.9, 1.0)
const GPU_COLOUR := Color(1.0, 0.6, 0.2)
const LINE_COLOUR := Color(1, 1, 1, 0.25)
const TEXT_COLOUR := Color(1, 1, 1, 0.7)

## Shown when play starts. F3 toggles it either way.
@export var show_on_start := true

var _label: Label
var _graph: Control
var _frames := PackedFloat32Array()   # ms, oldest first
var _gpu := PackedFloat32Array()
var _last_tick := 0
var _since_update := 0.0
var _splats := -1
var _reports := 0


func _ready() -> void:
	layer = 100
	_frames.resize(HISTORY)
	_gpu.resize(HISTORY)
	var panel := PanelContainer.new()
	panel.mouse_filter = Control.MOUSE_FILTER_IGNORE
	panel.position = Vector2(8, 8)
	var style := StyleBoxFlat.new()
	style.bg_color = Color(0, 0, 0, 0.6)
	style.set_content_margin_all(8)
	style.set_corner_radius_all(4)
	panel.add_theme_stylebox_override("panel", style)
	add_child(panel)
	var box := VBoxContainer.new()
	box.mouse_filter = Control.MOUSE_FILTER_IGNORE
	panel.add_child(box)
	_label = Label.new()
	_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var font := SystemFont.new()
	font.font_names = PackedStringArray(["Consolas", "DejaVu Sans Mono", "Menlo", "monospace"])
	_label.add_theme_font_override("font", font)
	_label.add_theme_font_size_override("font_size", 14)
	box.add_child(_label)
	_graph = Control.new()
	_graph.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_graph.custom_minimum_size = GRAPH_SIZE
	_graph.draw.connect(_draw_graph)
	box.add_child(_graph)
	_set_shown(show_on_start)


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo and event.physical_keycode == KEY_F3:
		_set_shown(not visible)
		get_viewport().set_input_as_handled()


func _set_shown(on: bool) -> void:
	visible = on
	# GPU timing costs a little; only measure while shown.
	RenderingServer.viewport_set_measure_render_time(get_viewport().get_viewport_rid(), on)
	set_process(on)
	_last_tick = 0


func _process(delta: float) -> void:
	# Real time between frames: `delta` may be smoothed.
	var now := Time.get_ticks_usec()
	var ms := (now - _last_tick) / 1000.0 if _last_tick > 0 else delta * 1000.0
	_last_tick = now
	var vp := get_viewport().get_viewport_rid()
	_push(_frames, ms)
	_push(_gpu, RenderingServer.viewport_get_measured_render_time_gpu(vp))
	_graph.queue_redraw()
	_since_update += delta
	if _since_update >= UPDATE_EVERY:
		_since_update = 0.0
		_label.text = _report(vp)


static func _push(a: PackedFloat32Array, v: float) -> void:
	for i in a.size() - 1:
		a[i] = a[i + 1]
	a[a.size() - 1] = v


func _report(vp: RID) -> String:
	# Frames of the last second.
	var total := 0.0
	var worst := 0.0
	var n := 0
	for i in range(HISTORY - 1, -1, -1):
		var ms := _frames[i]
		if ms <= 0.0 or total >= 1000.0:
			break
		total += ms
		worst = maxf(worst, ms)
		n += 1
	var average := total / maxi(n, 1)
	var lines: Array[String] = []
	lines.append("frame  %5.1f ms avg  %5.1f ms worst  %4d fps" % [average, worst, Engine.get_frames_per_second()])
	# Godot's own process time includes the wait for vsync, so it is left out.
	lines.append("CPU    %5.1f ms render  %5.1f ms physics" % [
			RenderingServer.get_frame_setup_time_cpu() + RenderingServer.viewport_get_measured_render_time_cpu(vp),
			Performance.get_monitor(Performance.TIME_PHYSICS_PROCESS) * 1000.0])
	lines.append("GPU    %5.1f ms" % RenderingServer.viewport_get_measured_render_time_gpu(vp))

	var rd := RenderingServer.get_rendering_device()
	if rd != null:
		lines.append("VRAM   %s  (buffers %s, textures %s)" % [
				_size(rd.get_memory_usage(RenderingDevice.MEMORY_TOTAL)),
				_size(rd.get_memory_usage(RenderingDevice.MEMORY_BUFFERS)),
				_size(rd.get_memory_usage(RenderingDevice.MEMORY_TEXTURES))])
	else:
		lines.append("VRAM   %s" % _size(int(Performance.get_monitor(Performance.RENDER_VIDEO_MEM_USED))))

	var ram := "RAM   "
	var own := OS.get_static_memory_usage()
	if own > 0:
		ram += " Godot %s (peak %s) " % [_size(own), _size(OS.get_static_memory_peak_usage())]
	var info := OS.get_memory_info()
	# On Windows "available" counts the page file too; "free" is what Task Manager calls available.
	var unused: int = info.get("free" if OS.get_name() == "Windows" else "available", -1)
	if info.get("physical", -1) > 0 and unused >= 0:
		ram += " system %s of %s" % [_size(info["physical"] - unused), _size(info["physical"])]
	lines.append(ram)

	_reports += 1
	if _splats < 0 or _reports % 8 == 0:   # every 2 s
		_splats = _count_splats()
	lines.append("splats %s  draw calls %d  objects %d" % [
			_thousands(_splats),
			Performance.get_monitor(Performance.RENDER_TOTAL_DRAW_CALLS_IN_FRAME),
			Performance.get_monitor(Performance.RENDER_TOTAL_OBJECTS_IN_FRAME)])

	var size := get_viewport().get_visible_rect().size
	var vsync := DisplayServer.window_get_vsync_mode() != DisplayServer.VSYNC_DISABLED
	lines.append("%dx%d  vsync %s  %s" % [size.x, size.y, "on" if vsync else "off", RenderingServer.get_video_adapter_name()])
	var at := get_parent() as Node3D
	if at != null:
		var p := at.global_position
		lines.append("at     %.2f, %.2f, %.2f" % [p.x, p.y, p.z])
	return "\n".join(lines)


## Gaussians in the running scene, from every node with a gdgs `gaussian` resource.
func _count_splats() -> int:
	var scene := get_tree().current_scene
	if scene == null:
		return 0
	var count := 0
	for node in scene.find_children("*", "Node3D", true, false):
		if not ("gaussian" in node):
			continue
		var g: Object = node.get("gaussian")
		if g != null and "point_count" in g:
			count += int(g.get("point_count"))
	return count


func _draw_graph() -> void:
	var size := _graph.size
	# Scaled to the last second, so a hitch (loading, the first frames) does not
	# flatten the rest for the whole history; taller spikes are cut off.
	var top := 1000.0 / 30.0
	for i in range(HISTORY - 60, HISTORY):
		top = maxf(top, maxf(_frames[i], _gpu[i]))
	top *= 1.1
	for target in [1000.0 / 60.0, 1000.0 / 30.0]:
		var y: float = size.y * (1.0 - target / top)
		_graph.draw_line(Vector2(0, y), Vector2(size.x, y), LINE_COLOUR)
	for data in [[_gpu, GPU_COLOUR], [_frames, FRAME_COLOUR]]:
		var values: PackedFloat32Array = data[0]
		var points := PackedVector2Array()
		for i in HISTORY:
			points.append(Vector2(size.x * i / (HISTORY - 1), size.y * (1.0 - minf(values[i], top) / top)))
		_graph.draw_polyline(points, data[1], 1.0)
	var font := ThemeDB.fallback_font
	_graph.draw_string(font, Vector2(2, 12), "%.0f ms" % top, HORIZONTAL_ALIGNMENT_LEFT, -1, 11, TEXT_COLOUR)
	_graph.draw_string(font, Vector2(size.x - 70, 12), "frame", HORIZONTAL_ALIGNMENT_LEFT, -1, 11, FRAME_COLOUR)
	_graph.draw_string(font, Vector2(size.x - 28, 12), "GPU", HORIZONTAL_ALIGNMENT_LEFT, -1, 11, GPU_COLOUR)


static func _size(bytes: int) -> String:
	if bytes >= 1 << 30:
		return "%.2f GiB" % (bytes / float(1 << 30))
	return "%.0f MiB" % (bytes / float(1 << 20))


static func _thousands(n: int) -> String:
	var s := str(n)
	var out := ""
	while s.length() > 3:
		out = "," + s.substr(s.length() - 3) + out
		s = s.substr(0, s.length() - 3)
	return s + out
