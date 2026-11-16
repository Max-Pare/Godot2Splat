@tool
extends EditorPlugin
## Editor side of godot2splat: draws the SplatCaptureVolume box and adds a
## "Scan to Splat" button to the 3D editor toolbar while such a node is selected.
##
## The button runs the g2s command line tool on the open scene (capture, training,
## cleanup) in a separate process and shows its output. When that ends, the splat
## is imported here in the editor, the splat scene is built and opened.
##
## Which command to run is read from res://.g2s/command.json, written by
## `g2s install <project>`.

const Volume := preload("res://addons/godot2splat/capture_volume.gd")
const Gizmo := preload("res://addons/godot2splat/capture_volume_gizmo.gd")
const Builder := preload("res://addons/godot2splat/import/splat_scene_builder.gd")
const COMMAND_FILE := "res://.g2s/command.json"
const IMPORT_TIMEOUT := 3600.0   # seconds; the gdgs importer needs about 1.5 minutes per million Gaussians

var _gizmo: EditorNode3DGizmoPlugin
var _button: Button
var _dialog: AcceptDialog
var _log: RichTextLabel
var _stop: Button
var _volume: Node3D

var _pid := 0
var _pipe: FileAccess
var _reader: Thread
var _mutex := Mutex.new()
var _pending: PackedStringArray = []
var _reader_done := false
var _manifest := ""
var _reimported := {}


func _enter_tree() -> void:
	_gizmo = Gizmo.new()
	add_node_3d_gizmo_plugin(_gizmo)

	_button = Button.new()
	_button.text = "Scan to Splat"
	_button.tooltip_text = "Scan this scene into a Gaussian splat with g2s (saves the scene first)."
	_button.visible = false
	_button.pressed.connect(_on_button_pressed)
	add_control_to_container(CONTAINER_SPATIAL_EDITOR_MENU, _button)

	_dialog = AcceptDialog.new()
	_dialog.title = "Scan to Splat"
	_dialog.ok_button_text = "Close"
	_dialog.min_size = Vector2i(820, 460)
	_log = RichTextLabel.new()
	_log.scroll_following = true
	_log.selection_enabled = true
	_log.custom_minimum_size = Vector2(800, 400)
	_dialog.add_child(_log)
	_stop = _dialog.add_button("Stop", true, "stop")
	_stop.disabled = true
	_dialog.custom_action.connect(_on_dialog_action)
	EditorInterface.get_base_control().add_child(_dialog)

	EditorInterface.get_resource_filesystem().resources_reimported.connect(_on_resources_reimported)
	set_process(false)

	# Test hook: G2S_EDITOR_SCAN=res://scene.tscn scans that scene as if the button
	# had been pressed, and G2S_EDITOR_QUIT=1 closes the editor afterwards.
	var auto := OS.get_environment("G2S_EDITOR_SCAN")
	if auto != "":
		_scan_on_startup.call_deferred(auto)


func _exit_tree() -> void:
	if _pid != 0:
		OS.kill(_pid)
	if _reader != null and _reader.is_started():
		_reader.wait_to_finish()
	if _gizmo != null:
		remove_node_3d_gizmo_plugin(_gizmo)
	if _button != null:
		remove_control_from_container(CONTAINER_SPATIAL_EDITOR_MENU, _button)
		_button.queue_free()
	if _dialog != null:
		_dialog.queue_free()
	var fs := EditorInterface.get_resource_filesystem()
	if fs.resources_reimported.is_connected(_on_resources_reimported):
		fs.resources_reimported.disconnect(_on_resources_reimported)


func _handles(object: Object) -> bool:
	return object is Volume


func _edit(object: Object) -> void:
	_volume = object as Node3D


func _make_visible(visible: bool) -> void:
	_button.visible = visible


func _on_button_pressed() -> void:
	if _pid != 0:
		_dialog.popup_centered()
		return
	var scene := EditorInterface.get_edited_scene_root()
	if scene == null or scene.scene_file_path == "":
		_show_message("Save the scene to a file first.")
		return
	if EditorInterface.save_scene() != OK:
		_show_message("Could not save the scene.")
		return
	_start(scene.scene_file_path, _volume)


# --- running g2s -----------------------------------------------------------

func _start(scene_path: String, volume: Node3D) -> void:
	_log.clear()
	_stop.disabled = true
	_dialog.popup_centered()
	var command := _read_command()
	if command.is_empty():
		_append("Cannot find the g2s command (%s is missing or unreadable).\nRun this once in a terminal:  g2s install %s" % [
				COMMAND_FILE, ProjectSettings.globalize_path("res://")])
		return
	if not EditorInterface.is_plugin_enabled("gdgs"):
		# Its importer turns the .ply into a splat resource.
		EditorInterface.set_plugin_enabled("gdgs", true)

	var scene_name := scene_path.get_file().get_basename()
	var out_dir: String = volume.get("output_dir") if volume != null else ""
	if out_dir == "":
		out_dir = "res://splats/%s/" % scene_name
	_manifest = out_dir.trim_suffix("/") + "/%s_splat.json" % scene_name

	var arguments := PackedStringArray(command.slice(1))
	arguments.append_array(["build", ProjectSettings.globalize_path("res://"), scene_path,
			"--no-godot-import", "--godot", OS.get_executable_path()])
	_append("$ %s %s\n" % [command[0], " ".join(arguments)])
	var process := OS.execute_with_pipe(command[0], arguments)
	if process.is_empty():
		_append("Could not start %s" % command[0])
		return
	_pid = process["pid"]
	_pipe = process["stdio"]
	_reader_done = false
	_reader = Thread.new()
	_reader.start(_read_output)
	_stop.disabled = false
	set_process(true)


