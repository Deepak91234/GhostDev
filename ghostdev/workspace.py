from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

from .config import Project


DENIED = {".git", ".env", "node_modules", ".venv", ".state", "__pycache__", "verification", "config.json"}
MAX_BYTES = 500_000


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or "\\" in relative or any(part in {"..", ".", ""} or part.startswith(".") or part in DENIED for part in path.parts):
        raise ValueError("Path is outside permitted project scope")
    if any(word in path.name.lower() for word in ("secret", "credential", "id_rsa", "id_ed25519", ".pem", ".key")):
        raise ValueError("Secret files are excluded")
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Symlinks are not permitted")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes task root")
    if candidate.exists() and (not candidate.is_file() or candidate.stat().st_size > MAX_BYTES):
        raise ValueError("Only bounded regular files are permitted")
    return candidate


def permitted(relative: str, project: Project) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in project.writable)


def inventory(root: Path, project: Project) -> dict[str, str]:
    files = {}
    total = 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in DENIED and not (Path(folder)/d).is_symlink())
        for name in sorted(names):
            relative = (Path(folder)/name).relative_to(root).as_posix()
            if not permitted(relative, project):
                continue
            try:
                path = safe_path(root, relative)
                data = path.read_bytes()
                data.decode("utf-8")
            except (ValueError, UnicodeError):
                continue
            files[relative] = sha(data)
            total += len(data)
            if len(files) > 150 or total > 5_000_000:
                raise ValueError("Project exceeds MVP bounded-copy limits")
    return files


class Workspace:
    def __init__(self, task_dir: Path, project: Project):
        self.directory = task_dir
        self.root = task_dir / "project"
        self.base = task_dir / "base"
        self.project = project
        self.meta_path = task_dir / "workspace.json"

    def prepare(self, mutating: bool):
        if self.meta_path.exists():
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        original = self.project.root
        if not original.is_dir() or original.is_symlink():
            raise ValueError("Registered project root is missing or a symlink")
        commit = None
        if (original / ".git").exists():
            status = subprocess.run(["git", "-C", str(original), "status", "--porcelain"], capture_output=True, text=True, timeout=5, check=True)
            if mutating and status.stdout.strip():
                raise ValueError("Original project has uncommitted changes; mutation refused")
            commit = subprocess.run(["git", "-C", str(original), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        manifest = inventory(original, self.project)
        if not manifest:
            raise ValueError("No permitted UTF-8 files in project")
        for relative in manifest:
            data = safe_path(original, relative).read_bytes()
            for root in (self.root, self.base):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        self.meta_path.write_text(json.dumps({"manifest": manifest, "commit": commit}))

    def path(self, relative: str, *, write=False) -> Path:
        if not permitted(relative, self.project):
            raise ValueError("File pattern is not registered")
        if write and relative not in self.manifest:
            raise ValueError("MVP editing is limited to existing registered files")
        return safe_path(self.root, relative)

    @property
    def manifest(self):
        return json.loads(self.meta_path.read_text())["manifest"]

    def patch(self, relative: str, old: str, new: str):
        if not old:
            raise ValueError("An exact nonempty unique old substring is required")
        path = self.path(relative, write=True)
        source = path.read_text()
        if source.count(old) != 1:
            raise ValueError("Patch context is missing or ambiguous")
        result = source.replace(old, new, 1)
        if len(result.encode()) > MAX_BYTES:
            raise ValueError("Patched file exceeds limit")
        path.write_text(result)
        return {"path": relative, "sha256": sha(result.encode())}

    def diff(self) -> dict:
        parts, changes = [], []
        for relative in sorted(self.manifest):
            path = self.path(relative)
            old = (self.base/relative).read_bytes()
            new = path.read_bytes()
            if old != new:
                changes.append(relative)
                parts.extend(difflib.unified_diff(old.decode().splitlines(keepends=True), new.decode().splitlines(keepends=True), fromfile="a/"+relative, tofile="b/"+relative))
        patch = "".join(parts)
        return {"patch": patch, "sha256": sha(patch.encode()), "files": changes}

    def apply(self, reviewed_hash: str):
        import fcntl
        lock_path = self.directory.parent / ("apply-"+self.project.id+".lock")
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            diff = self.diff()
            if reviewed_hash != diff["sha256"] or not diff["files"]:
                raise ValueError("Reviewed diff hash no longer matches or no changes exist")
            meta = json.loads(self.meta_path.read_text())
            if inventory(self.project.root, self.project) != meta["manifest"]:
                raise ValueError("Original contents changed; refusing to overwrite concurrent edits")
            if meta["commit"]:
                current = subprocess.run(["git", "-C", str(self.project.root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
                clean = subprocess.run(["git", "-C", str(self.project.root), "status", "--porcelain"], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
                if current != meta["commit"] or clean:
                    raise ValueError("Original repository revision or status changed")
            # Cooperative per-project lock + recheck each file. Roll back only our own writes on error.
            written = []
            try:
                for relative in diff["files"]:
                    target = safe_path(self.project.root, relative)
                    if sha(target.read_bytes()) != meta["manifest"][relative]:
                        raise ValueError("Concurrent original edit detected")
                    data = self.path(relative).read_bytes()
                    temp = target.with_name(".ghostdev-"+target.name)
                    if temp.exists() or temp.is_symlink():
                        raise ValueError("Apply staging path already exists")
                    with temp.open("xb") as file:
                        file.write(data)
                    temp.chmod(target.stat().st_mode & 0o777)
                    os.replace(temp, target)
                    written.append((relative, sha(data)))
            except BaseException:
                for relative, new_hash in reversed(written):
                    target = safe_path(self.project.root, relative)
                    if sha(target.read_bytes()) == new_hash:
                        target.write_bytes((self.base/relative).read_bytes())
                raise
        return diff
