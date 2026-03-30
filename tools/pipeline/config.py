from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .utils import build_worker_dir, ensure_directory


@dataclass(frozen=True)
class ToolPaths:
    fetch_script: Path
    exporter_exe: Path


@dataclass(frozen=True)
class WorkerPaths:
    root: Path
    module_dir: Path
    symbols_dir: Path
    exports_dir: Path
    logs_dir: Path


@dataclass(frozen=True)
class RunPaths:
    root: Path
    merged_root: Path
    merged_exports_dir: Path
    merged_logs_dir: Path
    final_output_dir: Path


@dataclass(frozen=True)
class RunConfig:
    workspace_root: Path
    input_path: Path | None
    url_source_path: Path
    module_name: str
    workers: int
    final_output_root: Path
    run_root: Path
    run_id: str
    keep_workdirs: bool
    overwrite: bool
    failed_only_path: Path | None
    tool_paths: ToolPaths
    urls: list[str]


def build_tool_paths(workspace_root: Path) -> ToolPaths:
    return ToolPaths(
        fetch_script=workspace_root / "tools" / "fetch_ms_symbol.py",
        exporter_exe=workspace_root / "tools" / "PdbJsonExporter" / "PdbJsonExporter.exe",
    )


def ensure_tool_paths(tool_paths: ToolPaths) -> None:
    missing_paths = [
        str(path)
        for path in (tool_paths.fetch_script, tool_paths.exporter_exe)
        if not path.is_file()
    ]
    if missing_paths:
        missing_text = ", ".join(missing_paths)
        raise FileNotFoundError(f"required tool files are missing: {missing_text}")


def build_worker_paths(worker_root: Path) -> WorkerPaths:
    return WorkerPaths(
        root=worker_root,
        module_dir=worker_root / "module",
        symbols_dir=worker_root / "symbols",
        exports_dir=worker_root / "exports",
        logs_dir=worker_root / "logs",
    )


def ensure_worker_directories(worker_paths: WorkerPaths) -> WorkerPaths:
    ensure_directory(worker_paths.root)
    ensure_directory(worker_paths.module_dir)
    ensure_directory(worker_paths.symbols_dir)
    ensure_directory(worker_paths.exports_dir)
    ensure_directory(worker_paths.logs_dir)
    return worker_paths


def prepare_run_layout(
    module_name: str,
    run_root: Path,
    run_id: str,
    worker_count: int,
    final_output_root: Path,
) -> tuple[RunPaths, list[WorkerPaths]]:
    root = ensure_directory(run_root / module_name / run_id)
    merged_root = ensure_directory(root / "merged")
    merged_exports_dir = ensure_directory(merged_root / "exports")
    merged_logs_dir = ensure_directory(merged_root / "logs")
    final_output_dir = final_output_root / module_name

    run_paths = RunPaths(
        root=root,
        merged_root=merged_root,
        merged_exports_dir=merged_exports_dir,
        merged_logs_dir=merged_logs_dir,
        final_output_dir=final_output_dir,
    )

    worker_paths: list[WorkerPaths] = []
    for worker_index in range(1, worker_count + 1):
        worker_root = build_worker_dir(root, worker_index)
        worker_paths.append(ensure_worker_directories(build_worker_paths(worker_root)))

    return run_paths, worker_paths