func _read_command() -> PackedStringArray:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(COMMAND_FILE))
	if typeof(parsed) != TYPE_DICTIONARY or typeof(parsed.get("argv")) != TYPE_ARRAY:
		return PackedStringArray()
	return PackedStringArray(parsed["argv"])


## Runs on its own thread: reading the pipe blocks until the tool prints a line.
func _read_output() -> void:
	while _pipe.is_open() and _pipe.get_error() == OK:
		var line := _pipe.get_line()
		_mutex.lock()
		_pending.append(line)
		_mutex.unlock()
	_mutex.lock()
	_reader_done = true
	_mutex.unlock()


func _process(_delta: float) -> void:
	_mutex.lock()
	var lines := _pending
	_pending = PackedStringArray()
	var done := _reader_done
	_mutex.unlock()
	for line in lines:
		if line != "":
			_append(line)
	if done and not OS.is_process_running(_pid):
		set_process(false)
		_reader.wait_to_finish()
		var code := OS.get_process_exit_code(_pid)
		_pid = 0
		_pipe = null
		_stop.disabled = true
		if code == 0:
			_import_and_build()
		else:
			_finish(false, "g2s ended with exit code %d." % code)


func _on_dialog_action(action: StringName) -> void:
	if action == &"stop" and _pid != 0:
		_append("Stopping ...")
		OS.kill(_pid)


# --- back into the project -------------------------------------------------

func _on_resources_reimported(paths: PackedStringArray) -> void:
	for path in paths:
		_reimported[path] = true


func _import_and_build() -> void:
	var manifest: Variant = JSON.parse_string(FileAccess.get_file_as_string(_manifest))
	if typeof(manifest) != TYPE_DICTIONARY:
		_finish(false, "g2s finished but %s is missing." % _manifest)
		return
	var splat: String = manifest["splat"]
	_append("Importing %s (%d Gaussians). The editor does not respond while this runs: about 1.5 minutes per million." % [
			splat, manifest["gaussians"]])
	# Let the message show before the import blocks the editor.
	await get_tree().create_timer(0.3).timeout

	var fs := EditorInterface.get_resource_filesystem()
	_reimported.clear()
	fs.scan()
	var waited := 0.0
	while not _reimported.has(splat):
		await get_tree().create_timer(0.25).timeout
		waited += 0.25
		if fs.is_scanning():
			continue
		# An unchanged file is not imported again: accept the resource that is there.
		if waited > 5.0 and ResourceLoader.exists(splat):
			break
		if waited > IMPORT_TIMEOUT:
			_finish(false, "%s was not imported. Is the gdgs plugin enabled?" % splat)
			return

	var result := Builder.build(_manifest)
	if not result["ok"]:
		_finish(false, result["message"])
		return
	var scene: String = result["scene"]
	fs.scan()
	if EditorInterface.get_open_scenes().has(scene):
		EditorInterface.reload_scene_from_path(scene)
	else:
		EditorInterface.open_scene_from_path(scene)
	_finish(true, "Built %s (%d collision bodies copied)." % [scene, result["bodies"]])


func _finish(ok: bool, message: String) -> void:
	_append(("Done. " if ok else "Failed. ") + message)
	if OS.get_environment("G2S_EDITOR_SCAN") != "":
		print("g2s-editor: %s %s" % ["OK" if ok else "FAILED", message])
		if OS.get_environment("G2S_EDITOR_QUIT") != "":
			get_tree().quit(0 if ok else 1)


func _append(text: String) -> void:
	_log.add_text(text + "\n")
	if OS.get_environment("G2S_EDITOR_SCAN") != "":
		print("g2s-editor| " + text)


func _show_message(text: String) -> void:
	_log.clear()
	_append(text)
	_dialog.popup_centered()


# --- test hook -------------------------------------------------------------

func _scan_on_startup(scene_path: String) -> void:
	# The editor is still starting: wait until its first file scan is over.
	var fs := EditorInterface.get_resource_filesystem()
	await get_tree().create_timer(2.0).timeout
	while fs.is_scanning():
		await get_tree().create_timer(0.25).timeout
	EditorInterface.open_scene_from_path(scene_path)
	await get_tree().create_timer(1.0).timeout
	var scene := EditorInterface.get_edited_scene_root()
	var volume: Node3D = null
	if scene != null:
		for node in [scene] + scene.find_children("*", "Node3D", true, false):
			if node is Volume:
				volume = node
				break
	if volume == null:
		print("g2s-editor: FAILED no SplatCaptureVolume in %s" % scene_path)
		get_tree().quit(1)
		return
	print("g2s-editor: scanning %s" % scene_path)
	_start(scene_path, volume)
