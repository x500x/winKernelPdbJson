#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import sys
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


DEFAULT_SYMBOL_SERVER = "https://msdl.microsoft.com/download/symbols"
MAX_PATH = 260


class SymbolError(RuntimeError):
    """Raised when symbol index parsing or download fails."""


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SYMSRV_INDEX_INFOW(ctypes.Structure):
    _fields_ = [
        ("sizeofstruct", wintypes.DWORD),
        ("file", wintypes.WCHAR * (MAX_PATH + 1)),
        ("stripped", wintypes.BOOL),
        ("timestamp", wintypes.DWORD),
        ("size", wintypes.DWORD),
        ("dbgfile", wintypes.WCHAR * (MAX_PATH + 1)),
        ("pdbfile", wintypes.WCHAR * (MAX_PATH + 1)),
        ("guid", GUID),
        ("sig", wintypes.DWORD),
        ("age", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class SymbolIndexInfo:
    image_path: Path
    pdb_name: str
    guid: str
    age: int

    @property
    def symbol_store_key(self) -> str:
        return build_symbol_store_key(self.guid, self.age)

    def build_download_url(self, symbol_server: str) -> str:
        base = symbol_server.rstrip("/")
        return f"{base}/{self.pdb_name}/{self.symbol_store_key}/{self.pdb_name}"


def format_winerror(code: int | None) -> str:
    if not code:
        return "unknown error"
    return ctypes.FormatError(code).strip()


def guid_to_canonical_text(guid: GUID) -> str:
    tail = "".join(f"{byte:02X}" for byte in guid.Data4)
    return (
        f"{guid.Data1:08X}-{guid.Data2:04X}-{guid.Data3:04X}-"
        f"{tail[:4]}-{tail[4:]}"
    )


def guid_to_compact_text(guid: GUID) -> str:
    return guid_to_canonical_text(guid).replace("-", "")


def build_symbol_store_key(guid_text: str, age: int) -> str:
    compact_guid = guid_text.replace("-", "").upper()
    return f"{compact_guid}{age:X}"


def build_download_url(symbol_server: str, pdb_name: str, symbol_store_key: str) -> str:
    return f"{symbol_server.rstrip('/')}/{pdb_name}/{symbol_store_key}/{pdb_name}"


def load_dbghelp() -> ctypes.WinDLL:
    try:
        dbghelp = ctypes.WinDLL("dbghelp.dll", use_last_error=True)
    except OSError as exc:
        raise SymbolError(f"failed to load dbghelp.dll: {exc}") from exc

    if not hasattr(dbghelp, "SymSrvGetFileIndexInfoW"):
        raise SymbolError("dbghelp.dll does not export SymSrvGetFileIndexInfoW")

    dbghelp.SymSrvGetFileIndexInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(SYMSRV_INDEX_INFOW),
        wintypes.DWORD,
    ]
    dbghelp.SymSrvGetFileIndexInfoW.restype = wintypes.BOOL
    return dbghelp


def read_index_info(image_path: Path) -> SymbolIndexInfo:
    image_path = image_path.expanduser().resolve()
    if not image_path.is_file():
        raise SymbolError(f"input file not found: {image_path}")

    dbghelp = load_dbghelp()
    info = SYMSRV_INDEX_INFOW()
    info.sizeofstruct = ctypes.sizeof(info)

    ok = dbghelp.SymSrvGetFileIndexInfoW(str(image_path), ctypes.byref(info), 0)
    if not ok:
        error_code = ctypes.get_last_error()
        error_text = format_winerror(error_code)
        raise SymbolError(
            f"SymSrvGetFileIndexInfoW failed for {image_path} "
            f"(WinError {error_code}: {error_text})"
        )

    pdb_name = PureWindowsPath(info.pdbfile).name
    if not pdb_name:
        raise SymbolError(f"no pdb file name was found in {image_path}")

    return SymbolIndexInfo(
        image_path=image_path,
        pdb_name=pdb_name,
        guid=guid_to_canonical_text(info.guid),
        age=int(info.age),
    )


def download_pdb(
    index_info: SymbolIndexInfo,
    output_dir: Path,
    symbol_server: str,
    overwrite: bool,
    timeout: int,
) -> Path:
    output_root = output_dir.expanduser().resolve()
    destination = (
        output_root
        / index_info.pdb_name
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and not overwrite:
        return destination

    url = index_info.build_download_url(symbol_server)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "fetch-ms-symbol/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise SymbolError(
            f"HTTP {exc.code} while downloading symbol from {url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SymbolError(f"network error while downloading symbol: {exc}") from exc

    destination.write_bytes(data)
    return destination


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract PDB GUID/Age from a PE image and download the symbol.",
    )
    parser.add_argument("image", help="Path to a PE image such as hal.dll")
    parser.add_argument(
        "--symbol-server",
        default=DEFAULT_SYMBOL_SERVER,
        help="Base URL of the symbol server",
    )
    parser.add_argument(
        "--output-dir",
        default="downloaded_symbols",
        help="Directory used to store downloaded symbols",
    )
    parser.add_argument(
        "--timeout",
        default=60,
        type=int,
        help="Network timeout in seconds",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing downloaded symbol",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Only print the parsed PDB metadata",
    )
    return parser


def print_index_info(index_info: SymbolIndexInfo, symbol_server: str) -> None:
    print(f"image_path      : {index_info.image_path}")
    print(f"pdb_name        : {index_info.pdb_name}")
    print(f"guid            : {index_info.guid}")
    print(f"guid_compact    : {index_info.guid.replace('-', '')}")
    print(f"age             : {index_info.age}")
    print(f"symbol_store_key: {index_info.symbol_store_key}")
    print(f"download_url    : {index_info.build_download_url(symbol_server)}")


def main(argv: list[str] | None = None) -> int:
    parser = create_argument_parser()
    args = parser.parse_args(argv)

    try:
        index_info = read_index_info(Path(args.image))
        print_index_info(index_info, args.symbol_server)

        if args.no_download:
            return 0

        saved_path = download_pdb(
            index_info=index_info,
            output_dir=Path(args.output_dir),
            symbol_server=args.symbol_server,
            overwrite=args.overwrite,
            timeout=args.timeout,
        )
        print(f"saved_path      : {saved_path}")
        print(f"saved_size      : {saved_path.stat().st_size}")
        return 0
    except SymbolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

