#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from github_contents import GitHubContentsClient, GitHubContentsError
from winbindex_urls import build_url_sets


@dataclass(frozen=True)
class ModuleRemoteState:
    raw_urls: list[str]
    dedup_urls: list[str]
    failed_urls: list[str]


@dataclass(frozen=True)
class PipelineResult:
    returncode: int
    run_root: Path | None
    exported_files: list[Path]
    success_urls: list[str]
    failed_urls: list[str]
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ModuleSyncSummary:
    module_name: str
    new_url_count: int
    retry_url_count: int
    success_count: int
    failure_count: int
    uploaded_json_count: int
    skipped_json_count: int
    raw_updated: bool
    dedup_updated: bool
    missing_updated: bool


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    script_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Sync Winbindex URLs, export new PDB JSON files, and upload changes with gh api.",
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name format.")
    parser.add_argument("--branch", default="main", help="Git branch to update.")
    parser.add_argument(
        "--modules-file",
        type=Path,
        default=script_root / "modules.json",
        help="JSON file that contains the module list.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=script_root / ".sync-work",
        help="Working directory used for temporary inputs, outputs, and logs.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Worker count passed to tools/main.py.",
    )
    parser.add_argument(
        "--gh-bin",
        default="gh",
        help="gh executable name or path.",
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="Keep intermediate outputs for inspection.",
    )
    return parser.parse_args(argv)


def load_modules(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    modules = payload.get("modules", [])
    if not isinstance(modules, list) or not modules:
        raise ValueError(f"invalid modules file: {path}")

    normalized: list[str] = []
    seen: set[str] = set()
    for value in modules:
        module_name = str(value).strip()
        if not module_name or module_name in seen:
            continue
        seen.add(module_name)
        normalized.append(module_name)

    if not normalized:
        raise ValueError(f"modules file contains no usable module names: {path}")
    return normalized


def parse_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def format_lines(lines: list[str]) -> str:
    text = "\n".join(lines)
    if text:
        text += "\n"
    return text


def ordered_difference(source: list[str], baseline: list[str]) -> list[str]:
    baseline_set = set(baseline)
    return [item for item in source if item not in baseline_set]


def build_processing_urls(
    latest_dedup: list[str],
    current_dedup: list[str],
    current_failed: list[str],
) -> tuple[list[str], list[str], list[str]]:
    new_urls = ordered_difference(latest_dedup, current_dedup)
    new_set = set(new_urls)
    latest_set = set(latest_dedup)
    retry_urls = [url for url in current_failed if url in latest_set and url not in new_set]
    return new_urls, retry_urls, [*new_urls, *retry_urls]


def build_next_failed_urls(
    latest_dedup: list[str],
    previous_failed: list[str],
    current_failed: list[str],
    successful_urls: list[str],
) -> list[str]:
    latest_set = set(latest_dedup)
    failed_set = {
        url
        for url in previous_failed
        if url in latest_set
    }
    failed_set.update(current_failed)
    failed_set.difference_update(successful_urls)

    ordered = [url for url in latest_dedup if url in failed_set]
    for url in current_failed:
        if url in failed_set and url not in ordered:
            ordered.append(url)
    return ordered


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def raw_url_path(module_name: str) -> str:
    return f"url/raw/{module_name}.url.txt"


def dedup_url_path(module_name: str) -> str:
    return f"url/dedup/{module_name}.url.dedup.txt"


def failed_url_path(module_name: str) -> str:
    return f"url/404/{module_name}.txt"


def fetch_remote_state(client: GitHubContentsClient, module_name: str) -> ModuleRemoteState:
    return ModuleRemoteState(
        raw_urls=parse_lines(client.read_text(raw_url_path(module_name), default="")),
        dedup_urls=parse_lines(client.read_text(dedup_url_path(module_name), default="")),
        failed_urls=parse_lines(client.read_text(failed_url_path(module_name), default="")),
    )


def remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_lines(lines), encoding="utf-8")


def parse_run_root(stdout: str) -> Path | None:
    for line in stdout.splitlines():
        if line.startswith("run_root"):
            _, _, value = line.partition(":")
            candidate = value.strip()
            if candidate:
                return Path(candidate)
    return None


