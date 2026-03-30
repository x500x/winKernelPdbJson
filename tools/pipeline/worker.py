from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

from .config import RunConfig, WorkerPaths, build_worker_paths, ensure_worker_directories
from .utils import append_jsonl, build_worker_dir, clear_directory, iso_timestamp, trim_output


def build_module_destination(worker_paths: WorkerPaths, module_name: str) -> Path:
    return worker_paths.module_dir / module_name


def build_fetch_command(fetch_script: Path, output_dir: Path, module_path: Path) -> list[str]:
    return [
        sys.executable,
        str(fetch_script),
        "--output-dir",
        str(output_dir),
        str(module_path),
    ]


def build_export_command(exporter_exe: Path, pdb_path: Path, output_dir: Path) -> list[str]:
    return [str(exporter_exe), str(pdb_path), str(output_dir)]


def worker_log_path(worker_paths: WorkerPaths) -> Path:
    return worker_paths.logs_dir / "worker.log"


def write_worker_log(worker_paths: WorkerPaths, message: str) -> None:
    log_path = worker_log_path(worker_paths)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{iso_timestamp()}] {message}\n")


def reset_temp_directories(worker_paths: WorkerPaths) -> None:
    clear_directory(worker_paths.module_dir)
    clear_directory(worker_paths.symbols_dir)


def extract_saved_path(stdout: str) -> Path | None:
    for line in stdout.splitlines():
        if line.startswith("saved_path"):
            _, _, value = line.partition(":")
            candidate = value.strip()
            if candidate:
                return Path(candidate)
    return None


def extract_export_path(stdout: str) -> Path | None:
    for line in stdout.splitlines():
        if line.startswith("Export finished:"):
            _, _, value = line.partition(":")
            candidate = value.strip()
            if candidate:
                return Path(candidate)
    return None


def download_module(url: str, destination: Path, timeout: int = 60) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "pdb-pipeline/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
    destination.write_bytes(data)


def build_success_record(
    worker_index: int,
    url: str,
    module_path: Path,
    pdb_path: Path,
    json_path: Path,
) -> dict:
    return {
        "timestamp": iso_timestamp(),
        "worker": f"worker_{worker_index}",
        "worker_index": worker_index,
        "url": url,
        "module_path": str(module_path),
        "pdb_path": str(pdb_path),
        "json_path": str(json_path),
    }


def build_failure_record(
    worker_index: int,
    url: str,
    stage: str,
    error: str,
    returncode: int | None = None,
    stdout: str = "",
    stderr: str = "",
) -> dict:
    record = {
        "timestamp": iso_timestamp(),
        "worker": f"worker_{worker_index}",
        "worker_index": worker_index,
        "url": url,
        "stage": stage,
        "error": error,
    }
    if returncode is not None:
        record["returncode"] = returncode
    if stdout:
        record["stdout"] = trim_output(stdout)
    if stderr:
        record["stderr"] = trim_output(stderr)
    return record


