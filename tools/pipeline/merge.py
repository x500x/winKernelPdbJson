from __future__ import annotations

import json
import shutil
from datetime import datetime

from .config import RunConfig, RunPaths, WorkerPaths
from .utils import ensure_directory, read_jsonl, write_jsonl, write_text_lines


def cleanup_worker_roots(worker_paths: list[WorkerPaths]) -> None:
    for worker_path in worker_paths:
        if worker_path.root.exists():
            shutil.rmtree(worker_path.root)


def merge_run_outputs(
    config: RunConfig,
    run_paths: RunPaths,
    worker_paths: list[WorkerPaths],
    started_at: datetime,
    finished_at: datetime,
    exit_codes: dict[str, int | None],
) -> dict:
    ensure_directory(run_paths.merged_exports_dir)
    ensure_directory(run_paths.merged_logs_dir)
    ensure_directory(run_paths.final_output_dir)

    success_records: list[dict] = []
    failure_records: list[dict] = []
    duplicate_records: list[dict] = []
    worker_counts: dict[str, int] = {}

    for worker_path in worker_paths:
        success = read_jsonl(worker_path.logs_dir / "success.jsonl")
        failure = read_jsonl(worker_path.logs_dir / "failure.jsonl")
        success_records.extend(success)
        failure_records.extend(failure)
        worker_counts[worker_path.root.name] = len(success) + len(failure)

        for export_file in sorted(worker_path.exports_dir.glob("*.json")):
            merged_destination = run_paths.merged_exports_dir / export_file.name
            if merged_destination.exists():
                duplicate_records.append(
                    {
                        "stage": "merged",
                        "source": str(export_file),
                        "destination": str(merged_destination),
                        "reason": "duplicate_export_name",
                    }
                )
                continue
            shutil.copy2(export_file, merged_destination)

    final_output_copied = 0
    for export_file in sorted(run_paths.merged_exports_dir.glob("*.json")):
        final_destination = run_paths.final_output_dir / export_file.name
        if final_destination.exists() and not config.overwrite:
            duplicate_records.append(
                {
                    "stage": "final_output",
                    "source": str(export_file),
                    "destination": str(final_destination),
                    "reason": "existing_final_output",
                }
            )
            continue
        shutil.copy2(export_file, final_destination)
        final_output_copied += 1

    write_jsonl(run_paths.merged_logs_dir / "all_success.jsonl", success_records)
    write_jsonl(run_paths.merged_logs_dir / "all_failure.jsonl", failure_records)
    write_jsonl(run_paths.merged_logs_dir / "duplicates.jsonl", duplicate_records)
    write_text_lines(
        run_paths.merged_logs_dir / "failed_urls.txt",
        [record["url"] for record in failure_records if record.get("url") and record["url"] != "<worker-fatal>"],
    )

    summary = {
        "module_name": config.module_name,
        "input_path": str(config.input_path) if config.input_path else None,
        "url_source_path": str(config.url_source_path),
        "run_root": str(run_paths.root),
        "run_id": config.run_id,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "total_urls": len(config.urls),
        "success_count": len(success_records),
        "failure_count": len(failure_records),
        "unprocessed_count": max(0, len(config.urls) - len(success_records) - len(failure_records)),
        "merged_export_count": len(list(run_paths.merged_exports_dir.glob("*.json"))),
        "duplicate_count": len(duplicate_records),
        "final_output_copied": final_output_copied,
        "worker_counts": worker_counts,
        "worker_exit_codes": exit_codes,
    }
    (run_paths.merged_logs_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return summary
