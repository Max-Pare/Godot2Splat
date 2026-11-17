"""The Unreal project side: finding the editor, installing the capture plugin, running the scan.

Only the scan runs in Unreal. The plugin (unreal/G2SCapture) writes the same capture
folder as the Godot scan, already in Godot's axes and metres, so ingest, training and
cleanup do not know which engine took the pictures. The splat goes into a Godot project.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "unreal" / "G2SCapture"
PLUGIN_NAME = "G2SCapture"
HASH_FILE = ".g2s_source_hash"   # in the installed plugin: hash of the source it was built from
LOG_TAG = "LogG2S: "


def find_uproject(path: Path) -> Path | None:
    """The .uproject file of an Unreal project given as the file or its folder; None if not one."""
    path = Path(path)
    if path.suffix == ".uproject" and path.is_file():
        return path
    if path.is_dir():
        found = sorted(path.glob("*.uproject"))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise SystemExit(f"{path} holds {len(found)} .uproject files; pass the one to use")
    return None


def is_project(path: Path) -> bool:
    return find_uproject(path) is not None


def to_map(project_dir: Path, scene: str) -> tuple[str, Path]:
    """(package path such as /Game/Maps/Room, .umap file) of a map given as a package
    path, a .umap path relative to the project, or an absolute .umap path."""
    root = Path(project_dir).resolve()
    content = root / "Content"
    if scene.startswith("/Game/"):
        rel = scene[len("/Game/"):]
        if "." in Path(rel).name:   # /Game/Maps/Room.Room
            rel = rel.rsplit(".", 1)[0]
        return "/Game/" + rel, content / (rel + ".umap")
    p = Path(scene)
    full = p.resolve() if p.is_absolute() or p.exists() else (root / p).resolve()
    if full.suffix != ".umap":
        raise SystemExit(f"not a map: {scene}. Give /Game/... or the path of a .umap file")
    try:
        rel = full.relative_to(content).with_suffix("").as_posix()
    except ValueError:
        raise SystemExit(f"map {scene} is not inside {content}")
    return "/Game/" + rel, full


def engine_dir(uproject: Path) -> Path:
    """Engine folder for the project's EngineAssociation (5.8 -> .../UE_5.8)."""
    association = json.loads(Path(uproject).read_text(encoding="utf-8-sig")).get("EngineAssociation", "")
    tried = []
    if sys.platform == "win32" and association:
        import winreg
        keys = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\EpicGames\Unreal Engine\{association}", "InstalledDirectory"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Epic Games\Unreal Engine\Builds", association)]
        for hive, key, value in keys:
            try:
                with winreg.OpenKey(hive, key) as k:
                    found = Path(winreg.QueryValueEx(k, value)[0])
                if (found / "Engine").is_dir():
                    return found
                tried.append(str(found))
            except OSError:
                pass
    for base in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Epic Games", Path.home() / "UnrealEngine"):
        candidate = base / f"UE_{association}"
        if (candidate / "Engine").is_dir():
            return candidate
        tried.append(str(candidate))
    raise SystemExit(f"Unreal Engine {association or '(no EngineAssociation)'} not found (tried {', '.join(tried) or 'nothing'}). "
                     f"Pass --unreal <path to UnrealEditor-Cmd> or set G2S_UNREAL.")


def editor_binary(uproject: Path, given: str = "") -> Path:
    """The console build of the editor: it prints the log to standard output."""
    if given:
        p = Path(given)
        if not p.is_file():
            raise SystemExit(f"Unreal editor not found: {given}")
        return p
    name = "UnrealEditor-Cmd.exe" if sys.platform == "win32" else "UnrealEditor"
    platform = {"win32": "Win64", "darwin": "Mac"}.get(sys.platform, "Linux")
    p = engine_dir(uproject) / "Engine" / "Binaries" / platform / name
    if not p.is_file():
        raise SystemExit(f"Unreal editor not found at {p}")
    return p


