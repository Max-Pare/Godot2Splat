"""The Unreal project helpers, without Unreal."""
import json

import pytest

from g2s import unreal
from g2s.cli import default_workspace


def _project(tmp_path):
    (tmp_path / "Game.uproject").write_text(json.dumps({"FileVersion": 3, "EngineAssociation": "5.8"}))
    (tmp_path / "Content" / "Maps").mkdir(parents=True)
    (tmp_path / "Content" / "Maps" / "Room.umap").write_bytes(b"")
    return tmp_path


def test_finds_the_uproject(tmp_path):
    root = _project(tmp_path)
    assert unreal.find_uproject(root) == root / "Game.uproject"
    assert unreal.find_uproject(root / "Game.uproject") == root / "Game.uproject"
    assert unreal.find_uproject(root / "Content") is None
    (root / "Other.uproject").write_text("{}")
    with pytest.raises(SystemExit):
        unreal.find_uproject(root)


@pytest.mark.parametrize("scene", ["/Game/Maps/Room", "/Game/Maps/Room.Room", "Content/Maps/Room.umap"])
def test_map_paths(tmp_path, scene):
    root = _project(tmp_path)
    package, file = unreal.to_map(root, scene)
    assert package == "/Game/Maps/Room"
    assert file == (root / "Content" / "Maps" / "Room.umap").resolve()
    assert default_workspace(root, package) == root / ".g2s" / "Room"


def test_map_outside_content_is_refused(tmp_path):
    root = _project(tmp_path)
    (root / "Room.umap").write_bytes(b"")
    with pytest.raises(SystemExit):
        unreal.to_map(root, str(root / "Room.umap"))


def test_plugin_source_hash_is_stable():
    assert unreal.source_hash() == unreal.source_hash()
    assert (unreal.PLUGIN / "G2SCapture.uplugin").is_file()
