"""Runs one tool function inside the sandbox and prints its result as JSON.

Standard library only, and it never imports MarketingApp: it is executed as a plain script
(``python -I runner.py``) inside a container or a bare subprocess. It reads one JSON request
on stdin::

    {"file": "/tool/prices.py", "function": "get_price", "args": {...},
     "paths": ["/deps"], "limits": {"memory_mb": 512, "cpu_seconds": 30}}

and writes one line to stdout: ``ETHGENT_SANDBOX_RESULT <json>``. Anything the tool prints
goes to stderr, so it cannot forge the result line.
"""

import asyncio
import inspect
import json
import signal
import sys
import traceback
import types

MARKER = "ETHGENT_SANDBOX_RESULT "


def _expired(signum, frame):  # noqa: ARG001
    raise TimeoutError("wall-clock limit reached")


def _apply_limits(limits: dict) -> None:
    wall_seconds = float(limits.get("wall_seconds") or 0)
    if wall_seconds > 0 and hasattr(signal, "setitimer"):
        # A sleeping or blocked tool uses no CPU, so RLIMIT_CPU would not stop it.
        signal.signal(signal.SIGALRM, _expired)
        signal.setitimer(signal.ITIMER_REAL, wall_seconds)
    try:
        import resource
    except ImportError:  # not on POSIX: the container limits still apply
        return
    memory_mb = int(limits.get("memory_mb") or 0)
    if memory_mb > 0:
        size = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (size, size))
    cpu_seconds = int(limits.get("cpu_seconds") or 0)
    if cpu_seconds > 0:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    file_mb = int(limits.get("file_mb") or 0)
    if file_mb > 0:
        size = file_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))


def _emit(payload: dict, out) -> None:
    out.write(MARKER + json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    out.flush()


def main() -> int:
    real_stdout = sys.stdout
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:
        _emit({"ok": False, "error": f"bad request: {exc}"}, real_stdout)
        return 2

    sys.stdout = sys.stderr  # tool output must not reach the result channel
    try:
        _apply_limits(request.get("limits") or {})
        for path in request.get("paths") or []:
            if path not in sys.path:
                sys.path.append(path)
        file_path = request["file"]
        name = request["function"]
        module = types.ModuleType("ethgent_sandboxed_tool")
        module.__file__ = file_path
        with open(file_path, encoding="utf-8") as handle:
            source = handle.read()
        exec(compile(source, file_path, "exec"), module.__dict__)
        func = getattr(module, name, None)
        if not callable(func):
            raise RuntimeError(f"{name} is not exported by the tool file")
        args = request.get("args") or {}
        result = func(*(request.get("positional") or []), **args)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        payload = {"ok": True, "result": result}
    except MemoryError:
        payload = {"ok": False, "error": "memory limit exceeded"}
    except BaseException as exc:  # noqa: BLE001 - every failure must come back as a result
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(limit=5)[-2000:],
        }
    if hasattr(signal, "setitimer"):
        signal.setitimer(signal.ITIMER_REAL, 0)
    sys.stdout = real_stdout
    _emit(payload, real_stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
