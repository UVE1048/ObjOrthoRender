import re
import shutil
import subprocess
import sys
from pathlib import Path

RELEASE_VERSION = "1.0.3"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _extract_version_from_gui(gui_path: Path) -> str:
    m = re.search(r'^\s*APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"\s*$', _read_text(gui_path), flags=re.M)
    if not m:
        raise RuntimeError(f"Failed to find APP_VERSION in: {gui_path}")
    return m.group(1)


def _validate_version(v: str) -> str:
    v = (v or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        raise SystemExit(f"Invalid version: {v} (expected X.Y.Z)")
    return v


def _replace_version(path: Path, old: str, new: str) -> None:
    text = _read_text(path)
    updated = text.replace(old, new)
    if updated == text:
        raise RuntimeError(f"No version '{old}' occurrences found in: {path}")
    _write_text(path, updated)


def _rm_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True)

def _step(i: int, total: int, title: str) -> None:
    print(f"[{i}/{total}] {title}", flush=True)


def main() -> None:
    new_version = _validate_version(RELEASE_VERSION)
    root = Path(__file__).resolve().parent

    gui_path = root / "gui_app.py"
    old_version = _extract_version_from_gui(gui_path)
    update_versions = old_version != new_version

    total_steps = 7
    step = 1
    if update_versions:
        _step(step, total_steps, f"Update version {old_version} -> {new_version}")
    else:
        _step(step, total_steps, f"Version unchanged: {new_version} (skip version update)")
    step += 1

    to_update = [
        root / "CMD.txt",
        root / "build_exe.py",
        root / "Dockerfile",
        root / "gui_app.py",
        root / "render_orthophotos.py",
        root / "README.md",
        root / "ObjOrthoRender使用说明.md",
    ]

    if update_versions:
        for p in to_update:
            _replace_version(p, old_version, new_version)

    _step(step, total_steps, "Clean dist/ and build/")
    step += 1
    _rm_tree(root / "dist")
    _rm_tree(root / "build")

    _step(step, total_steps, "Build Windows EXE (PyInstaller)")
    step += 1
    _run([sys.executable, "build_exe.py"], cwd=root)

    image_tag = f"obj-ortho-cli:{new_version}"
    _step(step, total_steps, f"Docker build {image_tag}")
    step += 1
    _run(["docker", "build", "-t", image_tag, "."], cwd=root)

    _step(step, total_steps, "Prepare docker_image/ and remove old tar")
    step += 1
    docker_image_dir = root / "docker_image"
    docker_image_dir.mkdir(parents=True, exist_ok=True)

    for fp in docker_image_dir.glob("obj-ortho-cli_*.tar"):
        try:
            fp.unlink()
        except Exception:
            pass

    tar_path = docker_image_dir / f"obj-ortho-cli_{new_version}.tar"
    _step(step, total_steps, f"Docker save -> {tar_path.name}")
    step += 1
    _run(["docker", "save", "-o", str(tar_path), image_tag], cwd=root)

    _step(step, total_steps, "Done")
    print(f"Done: version={new_version}", flush=True)
    print(f"EXE: {root / 'dist'}", flush=True)
    print(f"Docker image: {image_tag}", flush=True)
    print(f"Tar: {tar_path}", flush=True)


if __name__ == "__main__":
    main()
