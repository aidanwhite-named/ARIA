"""subprocess 실행 유틸리티.

지켜야 할 것:
- shell=False, 인수 배열. 프롬프트를 셸 명령 문자열에 절대 연결하지 않는다.
- stdin 쓰기와 stdout 읽기를 동시에 한다. 긴 프롬프트를 stdin 에 밀어넣으면서
  stdout 을 안 빨아들이면 파이프 버퍼(Windows 기본 ~64KB)가 차면서 양쪽이
  서로를 기다리는 교착에 빠진다.
- 디코딩은 UTF-8 고정. Windows 기본 cp949 로 읽으면 한글이 깨진다.
- 취소는 자식까지 포함한 프로세스 트리 전체를 종료한다.

줄 조립을 직접 하는 이유:

  asyncio 의 StreamReader.readline() 은 한 줄이 스트림 limit(기본 64KB)을
  넘으면 **내부 버퍼를 비우고** ValueError 를 던진다. 즉 데이터가 사라진다.
  실측으로 200,013 바이트 중 131,070 바이트가 소실됐고, 남은 조각이 그대로
  JSON 파서에 들어갔다.

  Claude 의 최종 result 이벤트나 agy 의 result 이벤트는 답변 전문을 한 줄에
  담기 때문에, 긴 분석 결과에서는 반드시 발생한다. 그래서 readline 을 쓰지
  않고 read() 로 청크를 받아 개행을 직접 찾는다. 이 방식에는 길이 상한이 없다.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import psutil

LineHandler = Callable[[str], Awaitable[None]]

_READ_CHUNK = 65536
# StreamReader 자체 한도. read() 만 쓰므로 예외 경로는 없지만, 여유를 둬서
# 전송 계층이 자주 멈췄다 재개하는 것을 줄인다.
_STREAM_LIMIT = 4 * 1024 * 1024
# 개행 없이 계속 들어오는 병적인 스트림에 대한 메모리 가드.
_MAX_PENDING_LINE_BYTES = 16 * 1024 * 1024


@dataclass
class ProcessResult:
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    stdout: str = ""
    stderr: str = ""
    launch_error: str | None = None
    # 최종 결과를 다 받았는데 CLI 가 스스로 끝나지 않아 ARIA 가 끊었다.
    # 이건 타임아웃이 아니다 — 결과는 전부 손에 있다. 둘을 같은 칸에 넣으면
    # "답을 못 받았다"와 "답은 받았는데 프로세스가 안 죽었다"가 구분되지 않는다.
    completed_without_exit: bool = False


@dataclass
class RunningProcess:
    process: asyncio.subprocess.Process
    cancelled: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_running: dict[str, RunningProcess] = {}


def kill_process_tree(pid: int, timeout: float = 5.0) -> None:
    """해당 프로세스와 자손만 종료한다."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    try:
        children = parent.children(recursive=True)
    except psutil.Error:
        children = []

    for proc in [*children, parent]:
        with contextlib.suppress(psutil.Error):
            proc.terminate()

    _, alive = psutil.wait_procs([*children, parent], timeout=timeout)
    for proc in alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()


async def cancel_job(job_id: str) -> bool:
    handle = _running.get(job_id)
    if handle is None:
        return False
    handle.cancelled = True
    pid = handle.process.pid
    if pid is not None:
        await asyncio.to_thread(kill_process_tree, pid)
    return True


def is_running(job_id: str) -> bool:
    return job_id in _running


async def _pump_lines(
    stream: asyncio.StreamReader | None,
    chunks: list[str],
    handler: LineHandler | None,
    max_capture_chars: int,
) -> None:
    """청크를 읽어 개행 단위로 조립한다. 줄 길이에 상한이 없다."""
    if stream is None:
        return

    buffer = bytearray()
    captured = 0

    async def emit(raw: bytes) -> None:
        nonlocal captured
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace")
        if captured < max_capture_chars:
            chunks.append(text)
            captured += len(text)
        if handler is not None:
            line = text.rstrip("\r\n")
            if line:
                await handler(line)

    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        buffer.extend(chunk)

        while True:
            index = buffer.find(b"\n")
            if index < 0:
                break
            await emit(bytes(buffer[: index + 1]))
            del buffer[: index + 1]

        if len(buffer) > _MAX_PENDING_LINE_BYTES:
            # 개행이 오지 않는 비정상 스트림. 버리지 않고 끊어서 넘긴다.
            await emit(bytes(buffer))
            buffer.clear()

    if buffer:
        await emit(bytes(buffer))


