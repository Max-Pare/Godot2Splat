"""The Godot project side: project.godot edits, path handling, addon install."""
import json
from pathlib import Path

import pytest

from g2s import project
from g2s.cli import default_workspace

GDGS, OURS = project.PLUGINS


def test_enable_plugins_adds_a_missing_section():
    text = '[application]\n\nconfig/name="x"\n'
    out = project.enable_plugins(text, project.PLUGINS)
    assert out.startswith(text.rstrip("\n"))
    assert f'[editor_plugins]\n\nenabled=PackedStringArray("{GDGS}", "{OURS}")\n' in out


def test_enable_plugins_keeps_what_is_enabled():
    text = ('[editor_plugins]\n\nenabled=PackedStringArray("res://addons/other/plugin.cfg", "%s")\n\n'
            '[rendering]\n\nrenderer/rendering_method="forward_plus"\n' % GDGS)
    out = project.enable_plugins(text, project.PLUGINS)
    assert f'enabled=PackedStringArray("res://addons/other/plugin.cfg", "{GDGS}", "{OURS}")' in out
    assert out.count("enabled=") == 1 and out.endswith('renderer/rendering_method="forward_plus"\n')
    assert project.enable_plugins(out, project.PLUGINS) == out   # nothing left to do


def test_enable_plugins_fills_an_empty_section():
    out = project.enable_plugins("[editor_plugins]\n\n[rendering]\n", [OURS])
    assert out.index("enabled=") < out.index("[rendering]")


def test_scene_paths(tmp_path):
    (tmp_path / "levels").mkdir()
    (tmp_path / "levels" / "a.tscn").write_text("")
    assert project.to_res(tmp_path, "res://levels/a.tscn") == "res://levels/a.tscn"
    assert project.to_res(tmp_path, "levels/a.tscn") == "res://levels/a.tscn"
    assert project.to_res(tmp_path, str(tmp_path / "levels" / "a.tscn")) == "res://levels/a.tscn"
    assert project.res_to_path(tmp_path, "res://levels/a.tscn") == tmp_path / "levels" / "a.tscn"
    with pytest.raises(SystemExit, match="not inside the project"):
        project.to_res(tmp_path, "/etc/hosts")
    assert default_workspace(tmp_path, "res://levels/a.tscn") == tmp_path / ".g2s" / "a"


def test_install_copies_addons_and_is_repeatable(tmp_path):
    with pytest.raises(SystemExit, match="not a Godot project"):
        project.install(tmp_path)
    (tmp_path / "project.godot").write_text('config_version=5\n\n[application]\n\nconfig/name="t"\n')
    assert not project.addons_current(tmp_path)
    done = project.install(tmp_path)
    assert len(done) == 3
    assert (tmp_path / "addons" / "godot2splat" / "capture" / "capture_runner.gd").exists()
    assert (tmp_path / "addons" / "gdgs" / "plugin.cfg").exists()
    assert OURS in (tmp_path / "project.godot").read_text()
    argv = json.loads((tmp_path / ".g2s" / "command.json").read_text())["argv"]
    assert Path(argv[0]).exists() and argv[-3:] == ["-m", "g2s", "--merge-stderr"]
    assert project.addons_current(tmp_path)

    # A script that no longer ships is removed; the project's own gdgs is left alone.
    stale = tmp_path / "addons" / "godot2splat" / "old_script.gd"
    stale.write_text("")
    marker = tmp_path / "addons" / "gdgs" / "plugin.cfg"
    marker.write_text("changed by the project")
    assert project.install(tmp_path) == ["updated addons/godot2splat"]
    assert not stale.exists() and marker.read_text() == "changed by the project"
    project.install(tmp_path, force=True)
    assert marker.read_text() != "changed by the project"
