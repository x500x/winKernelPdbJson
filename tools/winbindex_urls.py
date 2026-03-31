#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import urllib.request
from pathlib import Path


PAGE_SIZE = 0x1000
CATALOG_URL_TEMPLATE = "https://winbindex.m417z.com/data/by_filename_compressed/{module}.json.gz"
USER_AGENT = "pdb-sync/1.0"


def make_symbol_url(name: str, timestamp: int, image_size: int) -> str:
    file_id = f"{timestamp:08X}{image_size:x}"
    return f"https://msdl.microsoft.com/download/symbols/{name}/{file_id}/{name}"


def align_up(value: int, alignment: int = PAGE_SIZE) -> int:
    mask = alignment - 1
    return value if (value & mask) == 0 else (value + alignment) & ~mask


def load_catalog_bytes(path: Path) -> bytes:
    return path.read_bytes()


def parse_catalog_bytes(data: bytes) -> dict:
    return json.loads(gzip.decompress(data).decode("utf-8"))


def load_catalog(path: Path) -> dict:
    source = load_catalog_bytes(path)
    if path.suffix != ".gz":
        return json.loads(source.decode("utf-8"))
    return parse_catalog_bytes(source)


def fetch_catalog(module_name: str, timeout: int = 60) -> dict:
    url = CATALOG_URL_TEMPLATE.format(module=module_name)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_catalog_bytes(response.read())


def resolve_download_name(module_name: str, entry: dict) -> str:
    name = module_name
    for updates in entry.get("windowsVersions", {}).values():
        for kb_name, kb_info in updates.items():
            if kb_name == "BASE":
                continue
            for assembly in kb_info.get("assemblies", {}).values():
                for attr in assembly.get("attributes", []):
                    if attr.get("name"):
                        name = attr["name"]
                        break
    return name


def build_raw_urls(module_name: str, data: dict) -> list[str]:
    urls: list[str] = []
    for entry in data.values():
        file_info = entry.get("fileInfo", {})
        timestamp = file_info.get("timestamp")
        if not timestamp:
            continue

        download_name = resolve_download_name(module_name, entry)
        virtual_size = file_info.get("virtualSize")
        if virtual_size:
            urls.append(make_symbol_url(download_name, timestamp, virtual_size))
            continue

        size = file_info.get("size")
        last_va = file_info.get("lastSectionVirtualAddress")
        last_ptr = file_info.get("lastSectionPointerToRawData")
        if not all([size, last_va, last_ptr]):
            continue

        last_section_size = size - last_ptr
        max_size = align_up(last_va + last_section_size)
        min_size = last_va + PAGE_SIZE

        candidate_size = max_size
        while candidate_size >= min_size:
            urls.append(make_symbol_url(download_name, timestamp, candidate_size))
            candidate_size -= PAGE_SIZE
    return urls


def deduplicate_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def build_url_sets(
    module_name: str,
    *,
    catalog_path: Path | None = None,
    timeout: int = 60,
) -> tuple[list[str], list[str]]:
    data = load_catalog(catalog_path) if catalog_path else fetch_catalog(module_name, timeout=timeout)
    raw_urls = build_raw_urls(module_name, data)
    dedup_urls = deduplicate_urls(raw_urls)
    return raw_urls, dedup_urls


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download or load a Winbindex catalog and generate raw/dedup symbol URLs.",
    )
    parser.add_argument("module_name", help="Module name such as ci.dll")
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Existing .json or .json.gz catalog path. When omitted the catalog is downloaded.",
    )
    parser.add_argument("--raw-output", type=Path, help="Write raw URLs to this file")
    parser.add_argument("--dedup-output", type=Path, help="Write deduplicated URLs to this file")
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Network timeout in seconds when downloading the catalog.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_urls, dedup_urls = build_url_sets(
        args.module_name,
        catalog_path=args.catalog,
        timeout=args.timeout,
    )

    if args.raw_output:
        write_lines(args.raw_output, raw_urls)
    if args.dedup_output:
        write_lines(args.dedup_output, dedup_urls)

    if not args.raw_output and not args.dedup_output:
        for url in raw_urls:
            print(url)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