def source_hash() -> str:
    h = hashlib.sha256()
    for f in sorted(PLUGIN.rglob("*")):
        if f.is_file():
            h.update(f.relative_to(PLUGIN).as_posix().encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def plugin_current(project_dir: Path) -> bool:
    installed = Path(project_dir) / "Plugins" / PLUGIN_NAME
    stamp = installed / HASH_FILE
    return stamp.exists() and stamp.read_text().strip() == source_hash() and (installed / "Binaries").is_dir()


def build_plugin(uproject: Path, dest: Path) -> None:
    """Compile the plugin for this project's engine into `dest` (about 2 minutes)."""
    root = engine_dir(uproject)
    script = root / "Engine" / "Build" / "BatchFiles" / ("RunUAT.bat" if sys.platform == "win32" else "RunUAT.sh")
    platform = {"win32": "Win64", "darwin": "Mac"}.get(sys.platform, "Linux")
    with tempfile.TemporaryDirectory(prefix="g2s_plugin_") as tmp:
        package = Path(tmp) / PLUGIN_NAME
        print(f"install: compiling the {PLUGIN_NAME} plugin for {root.name} (a few minutes)", flush=True)
        run = subprocess.run([str(script), "BuildPlugin", f"-Plugin={PLUGIN / (PLUGIN_NAME + '.uplugin')}",
                              f"-Package={package}", f"-TargetPlatforms={platform}"],
                             capture_output=True, text=True, errors="replace")
        if run.returncode != 0 or not (package / "Binaries").is_dir():
            errors = [ln for ln in run.stdout.splitlines() if "error" in ln.lower()]
            raise SystemExit("install: the plugin did not compile (a C++ toolchain for Unreal is needed, "
                             "e.g. Visual Studio 2022 Build Tools):\n" + "\n".join((errors or run.stdout.splitlines())[-30:]))
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(package, dest, ignore=shutil.ignore_patterns("Intermediate"))
    (dest / HASH_FILE).write_text(source_hash())


def install(project: Path) -> list[str]:
    """Copy and compile the capture plugin into the Unreal project; write the command
    the toolbar button runs. Returns what was done."""
    uproject = find_uproject(project)
    if uproject is None:
        raise SystemExit(f"{project} is not an Unreal project (no .uproject)")
    project_dir = uproject.parent
    done = []
    dest = project_dir / "Plugins" / PLUGIN_NAME
    if not plugin_current(project_dir):
        existed = dest.exists()
        build_plugin(uproject, dest)
        done.append(f"{'updated' if existed else 'installed'} Plugins/{PLUGIN_NAME} (restart the editor if it is open)")
    work = project_dir / ".g2s"
    work.mkdir(exist_ok=True)
    command = {"argv": [sys.executable, "-u", "-m", "g2s", "--merge-stderr"]}
    path = work / "command.json"
    text = json.dumps(command, indent=2)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        done.append("wrote .g2s/command.json for the Scan to Splat button")
    return done


def run_capture(uproject: Path, map_path: str, out: Path, editor: str, timeout: float,
                origin: str = "", box: str = "", settings: list[str] | None = None) -> tuple[int, str]:
    """Open the map in the editor, scan it into `out`, quit. Echoes the plugin's log lines.
    `origin`, `box` and `settings`: see G2SCaptureEditorModule.cpp (-G2SOrigin, -G2SBox, -G2SSet).
    Returns (exit code, the plugin's log lines)."""
    exe = editor_binary(uproject, editor)
    args = [str(exe), str(uproject), map_path, f"-G2SCapture={out}", f"-G2SMap={map_path}",
            "-unattended", "-nosplash", "-nop4", "-nosourcecontrol", "-NoLiveCoding", "-stdout",
            "-FullStdOutLogOutput"]
    if origin:
        args.append(f"-G2SOrigin={origin}")
    if box:
        args.append(f"-G2SBox={box}")
    if settings:
        args.append(f"-G2SSet={';'.join(settings)}")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, stop)
    timer.start()
    mine, tail = [], []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail = (tail + [line])[-40:]
            if LOG_TAG in line:
                text = line.split(LOG_TAG, 1)[1]
                for level in ("Display: ", "Warning: ", "Error: "):
                    text = text.removeprefix(level)
                mine.append(text)
                print(text, flush=True)
        code = proc.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise SystemExit(f"capture: Unreal did not finish within {timeout:.0f} s")
    if code != 0 and not mine:
        print("capture: last lines from Unreal:\n" + "\n".join(tail), flush=True)
    return code, "\n".join(mine)
