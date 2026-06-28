#!/usr/bin/env python3
"""stdio proxy for observing Zed ACP traffic to Grok Build.

Usage:
    uv run "C:\\path\\to\\zed-acp-coding-agent-proxy\\zed-acp-coding-agent-proxy.py"

The proxy preserves stdin/stdout as the ACP transport and writes diagnostic
copies of traffic and process events under ./logs.

Environment:
    GROK_BUILD_VERSION  npm package version, for example 0.2.72
    GROK_BUILD_MODEL    model ID, for example grok-build
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import msvcrt
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

MAX_PREVIEW_BYTES = 16_384
CHUNK_SIZE = 4096
DEFAULT_IDLE_TIMEOUT_SECONDS = 0
DEFAULT_VERSION = "0.2.72"
DEFAULT_MODEL = "grok-build"
ENV_VERSION = "GROK_BUILD_VERSION"
ENV_MODEL = "GROK_BUILD_MODEL"


def now_iso() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="milliseconds")
    )


def safe_version(version: str) -> str:
    if not re.fullmatch(r"[0-9A-Za-z_.+-]+", version):
        raise ValueError(f"unsafe version string: {version!r}")
    return version


def safe_model(model: str) -> str:
    if not re.fullmatch(r"[0-9A-Za-z_.:/+-]+", model):
        raise ValueError(f"unsafe model string: {model!r}")
    return model


def make_session_paths(base_dir: Path) -> tuple[Path, dict[str, Path]]:
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = logs_dir / f"session-{stamp}-{os.getpid()}"
    session_dir.mkdir()
    paths = {
        "events": session_dir / "events.log",
        "zed_to_grok": session_dir / "zed-to-grok.bin",
        "grok_to_zed": session_dir / "grok-to-zed.bin",
        "grok_stderr": session_dir / "grok-stderr.log",
        "frames": session_dir / "frames.log",
    }
    return session_dir, paths


class Logger:
    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths
        self.lock = threading.Lock()

    def event(self, message: str) -> None:
        line = f"{now_iso()} {message}\n"
        with self.lock:
            with self.paths["events"].open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()

    def stderr_line(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace")
        with self.lock:
            with self.paths["grok_stderr"].open("a", encoding="utf-8") as f:
                f.write(f"{now_iso()} {text}")
                if not text.endswith("\n"):
                    f.write("\n")
                f.flush()

    def raw(self, key: str, data: bytes) -> None:
        with self.lock:
            with self.paths[key].open("ab") as f:
                f.write(data)
                f.flush()

    def frame(self, direction: str, data: bytes) -> None:
        preview = data[:MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")
        truncated = (
            ""
            if len(data) <= MAX_PREVIEW_BYTES
            else f" ... <truncated {len(data) - MAX_PREVIEW_BYTES} bytes>"
        )
        with self.lock:
            with self.paths["frames"].open("a", encoding="utf-8") as f:
                f.write(f"{now_iso()} {direction} {len(data)} bytes\n")
                f.write(preview)
                f.write(truncated)
                if not preview.endswith("\n"):
                    f.write("\n")
                f.write("\n")
                f.flush()


def pipe_bytes(
    src: BinaryIO,
    dst: BinaryIO,
    raw_key: str,
    direction: str,
    logger: Logger,
    done: threading.Event,
    activity: queue.Queue[str],
) -> None:
    src_fd = src.fileno()
    dst_fd = dst.fileno()
    logger.event(f"{direction}: thread started src_fd={src_fd} dst_fd={dst_fd}")
    try:
        while not done.is_set():
            chunk = os.read(src_fd, CHUNK_SIZE)
            if not chunk:
                logger.event(f"{direction}: eof")
                return
            logger.raw(raw_key, chunk)
            logger.frame(direction, chunk)
            os.write(dst_fd, chunk)
            activity.put(direction)
    except BrokenPipeError:
        logger.event(f"{direction}: broken pipe")
    except OSError as exc:
        logger.event(f"{direction}: os error {exc!r}")
    except Exception as exc:
        logger.event(f"{direction}: exception {exc!r}")
    finally:
        done.set()


def pipe_stderr(src: BinaryIO, logger: Logger, done: threading.Event) -> None:
    try:
        while not done.is_set():
            line = src.readline()
            if not line:
                logger.event("grok stderr: eof")
                return
            logger.stderr_line(line)
    except Exception as exc:
        logger.event(f"grok stderr: exception {exc!r}")


def should_filter_grok_message(line: bytes, logger: Logger) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    try:
        message = json.loads(stripped.decode("utf-8"))
    except Exception:
        return False
    if not isinstance(message, dict):
        return False

    method = message.get("method")
    msg_id = message.get("id")
    if isinstance(method, str) and method.startswith("_x.ai/") and msg_id is None:
        logger.frame("grok->zed filtered", line)
        return True
    if msg_id == "skills-reload" and "result" in message:
        logger.frame("grok->zed filtered", line)
        return True
    return False


def pipe_grok_to_zed_filtered(
    src: BinaryIO,
    dst: BinaryIO,
    logger: Logger,
    done: threading.Event,
    activity: queue.Queue[str],
) -> None:
    src_fd = src.fileno()
    dst_fd = dst.fileno()
    direction = "grok->zed"
    buffer = bytearray()
    logger.event(
        f"{direction}: filtered thread started src_fd={src_fd} dst_fd={dst_fd}"
    )
    try:
        while not done.is_set():
            chunk = os.read(src_fd, CHUNK_SIZE)
            if not chunk:
                if buffer:
                    data = bytes(buffer)
                    logger.raw("grok_to_zed", data)
                    if not should_filter_grok_message(data, logger):
                        logger.frame(direction, data)
                        os.write(dst_fd, data)
                logger.event(f"{direction}: eof")
                return

            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line = bytes(buffer[: newline + 1])
                del buffer[: newline + 1]
                logger.raw("grok_to_zed", line)
                if should_filter_grok_message(line, logger):
                    activity.put(f"{direction} filtered")
                    continue
                logger.frame(direction, line)
                os.write(dst_fd, line)
                activity.put(direction)
    except BrokenPipeError:
        logger.event(f"{direction}: broken pipe")
    except OSError as exc:
        logger.event(f"{direction}: os error {exc!r}")
    except Exception as exc:
        logger.event(f"{direction}: exception {exc!r}")
    finally:
        done.set()


def build_command(version: str, model: str, passthrough_args: list[str]) -> list[str]:
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        raise RuntimeError("npx was not found on PATH")
    return [
        npx,
        "-y",
        f"@xai-official/grok@{version}",
        "agent",
        "--model",
        model,
        *passthrough_args,
        "stdio",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Zed ACP to Grok Build observability proxy"
    )
    parser.add_argument(
        "version",
        nargs="?",
        help=f"Deprecated. Grok npm package version. Prefer ${ENV_VERSION}.",
    )
    parser.add_argument(
        "legacy_model",
        nargs="?",
        help=f"Deprecated. Grok model ID. Prefer ${ENV_MODEL}.",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=None,
        help=f"Deprecated. Grok model ID. Prefer ${ENV_MODEL}.",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=DEFAULT_IDLE_TIMEOUT_SECONDS,
        help="Exit after N seconds without traffic. 0 disables the timeout.",
    )
    args, grok_args = parser.parse_known_args()

    version = safe_version(args.version or os.environ.get(ENV_VERSION, DEFAULT_VERSION))
    model = safe_model(
        args.model or args.legacy_model or os.environ.get(ENV_MODEL, DEFAULT_MODEL)
    )
    base_dir = Path(__file__).resolve().parent
    session_dir, paths = make_session_paths(base_dir)
    logger = Logger(paths)

    command = build_command(version, model, grok_args)
    logger.event(f"proxy started pid={os.getpid()} session_dir={session_dir}")
    logger.event(f"selected version={version}")
    logger.event(f"selected model={model}")
    logger.event(f"command={command!r}")
    logger.event(f"cwd={Path.cwd()}")
    logger.event(f"PATH={os.environ.get('PATH', '')}")

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except Exception as exc:
        logger.event(f"failed to start grok: {exc!r}")
        print(f"failed to start grok: {exc}", file=sys.stderr, flush=True)
        return 127

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    logger.event(f"grok started pid={process.pid}")
    done = threading.Event()
    activity: queue.Queue[str] = queue.Queue()

    for stream_name, stream in (
        ("stdin", sys.stdin.buffer),
        ("stdout", sys.stdout.buffer),
        ("grok_stdin", process.stdin),
        ("grok_stdout", process.stdout),
        ("grok_stderr", process.stderr),
    ):
        try:
            msvcrt.setmode(stream.fileno(), os.O_BINARY)
            logger.event(f"binary mode set stream={stream_name} fd={stream.fileno()}")
        except Exception as exc:
            logger.event(
                f"failed to set binary mode stream={stream_name} fd={stream.fileno()}: {exc!r}"
            )

    threads = [
        threading.Thread(
            target=pipe_bytes,
            args=(
                sys.stdin.buffer,
                process.stdin,
                "zed_to_grok",
                "zed->grok",
                logger,
                done,
                activity,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=pipe_grok_to_zed_filtered,
            args=(process.stdout, sys.stdout.buffer, logger, done, activity),
            daemon=True,
        ),
        threading.Thread(
            target=pipe_stderr, args=(process.stderr, logger, done), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    last_activity = time.monotonic()
    exit_code: int | None = None
    try:
        while not done.is_set():
            exit_code = process.poll()
            if exit_code is not None:
                logger.event(f"grok exited code={exit_code}")
                done.set()
                break
            try:
                activity.get(timeout=0.25)
                last_activity = time.monotonic()
            except queue.Empty:
                pass
            if (
                args.idle_timeout > 0
                and time.monotonic() - last_activity > args.idle_timeout
            ):
                logger.event(f"idle timeout after {args.idle_timeout}s")
                done.set()
                break
    except KeyboardInterrupt:
        logger.event("proxy interrupted")
        done.set()

    if process.poll() is None:
        logger.event("terminating grok")
        process.terminate()
        try:
            exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.event("killing grok after terminate timeout")
            process.kill()
            exit_code = process.wait(timeout=5)

    for thread in threads:
        thread.join(timeout=1)

    if exit_code is None:
        exit_code = process.poll()
    logger.event(f"proxy exiting code={exit_code}")
    return int(exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