def find_latest_run_root(run_parent: Path) -> Path | None:
    if not run_parent.is_dir():
        return None
    candidates = [path for path in run_parent.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return sorted(candidates)[-1]


def run_pipeline_for_module(
    script_root: Path,
    work_dir: Path,
    module_name: str,
    urls: list[str],
    workers: int,
) -> PipelineResult:
    if not urls:
        return PipelineResult(
            returncode=0,
            run_root=None,
            exported_files=[],
            success_urls=[],
            failed_urls=[],
            stdout="",
            stderr="",
        )

    inputs_dir = work_dir / "inputs"
    outputs_root = work_dir / "outputs"
    runs_root = work_dir / "runs"
    module_output_dir = outputs_root / module_name
    module_run_root = runs_root / module_name

    remove_path(module_output_dir)
    remove_path(module_run_root)
    inputs_dir.mkdir(parents=True, exist_ok=True)

    input_path = inputs_dir / f"{module_name}.url.txt"
    write_lines(input_path, urls)

    command = [
        sys.executable,
        str(script_root / "main.py"),
        "--input",
        str(input_path),
        "--workers",
        str(workers),
        "--final-output-root",
        str(outputs_root),
        "--run-root",
        str(runs_root),
        "--overwrite",
    ]
    result = subprocess.run(command, capture_output=True, text=True)

    run_root = parse_run_root(result.stdout) or find_latest_run_root(module_run_root)
    success_urls: list[str] = []
    failed_urls: list[str] = []
    exported_files = sorted(module_output_dir.glob("*.json")) if module_output_dir.is_dir() else []

    if run_root is not None:
        logs_dir = run_root / "merged" / "logs"
        success_records = read_jsonl(logs_dir / "all_success.jsonl")
        success_urls = [record["url"] for record in success_records if record.get("url")]
        failed_urls = parse_lines((logs_dir / "failed_urls.txt").read_text(encoding="utf-8")) if (logs_dir / "failed_urls.txt").is_file() else []

    if result.returncode != 0 and not failed_urls:
        failed_urls = list(urls)

    return PipelineResult(
        returncode=result.returncode,
        run_root=run_root,
        exported_files=exported_files,
        success_urls=success_urls,
        failed_urls=failed_urls,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def upload_json_exports(
    client: GitHubContentsClient,
    module_name: str,
    exported_files: list[Path],
) -> tuple[int, int]:
    uploaded = 0
    skipped = 0
    for export_file in exported_files:
        remote_path = f"{module_name}/{export_file.name}"
        if client.get_file_metadata(remote_path) is not None:
            skipped += 1
            continue
        client.upload_bytes(
            remote_path,
            export_file.read_bytes(),
            f"feat({module_name}): add {export_file.name}",
        )
        uploaded += 1
    return uploaded, skipped


def sync_text_snapshot(
    client: GitHubContentsClient,
    path: str,
    current_lines: list[str],
    next_lines: list[str],
    update_message: str,
    delete_message: str,
) -> bool:
    if current_lines == next_lines:
        return False
    if next_lines:
        client.upload_text(path, format_lines(next_lines), update_message)
        return True
    return client.delete_file(path, delete_message)


def sync_module(
    client: GitHubContentsClient,
    script_root: Path,
    work_dir: Path,
    module_name: str,
    workers: int,
) -> ModuleSyncSummary:
    latest_raw_urls, latest_dedup_urls = build_url_sets(module_name)
    remote_state = fetch_remote_state(client, module_name)
    new_urls, retry_urls, processing_urls = build_processing_urls(
        latest_dedup_urls,
        remote_state.dedup_urls,
        remote_state.failed_urls,
    )

    pipeline_result = run_pipeline_for_module(
        script_root=script_root,
        work_dir=work_dir,
        module_name=module_name,
        urls=processing_urls,
        workers=workers,
    )

    next_failed_urls = build_next_failed_urls(
        latest_dedup=latest_dedup_urls,
        previous_failed=remote_state.failed_urls,
        current_failed=pipeline_result.failed_urls,
        successful_urls=pipeline_result.success_urls,
    )

    uploaded_json_count, skipped_json_count = upload_json_exports(
        client=client,
        module_name=module_name,
        exported_files=pipeline_result.exported_files,
    )

    raw_updated = sync_text_snapshot(
        client,
        raw_url_path(module_name),
        remote_state.raw_urls,
        latest_raw_urls,
        f"chore(url): update raw URLs for {module_name}",
        f"chore(url): remove raw URLs for {module_name}",
    )
    dedup_updated = sync_text_snapshot(
        client,
        dedup_url_path(module_name),
        remote_state.dedup_urls,
        latest_dedup_urls,
        f"chore(url): update dedup URLs for {module_name}",
        f"chore(url): remove dedup URLs for {module_name}",
    )
    missing_updated = sync_text_snapshot(
        client,
        failed_url_path(module_name),
        remote_state.failed_urls,
        next_failed_urls,
        f"chore(url): update missing URLs for {module_name}",
        f"chore(url): remove missing URLs for {module_name}",
    )

    return ModuleSyncSummary(
        module_name=module_name,
        new_url_count=len(new_urls),
        retry_url_count=len(retry_urls),
        success_count=len(pipeline_result.success_urls),
        failure_count=len(next_failed_urls),
        uploaded_json_count=uploaded_json_count,
        skipped_json_count=skipped_json_count,
        raw_updated=raw_updated,
        dedup_updated=dedup_updated,
        missing_updated=missing_updated,
    )


def write_summary(path: Path, summaries: list[ModuleSyncSummary]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(summary) for summary in summaries]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def print_summary(summary: ModuleSyncSummary) -> None:
    print(
        "[{name}] new={new} retry={retry} success={success} failed={failed} "
        "json_uploaded={uploaded} json_skipped={skipped} raw_changed={raw} "
        "dedup_changed={dedup} missing_changed={missing}".format(
            name=summary.module_name,
            new=summary.new_url_count,
            retry=summary.retry_url_count,
            success=summary.success_count,
            failed=summary.failure_count,
            uploaded=summary.uploaded_json_count,
            skipped=summary.skipped_json_count,
            raw=summary.raw_updated,
            dedup=summary.dedup_updated,
            missing=summary.missing_updated,
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    script_root = Path(__file__).resolve().parent
    work_dir = args.work_dir.resolve()

    if args.workers <= 0:
        raise ValueError("--workers must be greater than 0")

    modules = load_modules(args.modules_file.resolve())
    client = GitHubContentsClient(repo=args.repo, branch=args.branch, gh_bin=args.gh_bin)
    client.ensure_gh_available()

    if not args.keep_work_dir:
        remove_path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[ModuleSyncSummary] = []
    try:
        for module_name in modules:
            summary = sync_module(
                client=client,
                script_root=script_root,
                work_dir=work_dir,
                module_name=module_name,
                workers=args.workers,
            )
            summaries.append(summary)
            print_summary(summary)
    except GitHubContentsError as exc:
        print(f"gh api error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 1
    finally:
        write_summary(work_dir / "summary.json", summaries)
        if not args.keep_work_dir:
            remove_path(work_dir / "inputs")
            remove_path(work_dir / "outputs")
            remove_path(work_dir / "runs")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
