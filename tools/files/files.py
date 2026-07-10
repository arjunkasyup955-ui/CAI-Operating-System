from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.files.file_providers import get_file_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=0.5)
_TIMEOUT_SECONDS = 15.0


class ReadFileArgs(BaseModel):
    path: str


class WriteFileArgs(BaseModel):
    path: str
    content: str


class AppendFileArgs(BaseModel):
    path: str
    content: str


class ReplaceTextArgs(BaseModel):
    path: str
    old_text: str
    new_text: str


class CreateFileArgs(BaseModel):
    path: str
    content: str = ""


class CreateDirectoryArgs(BaseModel):
    path: str


def read_file(path: str) -> dict:
    return get_file_provider().read_file(path).model_dump()


def write_file(path: str, content: str) -> dict:
    return get_file_provider().write_file(path, content).model_dump()


def append_file(path: str, content: str) -> dict:
    return get_file_provider().append_file(path, content).model_dump()


def replace_text(path: str, old_text: str, new_text: str) -> dict:
    return get_file_provider().replace_text(path, old_text, new_text).model_dump()


def create_file(path: str, content: str = "") -> dict:
    return get_file_provider().create_file(path, content).model_dump()


def create_directory(path: str) -> dict:
    return get_file_provider().create_directory(path).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("read_file", "Read a text file's contents", ReadFileArgs, read_file),
    ("write_file", "Create or overwrite a text file's contents", WriteFileArgs, write_file),
    ("append_file", "Append text to a file", AppendFileArgs, append_file),
    ("replace_text", "Replace a substring within a file", ReplaceTextArgs, replace_text),
    ("create_file", "Create a new file - fails if it already exists", CreateFileArgs, create_file),
    ("create_directory", "Create a directory (and parents) if it doesn't exist", CreateDirectoryArgs, create_directory),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["file_access"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