def process_single_url(worker_index: int, config: RunConfig, worker_paths: WorkerPaths, url: str) -> None:
    success_log_path = worker_paths.logs_dir / "success.jsonl"
    failure_log_path = worker_paths.logs_dir / "failure.jsonl"
    module_path = build_module_destination(worker_paths, config.module_name)

    reset_temp_directories(worker_paths)
    write_worker_log(worker_paths, f"start url={url}")

    try:
        download_module(url, module_path)
    except Exception as exc:
        record = build_failure_record(worker_index, url, "download", str(exc))
        append_jsonl(failure_log_path, record)
        write_worker_log(worker_paths, f"download failed url={url} error={exc}")
        return

    fetch_command = build_fetch_command(
        config.tool_paths.fetch_script,
        worker_paths.symbols_dir,
        module_path,
    )
    fetch_result = subprocess.run(fetch_command, capture_output=True, text=True)
    if fetch_result.returncode != 0:
        record = build_failure_record(
            worker_index,
            url,
            "fetch_symbol",
            "fetch_ms_symbol.py returned a non-zero exit code",
            returncode=fetch_result.returncode,
            stdout=fetch_result.stdout,
            stderr=fetch_result.stderr,
        )
        append_jsonl(failure_log_path, record)
        write_worker_log(worker_paths, f"fetch failed url={url} returncode={fetch_result.returncode}")
        reset_temp_directories(worker_paths)
        return

    pdb_path = extract_saved_path(fetch_result.stdout)
    if pdb_path is None or not pdb_path.is_file():
        pdb_candidates = sorted(worker_paths.symbols_dir.glob("*.pdb"))
        pdb_path = pdb_candidates[0] if pdb_candidates else None
    if pdb_path is None or not pdb_path.is_file():
        record = build_failure_record(
            worker_index,
            url,
            "fetch_symbol",
            "symbol download completed but no pdb file was found",
            stdout=fetch_result.stdout,
            stderr=fetch_result.stderr,
        )
        append_jsonl(failure_log_path, record)
        write_worker_log(worker_paths, f"missing pdb url={url}")
        reset_temp_directories(worker_paths)
        return

    export_command = build_export_command(
        config.tool_paths.exporter_exe,
        pdb_path,
        worker_paths.exports_dir,
    )
    export_result = subprocess.run(export_command, capture_output=True, text=True)
    if export_result.returncode != 0:
        record = build_failure_record(
            worker_index,
            url,
            "export_json",
            "PdbJsonExporter.exe returned a non-zero exit code",
            returncode=export_result.returncode,
            stdout=export_result.stdout,
            stderr=export_result.stderr,
        )
        append_jsonl(failure_log_path, record)
        write_worker_log(worker_paths, f"export failed url={url} returncode={export_result.returncode}")
        reset_temp_directories(worker_paths)
        return

    json_path = extract_export_path(export_result.stdout)
    if json_path is None or not json_path.is_file():
        record = build_failure_record(
            worker_index,
            url,
            "export_json",
            "exporter completed but no json output was found",
            stdout=export_result.stdout,
            stderr=export_result.stderr,
        )
        append_jsonl(failure_log_path, record)
        write_worker_log(worker_paths, f"missing json url={url}")
        reset_temp_directories(worker_paths)
        return

    record = build_success_record(worker_index, url, module_path, pdb_path, json_path)
    append_jsonl(success_log_path, record)
    write_worker_log(worker_paths, f"success url={url} json={json_path.name}")
    reset_temp_directories(worker_paths)


def run_worker_loop(worker_index: int, config: RunConfig, urls: list[str], worker_paths: WorkerPaths) -> None:
    for url in urls:
        process_single_url(worker_index, config, worker_paths, url)


def worker_main(worker_index: int, task_queue, config: RunConfig) -> None:
    worker_root = build_worker_dir(config.run_root / config.module_name / config.run_id, worker_index)
    worker_paths = ensure_worker_directories(build_worker_paths(worker_root))
    write_worker_log(worker_paths, f"worker started index={worker_index}")

    try:
        while True:
            url = task_queue.get()
            if url is None:
                break
            process_single_url(worker_index, config, worker_paths, url)
    except Exception as exc:
        record = build_failure_record(
            worker_index,
            url="<worker-fatal>",
            stage="worker_fatal",
            error=str(exc),
        )
        append_jsonl(worker_paths.logs_dir / "failure.jsonl", record)
        write_worker_log(worker_paths, f"worker fatal error={exc}")
        raise
    finally:
        write_worker_log(worker_paths, f"worker stopped index={worker_index}")


def worker_main_urls(worker_index: int, urls: list[str], config: RunConfig) -> None:
    worker_root = build_worker_dir(config.run_root / config.module_name / config.run_id, worker_index)
    worker_paths = ensure_worker_directories(build_worker_paths(worker_root))
    write_worker_log(worker_paths, f"worker started index={worker_index}")

    try:
        run_worker_loop(worker_index, config, urls, worker_paths)
    except Exception as exc:
        record = build_failure_record(
            worker_index,
            url="<worker-fatal>",
            stage="worker_fatal",
            error=str(exc),
        )
        append_jsonl(worker_paths.logs_dir / "failure.jsonl", record)
        write_worker_log(worker_paths, f"worker fatal error={exc}")
        raise
    finally:
        write_worker_log(worker_paths, f"worker stopped index={worker_index}")
