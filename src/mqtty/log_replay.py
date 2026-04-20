from __future__ import annotations

import argparse
import os
import sys
import termios
import time
from pathlib import Path
from typing import Any

from mqtty.log_io import decode_serial_record, open_serial_log_reader

ESCAPE_SEQ = {
    b"\x1b[C": "RIGHT",
    b"\x1b[D": "LEFT",
    b"\x1b[A": "UP",
    b"\x1b[B": "DOWN",
}


class RawTerminal:
    def __init__(self) -> None:
        self.fd: int | None = None
        self.original_mode: list[Any] | None = None

    def __enter__(self) -> RawTerminal:
        self.fd = sys.stdin.fileno()
        self.original_mode = termios.tcgetattr(self.fd)
        tty_mode = termios.tcgetattr(self.fd)
        tty_mode[3] &= ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(self.fd, termios.TCSADRAIN, tty_mode)
        os.set_blocking(self.fd, False)
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> None:
        if self.fd is not None and self.original_mode is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original_mode)
            os.set_blocking(self.fd, True)


class Replayer:
    def __init__(self, log_path: Path, raw: bool = False, follow: bool = False) -> None:
        self.log_path = log_path
        self.raw = raw
        self.follow = follow
        self.speed_factor = 1.0
        self.instant = False
        self.pending_remaining = 0.0

    def info(self) -> None:
        byte_size = self.log_path.stat().st_size
        line_count = 0
        total_ms = 0.0

        with open_serial_log_reader(self.log_path) as log_file:
            for line in log_file:
                line_count += 1
                try:
                    delay_ms, _payload = decode_serial_record(line)
                except Exception:
                    continue
                total_ms += delay_ms

        print(f"Lines            : {line_count}")
        print(f"File size (bytes): {byte_size}")
        print(f"Total duration   : {total_ms / 1000:.3f} s")

    def run(self) -> None:
        if self.raw:
            self._raw_dump()
        else:
            self._interactive()

    def _poll_key(self) -> str | None:
        try:
            data = os.read(sys.stdin.fileno(), 3)
            if not data:
                return None
        except BlockingIOError:
            return None

        if data.startswith(b"\x1b["):
            return ESCAPE_SEQ.get(data, "OTHER")
        if data in {b"\r", b"\n"}:
            return "ENTER"
        if data.lower() == b"i":
            return "INFO"
        return "OTHER"

    def _announce_speed(self) -> None:
        if self.instant:
            message = "Speed: instant\r\n"
        else:
            message = f"Speed: {self.speed_factor:.2f}x\r\n"
        sys.stdout.write(message)
        sys.stdout.flush()

    def _print_remaining(self) -> None:
        if self.instant:
            message = "Delay remaining: instant\r\n"
        else:
            delay_ms = max(self.pending_remaining, 0.0) * 1000
            message = f"Delay remaining: {delay_ms:.0f} ms\r\n"
        sys.stdout.write(message)
        sys.stdout.flush()

    def _set_speed(self, new_factor: float) -> None:
        self.speed_factor = max(0.01, min(100.0, new_factor))
        self.instant = False
        self._announce_speed()

    def _toggle_instant(self) -> None:
        self.instant = not self.instant
        if not self.instant:
            self.speed_factor = 1.0
        self._announce_speed()

    def _handle_pending_keys(self) -> bool:
        key = self._poll_key()
        if key == "RIGHT":
            self._set_speed(self.speed_factor * 2)
        elif key == "LEFT":
            self._set_speed(self.speed_factor / 2)
        elif key == "UP":
            self._set_speed(1.0)
        elif key == "DOWN":
            self._toggle_instant()
        elif key == "ENTER":
            return True
        elif key == "INFO":
            self._print_remaining()
        return False

    def _raw_dump(self) -> None:
        start = time.perf_counter()
        with open_serial_log_reader(self.log_path) as log_file:
            while True:
                line = log_file.readline()
                if not line:
                    if self.follow:
                        time.sleep(0.1)
                        continue
                    break

                try:
                    _delay_ms, payload = decode_serial_record(line)
                except Exception:
                    continue

                sys.stdout.buffer.write(payload)

        sys.stdout.flush()
        elapsed = time.perf_counter() - start
        print(f"\n===== Replay complete: {elapsed:.3f} s wall-time =====")

    def _interactive(self) -> None:
        print("===== Press Left/Right to slow/fast, Up reset, Down instant, Enter skip, i info =====\n")
        self._announce_speed()
        start = time.perf_counter()

        with RawTerminal(), open_serial_log_reader(self.log_path) as log_file:
            next_wall = time.perf_counter()
            while True:
                line = log_file.readline()
                if not line:
                    self._handle_pending_keys()
                    if self.follow:
                        time.sleep(0.05)
                        continue
                    break

                try:
                    delay_ms, payload = decode_serial_record(line)
                except Exception:
                    continue

                delay_seconds = 0.0 if self.instant else (delay_ms / 1000.0) / self.speed_factor
                next_wall += delay_seconds

                while True:
                    remaining = next_wall - time.perf_counter()
                    self.pending_remaining = remaining
                    if remaining <= 0:
                        break
                    if self._handle_pending_keys():
                        next_wall = time.perf_counter()
                        break
                    time.sleep(min(0.02, remaining))

                sys.stdout.buffer.write(payload)
                sys.stdout.flush()

        elapsed = time.perf_counter() - start
        print(f"\n===== Replay complete: {elapsed:.3f} s wall-time =====")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay an NDJSON serial log (plain or .zst) or inspect it.",
    )
    parser.add_argument("log", type=Path, help="Path to replay.jsonl or replay.jsonl.zst")
    parser.add_argument(
        "-r",
        "--raw",
        action="store_true",
        help="Dump payloads instantly and ignore timing controls",
    )
    parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Keep reading new records as the log grows",
    )
    parser.add_argument(
        "-i",
        "--info",
        action="store_true",
        help="Show log info and exit",
    )
    args = parser.parse_args()

    try:
        replayer = Replayer(args.log, raw=args.raw, follow=args.follow)
        if args.info:
            replayer.info()
        else:
            replayer.run()
    except RuntimeError as error:
        sys.stderr.write(f"Error: {error}\n")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
