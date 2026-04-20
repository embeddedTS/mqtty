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
