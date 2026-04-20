from __future__ import annotations

import base64
import contextlib
import io
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

import zstandard as zstd

ZSTD_MAGIC = b"\x28\xB5\x2F\xFD"


def normalize_compressed_log_path(path: Path) -> Path:
    if path.suffix in {".zst", ".zstd"}:
        return path
    return path.with_suffix(f"{path.suffix}.zst")


def is_compressed_serial_log(path: Path) -> bool:
    with path.open("rb") as raw_file:
        header = _peek_header(raw_file, 4)
    return path.suffix in {".zst", ".zstd"} or header.startswith(ZSTD_MAGIC)


def encode_serial_record(delta_ms: int, payload: bytes) -> str:
    record = {
        "t": delta_ms,
        "d": base64.b64encode(payload).decode("ascii"),
    }
    return json.dumps(record, separators=(",", ":"))


def decode_serial_record(line: str) -> tuple[float, bytes]:
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("Log record must be a JSON object.")

    delay = float(record["t"])
    payload = record["d"]
    if not isinstance(payload, str):
        raise ValueError("Log payload must be base64 text.")

    return delay, base64.b64decode(payload)


class SerialLogWriter:
    def __init__(self, path: Path, flush_interval_s: float = 0.25) -> None:
        self.path = normalize_compressed_log_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_interval_s = flush_interval_s
        self._lock = threading.Lock()
        self._dirty = False
        self._closed = False
        self._last_flush_at = time.monotonic()

        compressor = zstd.ZstdCompressor(level=5, write_content_size=False)
        self._raw_file = self.path.open("ab")
        self._compression_stream = compressor.stream_writer(self._raw_file)
        self._text_file = io.TextIOWrapper(self._compression_stream, encoding="utf-8")

    def write_record(self, delta_ms: int, payload: bytes) -> None:
        with self._lock:
            if self._closed:
                raise ValueError("I/O operation on closed log writer.")

            self._text_file.write(encode_serial_record(delta_ms, payload))
            self._text_file.write("\n")
            self._dirty = True

            if delta_ms == 0:
                self._flush_unlocked()

    def flush_if_due(self, now_monotonic: float | None = None) -> None:
        with self._lock:
            if self._closed or not self._dirty:
                return

            now = time.monotonic() if now_monotonic is None else now_monotonic
            if now - self._last_flush_at < self._flush_interval_s:
                return

            self._flush_unlocked(now)

    def _flush_unlocked(self, now_monotonic: float | None = None) -> None:
        self._text_file.flush()
        self._dirty = False
        self._last_flush_at = time.monotonic() if now_monotonic is None else now_monotonic

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return

            with contextlib.suppress(Exception):
                if self._dirty:
                    self._flush_unlocked()
            with contextlib.suppress(Exception):
                self._text_file.close()
            with contextlib.suppress(Exception):
                self._raw_file.close()

            self._closed = True


class FollowableSerialLogReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._compressed = is_compressed_serial_log(path)
        self._line_count = 0
        self._last_size = path.stat().st_size
        self._reader_manager: contextlib.AbstractContextManager[TextIO] | None = None
        self._text_stream: TextIO | None = None

    def __enter__(self) -> FollowableSerialLogReader:
        self._open_reader()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> None:
        self.close()

    def readline(self) -> str:
        text_stream = self._require_text_stream()
        line = text_stream.readline()
        if line:
            self._line_count += 1
            return line

        return ""

    def refresh_if_grown(self) -> bool:
        if not self._compressed:
            return False

        current_size = self.path.stat().st_size
        if current_size <= self._last_size:
            return False

        self._reopen_and_skip()
        self._last_size = current_size
        return True

    def close(self) -> None:
        if self._reader_manager is None:
            return

        self._reader_manager.__exit__(None, None, None)
        self._reader_manager = None
        self._text_stream = None

    def _open_reader(self) -> None:
        self._reader_manager = open_serial_log_reader(self.path)
        self._text_stream = self._reader_manager.__enter__()

    def _reopen_and_skip(self) -> None:
        self.close()
        self._open_reader()
        text_stream = self._require_text_stream()
        skipped = 0
        while skipped < self._line_count:
            line = text_stream.readline()
            if not line:
                break
            skipped += 1

    def _require_text_stream(self) -> TextIO:
        if self._text_stream is None:
            raise RuntimeError("Log reader is not open.")
        return self._text_stream


def _peek_header(raw_file: io.BufferedReader, size: int) -> bytes:
    try:
        return raw_file.peek(size)[:size]
    except AttributeError:
        position = raw_file.tell()
        header = raw_file.read(size)
        raw_file.seek(position)
        return header


@contextlib.contextmanager
def open_serial_log_reader(path: Path) -> Iterator[TextIO]:
    raw_file = path.open("rb")
    text_stream: TextIO | None = None
    try:
        header = _peek_header(raw_file, 4)
        is_compressed = path.suffix in {".zst", ".zstd"} or header.startswith(ZSTD_MAGIC)

        binary_stream: Any = raw_file
        if is_compressed:
            binary_stream = zstd.ZstdDecompressor().stream_reader(raw_file)

        text_stream = io.TextIOWrapper(binary_stream, encoding="utf-8", errors="ignore")
        yield text_stream
    finally:
        if text_stream is not None:
            text_stream.close()
        else:
            raw_file.close()
