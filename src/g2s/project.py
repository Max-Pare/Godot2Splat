"""The Godot project side: addon install, project.godot edits, running Godot."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ADDONS = Path(__file__).resolve().parents[2] / "godot" / "addons"
PLUGINS = ["res://addons/gdgs/plugin.cfg", "res://addons/godot2splat/plugin.cfg"]
WORK_DIR = ".g2s"   # inside the project; Godot skips folders that start with a dot


def check_project(project: Path) -> None:
    if not (Path(project) / "project.godot").is_file():
        raise SystemExit(f"{project} is not a Godot project (no project.godot)")


def res_to_path(project: Path, res: str) -> Path:
    """Filesystem path of a res:// path."""
    if not res.startswith("res://"):
        raise SystemExit(f"not a res:// path: {res}")
    return Path(project) / res[len("res://"):]


def to_res(project: Path, scene: str) -> str:
    """res:// path of a scene given as res://, relative to the project, or absolute."""
    if scene.startswith("res://"):
        return scene
    p = Path(scene)
    root = Path(project).resolve()
    full = p.resolve() if p.is_absolute() or p.exists() else (root / p).resolve()
    try:
        return "res://" + full.relative_to(root).as_posix()
    except ValueError:
        raise SystemExit(f"scene {scene} is not inside the project {project}")


def enable_plugins(text: str, plugins: list[str]) -> str:
    """project.godot text with `plugins` added to the enabled editor plugins."""
    line = re.search(r"^enabled=PackedStringArray\((.*)\)[ \t]*$", text, re.M)
    section = re.search(r"^\[editor_plugins\][ \t]*$", text, re.M)
    if section and line and line.start() > section.start():
        have = re.findall(r'"([^"]*)"', line.group(1))
        merged = have + [p for p in plugins if p not in have]
        new = "enabled=PackedStringArray(%s)" % ", ".join(f'"{p}"' for p in merged)
        return text[:line.start()] + new + text[line.end():]
    new = "enabled=PackedStringArray(%s)" % ", ".join(f'"{p}"' for p in plugins)
    if section:
        return text[:section.end()] + "\n\n" + new + text[section.end():]
    return text.rstrip("\n") + "\n\n[editor_plugins]\n\n" + new + "\n"


def install(project: Path, force: bool = False) -> list[str]:
    """Copy the addons into the project and enable them. Returns what was done.

    godot2splat is always refreshed. An existing gdgs addon is left alone unless
    `force`: the project may carry its own version.
    """
    project = Path(project)
    check_project(project)
    if not ADDONS.is_dir():
        raise SystemExit(f"addons not found at {ADDONS}")
    done = []
    for name in ("godot2splat", "gdgs"):
        dst = project / "addons" / name
        if name == "gdgs" and dst.exists() and not force:
            continue
        existed = dst.exists()
        if existed:
            # Scripts removed from the addon since the last install must not linger.
            # Godot's own files (.uid, .import) are kept so resource ids stay stable.
            for old in dst.rglob("*"):
                if old.is_file() and old.suffix not in (".uid", ".import"):
                    if not (ADDONS / name / old.relative_to(dst)).exists():
                        old.unlink()
        shutil.copytree(ADDONS / name, dst, dirs_exist_ok=True)
        done.append(f"{'updated' if existed else 'installed'} addons/{name}")

    proj = project / "project.godot"
    text = proj.read_text(encoding="utf-8")
    new = enable_plugins(text, PLUGINS)
    if new != text:
        proj.write_text(new, encoding="utf-8")
        done.append("enabled the plugins in project.godot (restart the editor if it is open)")

    work = project / WORK_DIR
    work.mkdir(exist_ok=True)
    (work / ".gdignore").touch()
    # How the editor button starts this tool: the interpreter of this install.
    # -u and --merge-stderr: the button shows one stream, line by line as it is printed.
    command = {"argv": [sys.executable, "-u", "-m", "g2s", "--merge-stderr"]}
    (work / "command.json").write_text(json.dumps(command, indent=2))
    return done


def addons_current(project: Path) -> bool:
    """True when the project's godot2splat addon matches this install and gdgs is present."""
    project = Path(project)
    if not (project / "addons" / "gdgs" / "plugin.cfg").exists():
        return False
    src = ADDONS / "godot2splat"
    for f in src.rglob("*"):
        if f.is_file():
            other = project / "addons" / "godot2splat" / f.relative_to(src)
            if not other.exists() or other.read_bytes() != f.read_bytes():
                return False
    text = (project / "project.godot").read_text(encoding="utf-8")
    return enable_plugins(text, PLUGINS) == text and (project / WORK_DIR / "command.json").exists()


def run_godot(godot: str, args: list[str], timeout: float, prefix: str,
              quiet: bool = False) -> tuple[int, str]:
    """Run Godot, echoing its output line by line with `prefix`. Returns (exit code, output)."""
    if shutil.which(godot) is None:
        raise SystemExit(f"Godot not found ('{godot}'). Install Godot 4.3+ or pass --godot.")
    proc = subprocess.Popen([godot, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    # Reading the output blocks for as long as Godot runs, so the time limit needs its own thread.
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, stop)
    timer.start()
    lines = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if not quiet and line.strip():
                print(f"{prefix}{line}", flush=True)
        code = proc.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise SystemExit(f"{prefix}Godot did not finish within {timeout:.0f} s")
    return code, "\n".join(lines)


def import_assets(project: Path, godot: str, timeout: float = 3600.0) -> None:
    """Let Godot import new and changed files (the editor does the same on focus)."""
    code, out = run_godot(godot, ["--headless", "--path", str(project), "--import"],
                          timeout, "godot: ", quiet=True)
    for line in out.splitlines():
        if "[gdgs]" in line or line.startswith("ERROR") or line.startswith("SCRIPT ERROR"):
            print(f"godot: {line}", flush=True)
    if code != 0:
        raise SystemExit(f"godot --import failed with exit code {code}:\n{out[-2000:]}")
