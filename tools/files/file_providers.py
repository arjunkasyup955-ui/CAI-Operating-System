from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

# tools/files/file_providers.py -> tools/files -> tools -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROTECTED_DIR_NAMES = {".git", ".venv", "venv", "__pycache__", "node_modules"}
_PROTECTED_FILE_NAMES = {".env"}
# The frozen Phase 0 kernel - the File Editor must never be able to touch it, not even
# via approval (this is a hard code-level ban, like Git's destructive-op refusal).
# backend/frontend/docs/database/docker are the AFOS platform's own real source trees -
# added as defense-in-depth after AI Builder's generic scaffold template collided with
# them (see Fix A, docs/bug_investigation_log.md); AI-Builder-generated code now lives
# under generated_ventures/{venture_id}/ instead, so none of these should ever be a
# legitimate target for create_file/write_file again, but this backstops that intent in
# code rather than relying solely on the path-prefixing convention holding forever.
_PROTECTED_TOP_LEVEL_DIRS = {"core", "backend", "frontend", "docs", "database", "docker"}


class PathTraversalError(Exception):
    pass


class ProtectedPathError(Exception):
    pass


def resolve_safe_path(path_str: str, root: Path) -> Path:
    """Confines every file operation to `root` and refuses anything that looks like an
    escape attempt or a touch on a protected area. Absolute paths are rejected before
    any join happens - Path(root) / Path(absolute) silently discards root and returns
    the absolute path unchanged, which would otherwise defeat the whole sandbox.
    """
    if not path_str or not path_str.strip():
        raise ValueError("path must not be empty")

    raw = Path(path_str)
    if raw.is_absolute():
        raise PathTraversalError(f"absolute paths are not allowed: {path_str}")
    if ".." in raw.parts:
        raise PathTraversalError(f"path traversal ('..') is not allowed: {path_str}")

    candidate = (root / raw).resolve()
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError:
        raise PathTraversalError(f"path escapes the sandboxed root: {path_str}") from None

    parts = relative.parts
    if any(part in _PROTECTED_DIR_NAMES for part in parts):
        raise ProtectedPathError(f"refused to touch a protected directory in path: {path_str}")
    if parts and parts[0] in _PROTECTED_TOP_LEVEL_DIRS:
        raise ProtectedPathError(f"refused to touch the frozen kernel path: {path_str}")
    if candidate.name in _PROTECTED_FILE_NAMES:
        raise ProtectedPathError(f"refused to touch a protected file: {path_str}")

    return candidate


class FileOperationResult(BaseModel):
    success: bool
    operation: str
    path: str = ""
    content: str = ""
    bytes_written: int = 0
    error: str = ""


class FileEditorProvider(Protocol):
    name: str

    def read_file(self, path: str) -> FileOperationResult: ...
    def write_file(self, path: str, content: str) -> FileOperationResult: ...
    def append_file(self, path: str, content: str) -> FileOperationResult: ...
    def replace_text(self, path: str, old_text: str, new_text: str) -> FileOperationResult: ...
    def create_file(self, path: str, content: str = "") -> FileOperationResult: ...
    def create_directory(self, path: str) -> FileOperationResult: ...


class LocalFileSystemProvider:
    """Default FileEditorProvider - real filesystem I/O, confined to `root` (the repo
    root by default). Never deletes anything: only read/write/append/replace/create
    are implemented, so there is no delete capability to misuse in the first place.
    """

    name = "local_filesystem"

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root).resolve() if root else _REPO_ROOT

    def read_file(self, path: str) -> FileOperationResult:
        target = resolve_safe_path(path, self._root)
        if not target.exists():
            return FileOperationResult(success=False, operation="read_file", path=path, error="file does not exist")
        if not target.is_file():
            return FileOperationResult(success=False, operation="read_file", path=path, error="path is not a file")
        content = target.read_text(encoding="utf-8")
        return FileOperationResult(success=True, operation="read_file", path=path, content=content)

    def write_file(self, path: str, content: str) -> FileOperationResult:
        target = resolve_safe_path(path, self._root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return FileOperationResult(
            success=True, operation="write_file", path=path, bytes_written=len(content.encode("utf-8"))
        )

    def append_file(self, path: str, content: str) -> FileOperationResult:
        target = resolve_safe_path(path, self._root)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(content)
        return FileOperationResult(
            success=True, operation="append_file", path=path, bytes_written=len(content.encode("utf-8"))
        )

    def replace_text(self, path: str, old_text: str, new_text: str) -> FileOperationResult:
        target = resolve_safe_path(path, self._root)
        if not target.exists():
            return FileOperationResult(success=False, operation="replace_text", path=path, error="file does not exist")
        content = target.read_text(encoding="utf-8")
        if old_text not in content:
            return FileOperationResult(
                success=False, operation="replace_text", path=path, error="old_text not found in file"
            )
        new_content = content.replace(old_text, new_text)
        target.write_text(new_content, encoding="utf-8")
        return FileOperationResult(
            success=True, operation="replace_text", path=path, bytes_written=len(new_content.encode("utf-8"))
        )

    def create_file(self, path: str, content: str = "") -> FileOperationResult:
        target = resolve_safe_path(path, self._root)
        if target.exists():
            return FileOperationResult(
                success=False, operation="create_file", path=path, error="file already exists - use write_file to overwrite"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return FileOperationResult(
            success=True, operation="create_file", path=path, bytes_written=len(content.encode("utf-8"))
        )

    def create_directory(self, path: str) -> FileOperationResult:
        target = resolve_safe_path(path, self._root)
        target.mkdir(parents=True, exist_ok=True)
        return FileOperationResult(success=True, operation="create_directory", path=path)


_provider: FileEditorProvider = LocalFileSystemProvider()


def get_file_provider() -> FileEditorProvider:
    return _provider


def set_file_provider(provider: FileEditorProvider) -> None:
    """Swappable, not cached - lets tests inject a fake provider (or a real one rooted
    at an isolated temp directory) without touching the actual repository."""
    global _provider
    _provider = provider
