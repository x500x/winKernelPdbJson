from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable


def derive_module_name(input_path: Path) -> str:
    name = input_path.name
    for suffix in (".url.dedup.txt", ".url.txt", ".dedup.txt", ".txt"):
        if name.endswith(suffix):
            module_name = name[: -len(suffix)]
            if module_name:
                return module_name
    raise ValueError(f"unsupported input file name: {input_path.name}")


def infer_module_name_from_failed_list(failed_list_path: Path) -> str:
    if failed_list_path.name != "failed_urls.txt":
        raise ValueError(f"unsupported failed list file name: {failed_list_path.name}")
    if len(failed_list_path.parents) < 4:
        raise ValueError(f"cannot infer module name from failed list path: {failed_list_path}")
    return failed_list_path.parents[3].name


def derive_default_final_output_root(input_path: Path) -> Path:
    parent = input_path.parent
    if parent.name.lower() == "dedup" and parent.parent.name.lower() == "url":
        return parent.parent.parent.resolve()
    if parent.name.lower() == "url":
        return parent.parent.resolve()
    return parent.resolve()


def read_urls(input_path: Path) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for line in input_path.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def split_urls_evenly(urls: list[str], worker_count: int) -> list[list[str]]:
    if worker_count <= 0:
        raise ValueError("worker_count must be greater than 0")

    buckets: list[list[str]] = [[] for _ in range(worker_count)]
    for index, url in enumerate(urls):
        buckets[index % worker_count].append(url)
    return buckets


def build_run_id(now: datetime | None = None) -> str:
    current = now or datetime.now()
    return current.strftime("%Y%m%d-%H%M%S")


def build_worker_dir(run_root: Path, worker_index: int) -> Path:
    return run_root / f"worker_{worker_index}"


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_jsonl(path: Path, record: dict) -> None:
    ensure_directory(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False))
        handle.write("\n")


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    ensure_directory(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False))
            handle.write("\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def write_text_lines(path: Path, lines: Iterable[str]) -> None:
    ensure_directory(path.parent)
    text = "\n".join(lines)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def clear_directory(path: Path) -> None:
    if not path.exists():
        return

    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def iso_timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def trim_output(text: str, max_length: int = 2000) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."
