from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GitHubContentsError(RuntimeError):
    """Raised when a gh api contents request fails."""


@dataclass(frozen=True)
class RemoteFileMetadata:
    path: str
    sha: str
    size: int
    file_type: str


class GitHubContentsClient:
    def __init__(self, repo: str, branch: str = "main", gh_bin: str = "gh") -> None:
        self.repo = repo
        self.branch = branch
        self.gh_bin = gh_bin

    def ensure_gh_available(self) -> None:
        result = subprocess.run(
            [self.gh_bin, "--version"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "gh is unavailable"
            raise GitHubContentsError(message)

    def build_read_endpoint(self, path: str) -> str:
        return f"repos/{self.repo}/contents/{path}?ref={self.branch}"

    def build_write_endpoint(self, path: str) -> str:
        return f"repos/{self.repo}/contents/{path}"

    def _run_api(
        self,
        args: list[str],
        *,
        text: bool,
        input_path: Path | None = None,
        allow_not_found: bool = False,
    ) -> subprocess.CompletedProcess[Any]:
        command = [self.gh_bin, "api", *args]
        if input_path is not None:
            command.extend(["--input", str(input_path)])

        result = subprocess.run(
            command,
            capture_output=True,
            text=text,
        )
        if result.returncode == 0:
            return result

        stderr_text = self._coerce_text(result.stderr)
        stdout_text = self._coerce_text(result.stdout)
        if allow_not_found and self._is_not_found(stderr_text, stdout_text):
            return result

        detail = stderr_text or stdout_text or "gh api request failed"
        raise GitHubContentsError(detail)

    @staticmethod
    def _coerce_text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    @staticmethod
    def _is_not_found(stderr_text: str, stdout_text: str) -> bool:
        combined = f"{stderr_text}\n{stdout_text}"
        return "HTTP 404" in combined or "Not Found" in combined

    def get_file_metadata(self, path: str) -> RemoteFileMetadata | None:
        result = self._run_api(
            [self.build_read_endpoint(path)],
            text=True,
            allow_not_found=True,
        )
        if result.returncode != 0:
            return None

        payload = json.loads(result.stdout)
        return RemoteFileMetadata(
            path=payload["path"],
            sha=payload["sha"],
            size=int(payload.get("size", 0)),
            file_type=payload.get("type", "file"),
        )

    def read_text(self, path: str, default: str = "") -> str:
        result = self._run_api(
            [
                "-H",
                "Accept: application/vnd.github.raw+json",
                self.build_read_endpoint(path),
            ],
            text=False,
            allow_not_found=True,
        )
        if result.returncode != 0:
            return default
        return bytes(result.stdout).decode("utf-8")

    def upload_text(self, path: str, text: str, message: str) -> dict[str, Any]:
        return self.upload_bytes(path, text.encode("utf-8"), message)

    def upload_bytes(self, path: str, data: bytes, message: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self.branch,
        }

        metadata = self.get_file_metadata(path)
        if metadata is not None:
            payload["sha"] = metadata.sha

        payload_path = self._write_payload(payload)
        try:
            result = self._run_api(
                ["--method", "PUT", self.build_write_endpoint(path)],
                text=True,
                input_path=payload_path,
            )
        finally:
            payload_path.unlink(missing_ok=True)

        return json.loads(result.stdout)

    def delete_file(self, path: str, message: str) -> bool:
        metadata = self.get_file_metadata(path)
        if metadata is None:
            return False

        payload = {
            "message": message,
            "sha": metadata.sha,
            "branch": self.branch,
        }
        payload_path = self._write_payload(payload)
        try:
            self._run_api(
                ["--method", "DELETE", self.build_write_endpoint(path)],
                text=True,
                input_path=payload_path,
            )
        finally:
            payload_path.unlink(missing_ok=True)
        return True

    @staticmethod
    def _write_payload(payload: dict[str, Any]) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            return Path(handle.name)
