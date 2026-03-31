from __future__ import annotations

import argparse
import multiprocessing
from datetime import datetime
from pathlib import Path
from typing import Sequence

from pipeline.config import RunConfig, build_tool_paths, ensure_tool_paths, prepare_run_layout
from pipeline.merge import cleanup_worker_roots, merge_run_outputs
from pipeline.utils import (
    build_run_id,
    derive_default_final_output_root,
    derive_module_name,
    infer_module_name_from_failed_list,
    read_urls,
    split_urls_evenly,
)
from pipeline.worker import worker_main, worker_main_urls


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run symbol download and JSON export for one deduplicated URL file."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Path to a single *.url.dedup.txt or *.url.txt file.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Number of worker processes. Defaults to 5.",
    )
    parser.add_argument(
        "--final-output-root",
        type=Path,
        help="Directory where merged JSON outputs are copied by module name.",
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        help="Directory used to store per-run worker directories and merged outputs.",
    )
    parser.add_argument(
        "--keep-workdirs",
        action="store_true",
        help="Keep worker directories after merge completes.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files in the final output directory.",
    )
    parser.add_argument(
        "--failed-only",
        type=Path,
        help="Re-run URLs from a previous merged/logs/failed_urls.txt file.",
    )
    parser.add_argument(
        "--module-name",
        help="Override the derived module name.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = create_argument_parser()
    args = parser.parse_args(argv)

    if not args.input and not args.failed_only:
        parser.error("either --input or --failed-only is required")
    if args.workers <= 0:
        parser.error("--workers must be greater than 0")
    return args


def build_run_config(args: argparse.Namespace) -> RunConfig:
    workspace_root = Path(__file__).resolve().parent
    input_path = args.input.resolve() if args.input else None
    failed_only_path = args.failed_only.resolve() if args.failed_only else None

    url_source_path = failed_only_path or input_path
    if url_source_path is None:
        raise ValueError("missing URL source path")
    if not url_source_path.is_file():
        raise FileNotFoundError(f"input file not found: {url_source_path}")

    if args.module_name:
        module_name = args.module_name
    elif input_path is not None:
        module_name = derive_module_name(input_path)
    elif failed_only_path is not None:
        module_name = infer_module_name_from_failed_list(failed_only_path)
    else:
        raise ValueError("cannot determine module name")

    if args.final_output_root is not None:
        final_output_root = args.final_output_root.resolve()
    elif input_path is not None:
        final_output_root = derive_default_final_output_root(input_path)
    else:
        raise ValueError("--final-output-root is required when only --failed-only is provided")

    run_root = args.run_root.resolve() if args.run_root else workspace_root / "runs"
    run_id = build_run_id()
    tool_paths = build_tool_paths(workspace_root)
    ensure_tool_paths(tool_paths)

    urls = read_urls(url_source_path)
    if not urls:
        raise ValueError(f"no URLs found in input file: {url_source_path}")

    return RunConfig(
        workspace_root=workspace_root,
        input_path=input_path,
        url_source_path=url_source_path,
        module_name=module_name,
        workers=args.workers,
        final_output_root=final_output_root,
        run_root=run_root,
        run_id=run_id,
        keep_workdirs=args.keep_workdirs,
        overwrite=args.overwrite,
        failed_only_path=failed_only_path,
        tool_paths=tool_paths,
        urls=urls,
    )


def run_pipeline(config: RunConfig) -> int:
    started_at = datetime.now()
    run_paths, worker_paths = prepare_run_layout(
        module_name=config.module_name,
        run_root=config.run_root,
        run_id=config.run_id,
        worker_count=config.workers,
        final_output_root=config.final_output_root,
    )

    processes: list[multiprocessing.Process] = []
    queue_creation_failed = False

    try:
        task_queue = multiprocessing.Queue()
        for url in config.urls:
            task_queue.put(url)
        for _ in range(config.workers):
            task_queue.put(None)

        for worker_index in range(1, config.workers + 1):
            process = multiprocessing.Process(
                target=worker_main,
                args=(worker_index, task_queue, config),
                name=f"worker_{worker_index}",
            )
            process.start()
            processes.append(process)
    except PermissionError:
        queue_creation_failed = True
        for worker_index, worker_urls in enumerate(split_urls_evenly(config.urls, config.workers), start=1):
            process = multiprocessing.Process(
                target=worker_main_urls,
                args=(worker_index, worker_urls, config),
                name=f"worker_{worker_index}",
            )
            process.start()
            processes.append(process)

    exit_codes: dict[str, int | None] = {}
    for process in processes:
        process.join()
        exit_codes[process.name] = process.exitcode

    finished_at = datetime.now()
    summary = merge_run_outputs(
        config=config,
        run_paths=run_paths,
        worker_paths=worker_paths,
        started_at=started_at,
        finished_at=finished_at,
        exit_codes=exit_codes,
    )

    if not config.keep_workdirs:
        cleanup_worker_roots(worker_paths)

    print(f"run_root          : {run_paths.root}")
    print(f"merged_exports    : {run_paths.merged_exports_dir}")
    print(f"merged_logs       : {run_paths.merged_logs_dir}")
    print(f"final_output_dir  : {run_paths.final_output_dir}")
    print(f"total_urls        : {summary['total_urls']}")
    print(f"success_count     : {summary['success_count']}")
    print(f"failure_count     : {summary['failure_count']}")
    print(f"unprocessed_count : {summary['unprocessed_count']}")
    if queue_creation_failed:
        print("dispatch_mode     : static-shards")
    else:
        print("dispatch_mode     : shared-queue")

    has_worker_failure = any((code or 0) != 0 for code in exit_codes.values())
    has_unprocessed = summary["unprocessed_count"] > 0
    return 1 if has_worker_failure or has_unprocessed else 0


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = parse_args(argv)
    config = build_run_config(args)
    return run_pipeline(config)


if __name__ == "__main__":
    raise SystemExit(main())