async def run_streaming(
    job_id: str,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    stdin_data: str | None = None,
    on_stdout_line: LineHandler | None = None,
    on_stderr_line: LineHandler | None = None,
    timeout_seconds: int = 900,
    max_capture_chars: int = 8_000_000,
    completion_signal: asyncio.Event | None = None,
    completion_grace_seconds: float = 15.0,
) -> ProcessResult:
    """CLI 를 실행하며 stdout/stderr 를 줄 단위로 흘려보낸다.

    completion_signal 을 주면 "최종 결과를 다 받았다"는 신호로 쓴다. 스트림이
    닫히기를 기다리는 것만으로는 부족하기 때문이다 — 실측(job d39dc2cc, agy
    1.1.26): 모델이 최종 response 와 status SUCCESS 를 보낸 뒤에도 프로세스가
    끝나지 않아 stdout 이 열린 채로 남았고, ARIA 는 15분 뒤 타임아웃으로 실패
    처리했다. 결과 텍스트·사용량·도구 기록이 전부 손에 있는데도 보고서가
    버려졌다.

    신호가 오면 CLI 가 스스로 끝날 시간을 completion_grace_seconds 만큼 주고,
    그래도 살아 있으면 트리를 끊고 completed_without_exit 로 표시한다. 신호 없이
    시간만 넘긴 경우는 예전 그대로 timed_out 이다.
    """
    result = ProcessResult()

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
    except (OSError, NotImplementedError, ValueError) as exc:
        result.launch_error = f"{type(exc).__name__}: {exc}"
        return result

    handle = RunningProcess(process=process)
    _running[job_id] = handle

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    async def feed_stdin() -> None:
        if process.stdin is None:
            return
        try:
            if stdin_data:
                process.stdin.write(stdin_data.encode("utf-8"))
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
                process.stdin.close()

    tasks = [
        asyncio.create_task(feed_stdin()),
        asyncio.create_task(
            _pump_lines(process.stdout, stdout_chunks, on_stdout_line, max_capture_chars)
        ),
        asyncio.create_task(
            _pump_lines(process.stderr, stderr_chunks, on_stderr_line, max_capture_chars)
        ),
    ]

    pump = asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))
    signal_wait: asyncio.Task | None = None
    if completion_signal is not None:
        signal_wait = asyncio.ensure_future(completion_signal.wait())

    async def _reap(limit: float) -> None:
        # 프로세스가 실제로 사라진 것을 확인하고 종료 코드를 남긴다.
        with contextlib.suppress(Exception):
            result.exit_code = await asyncio.wait_for(process.wait(), timeout=limit)

    async def _kill() -> None:
        if process.pid is not None:
            await asyncio.to_thread(kill_process_tree, process.pid)
        pump.cancel()
        for task in tasks:
            task.cancel()

    try:
        waiters: list[asyncio.Future] = [pump]
        if signal_wait is not None:
            waiters.append(signal_wait)
        done, _ = await asyncio.wait(
            waiters, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
        )

        if pump in done:
            # 스트림이 정상적으로 닫혔다. 예전 경로 그대로.
            try:
                result.exit_code = await asyncio.wait_for(process.wait(), timeout=30)
            except (asyncio.TimeoutError, TimeoutError):
                result.timed_out = True
                await _kill()
                await _reap(10)
        elif signal_wait is not None and signal_wait in done:
            # 최종 결과는 받았다. CLI 가 스스로 끝날 시간을 준다.
            finished, _ = await asyncio.wait([pump], timeout=completion_grace_seconds)
            if pump in finished:
                try:
                    result.exit_code = await asyncio.wait_for(process.wait(), timeout=30)
                except (asyncio.TimeoutError, TimeoutError):
                    result.completed_without_exit = True
                    await _kill()
                    await _reap(10)
            else:
                result.completed_without_exit = True
                await _kill()
                await _reap(10)
        else:
            # 신호도 없고 스트림도 안 닫혔다. 진짜 타임아웃이다.
            result.timed_out = True
            await _kill()
            await _reap(10)
    finally:
        if signal_wait is not None and not signal_wait.done():
            signal_wait.cancel()
        if not pump.done():
            pump.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        _running.pop(job_id, None)

    result.cancelled = handle.cancelled
    result.stdout = "".join(stdout_chunks)
    result.stderr = "".join(stderr_chunks)
    return result


async def run_capture(
    argv: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin_data: str | None = None,
    timeout_seconds: int = 60,
) -> ProcessResult:
    """probe 용 단발 실행.

    입력이 없으면 stdin 을 DEVNULL 로 준다. PIPE 로 열어두고
    communicate(None) 을 호출하면 asyncio 가 stdin 을 닫지 않아서, 자식이
    EOF 를 기다리며 멈춘다. 실측: `agy models` 가 이 상태에서 60초 타임아웃까지
    갔다가 빈 출력으로 끝났고, ARIA 는 이를 "로그인 필요"로 오판했다.
    """
    result = ProcessResult()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
    except (OSError, NotImplementedError, ValueError) as exc:
        result.launch_error = f"{type(exc).__name__}: {exc}"
        return result

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin_data.encode("utf-8") if stdin_data else None),
            timeout=timeout_seconds,
        )
        result.exit_code = process.returncode
        result.stdout = stdout.decode("utf-8", errors="replace")
        result.stderr = stderr.decode("utf-8", errors="replace")
    except (asyncio.TimeoutError, TimeoutError):
        result.timed_out = True
        if process.pid is not None:
            await asyncio.to_thread(kill_process_tree, process.pid)
    return result
