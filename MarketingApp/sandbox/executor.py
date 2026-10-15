"""Runs a tool from an installed pack outside the agent's process.

Two modes, chosen by ``ETHGENT_SANDBOX`` (``auto`` by default):

* ``container``: a throwaway Docker container per call. The tool file and the runner are
  mounted read-only; the root filesystem is read-only; all capabilities are dropped; memory,
  CPU, process count and time are limited; the network can be cut with
  ``ETHGENT_SANDBOX_NETWORK=none``. The container sees no environment variable except the
  ones the tool declared, so it never sees the agent's model keys or other tools' secrets.
* ``subprocess``: a separate Python process with an empty environment (again, only the
  declared variables) and the same memory/CPU/time limits. It shares the host's filesystem
  and network, so it is weaker than a container; it is the fallback when Docker is missing.

``auto`` uses ``container`` when Docker is reachable and ``subprocess`` otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

RUNNER_PATH = Path(__file__).with_name("runner.py")
MARKER = "ETHGENT_SANDBOX_RESULT "
VALID_MODES = {"auto", "container", "subprocess"}

# Variables the agent itself reads. A pack may not receive these even if it declares them,
# otherwise a pack could ask for OPENAI_API_KEY by name and get the agent's model key.
RESERVED_ENV_NAMES = frozenset(
    {
        "BASE_MODEL_NAME", "BASE_REASONING_EFFORT", "BROWSER_ACTION_DELAY_SCALE", "BROWSER_AGENT_MODEL",
        "BROWSER_REASONING_EFFORT", "DEFAULT_CHAT_ID", "DISCORD_TOKEN", "DISCORD_ALLOWED_USER_IDS",
        "DISCORD_ADMIN_IDS", "GEMINI_API_KEY", "GEMINI_API_KEY_FALLBACK", "GEMINI_API_KEY_SECONDARY",
        "GEMINI_OPENAI_BASE_URL", "HEARTBEAT_CHAT_ID", "KIMI_API_KEY", "LLM_PROVIDER", "MODEL_API_KEY",
        "MODEL_PROVIDER", "MOONSHOT_API_KEY", "MOONSHOT_BASE_URL", "OPENAI_API_KEY", "OPENAI_COMPAT_BASE_URL",
        "PANEL_ALLOWED_ORIGINS", "PEXELS_API_KEY", "SUBMODEL_MODEL_NAME", "SUBMODEL_REASONING_EFFORT",
        "TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_USER_IDS", "TELEGRAM_ADMIN_IDS", "TOOL_GENERATOR_MODEL",
        "TOOL_GENERATOR_REASONING_EFFORT", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    }
)
RESERVED_ENV_PREFIXES = (
    "ETHGENT_", "SUPABASE_", "ASSET_POOL_", "AGENT_", "PLATFORM_DATA_", "GEMINI_", "OPENAI_", "MOONSHOT_",
    "KIMI_", "TELEGRAM_", "DISCORD_", "AWS_", "GITHUB_", "GH_", "RAILWAY_",
)


class SandboxError(RuntimeError):
    """The sandbox could not run the tool at all (as opposed to the tool failing)."""


def is_reserved_env_name(name: str) -> bool:
    return name in RESERVED_ENV_NAMES or name.startswith(RESERVED_ENV_PREFIXES)


def settings() -> dict[str, Any]:
    mode = (os.getenv("ETHGENT_SANDBOX") or "auto").strip().lower()
    if mode not in VALID_MODES:
        raise SandboxError(f"ETHGENT_SANDBOX must be one of {sorted(VALID_MODES)}, not {mode!r}")
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {
        "mode": mode,
        "image": os.getenv("ETHGENT_SANDBOX_IMAGE") or f"python:{version}-slim",
        "network": os.getenv("ETHGENT_SANDBOX_NETWORK") or "bridge",
        "timeout": float(os.getenv("ETHGENT_SANDBOX_TIMEOUT") or 60),
        "memory_mb": int(os.getenv("ETHGENT_SANDBOX_MEMORY_MB") or 512),
        "cpus": float(os.getenv("ETHGENT_SANDBOX_CPUS") or 1),
        "pids": int(os.getenv("ETHGENT_SANDBOX_PIDS") or 64),
        "share_deps": (os.getenv("ETHGENT_SANDBOX_SHARE_DEPS") or "1") != "0",
        "startup_grace": float(os.getenv("ETHGENT_SANDBOX_STARTUP_GRACE") or 20),
    }


@lru_cache(maxsize=1)
def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return False
    return probe.returncode == 0 and bool(probe.stdout.strip())


def resolve_mode(conf: dict[str, Any] | None = None) -> str:
    conf = conf or settings()
    if conf["mode"] == "container" and not docker_available():
        raise SandboxError("ETHGENT_SANDBOX=container but Docker is not reachable")
    if conf["mode"] == "auto":
        return "container" if docker_available() else "subprocess"
    return conf["mode"]


def tool_environment(env_names: list[str], values: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Only the declared, non-reserved names, with values from ``values`` or the process env."""
    env: dict[str, str] = {}
    withheld: list[str] = []
    for name in env_names or []:
        if is_reserved_env_name(name):
            withheld.append(name)
            continue
        value = values.get(name, os.environ.get(name))
        if value:
            env[name] = value
    return env, withheld


def _dependency_paths() -> list[str]:
    """The agent's installed packages, so a pack tool can import requests and the like."""
    paths = []
    for key in ("purelib", "platlib"):
        path = sysconfig.get_paths().get(key)
        if path and os.path.isdir(path) and path not in paths:
            paths.append(path)
    return paths


def _parse_output(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(MARKER):
            try:
                return json.loads(line[len(MARKER):])
            except Exception:
                return None
    return None


def _container_command(conf: dict[str, Any], name: str, tool_file: Path, env: dict[str, str]) -> tuple[list[str], list[str]]:
    cmd = [
        "docker", "run", "--rm", "-i", "--name", name,
        "--network", conf["network"],
        "--memory", f"{conf['memory_mb']}m", "--memory-swap", f"{conf['memory_mb']}m",
        "--cpus", str(conf["cpus"]), "--pids-limit", str(conf["pids"]),
        "--read-only", "--tmpfs", "/tmp:rw,size=64m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "65534:65534", "-w", "/tmp",
        "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{tool_file.resolve()}:/tool/{tool_file.name}:ro",
        "-v", f"{RUNNER_PATH.resolve()}:/runner/runner.py:ro",
    ]
    inner_paths: list[str] = []
    image_version = conf["image"].split(":", 1)[-1]
    host_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if conf["share_deps"] and image_version.startswith(host_version):
        for index, path in enumerate(_dependency_paths()):
            inner = f"/deps{index}"
            cmd += ["-v", f"{path}:{inner}:ro"]
            inner_paths.append(inner)
    for key in env:  # values travel through the docker client's environment, not the command line
        cmd += ["-e", key]
    cmd += [conf["image"], "python", "-I", "/runner/runner.py"]
    return cmd, inner_paths


def run_tool(
    tool_file: Path,
    function: str,
    args: dict[str, Any],
    env_names: list[str] | None = None,
    env_values: dict[str, str] | None = None,
    *,
    positional: list[Any] | None = None,
    conf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ``function`` from ``tool_file`` in the sandbox. Never raises for a tool failure."""
    conf = conf or settings()
    mode = resolve_mode(conf)
    tool_file = Path(tool_file)
    if not tool_file.is_file():
        raise SandboxError(f"{tool_file} not found")
    env, withheld = tool_environment(list(env_names or []), dict(env_values or {}))
    limits = {
        "memory_mb": conf["memory_mb"],
        "cpu_seconds": max(1, int(conf["timeout"])),
        "wall_seconds": conf["timeout"],
        "file_mb": 64,
    }
    started = time.monotonic()

    if mode == "container":
        name = f"ethgent-sbx-{uuid.uuid4().hex[:12]}"
        cmd, inner_paths = _container_command(conf, name, tool_file, env)
        request = {"file": f"/tool/{tool_file.name}", "function": function, "args": args, "positional": positional or [], "paths": inner_paths, "limits": limits}
        client_env = {**os.environ, **env}
        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                env=client_env,
                timeout=conf["timeout"] + conf["startup_grace"],
            )
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            # The tool ignored its own time limit: kill the container and wait until it is gone.
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=20)
            subprocess.run(["docker", "wait", name], capture_output=True, timeout=20)
            stdout, stderr, code, timed_out = str(exc.stdout or ""), str(exc.stderr or ""), -9, True
    else:
        request = {"file": str(tool_file.resolve()), "function": function, "args": args, "positional": positional or [], "paths": _dependency_paths(), "limits": limits}
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with tempfile.TemporaryDirectory(prefix="ethgent-sbx-") as workdir:
            proc = subprocess.Popen(
                [sys.executable, "-I", str(RUNNER_PATH)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=workdir,
                env={**base_env, "HOME": workdir, **env},
                start_new_session=True,
            )
            try:
                # The runner stops the tool itself at the timeout; this is the backstop for a tool that
                # catches that and keeps going.
                stdout, stderr = proc.communicate(json.dumps(request), timeout=conf["timeout"] + 5)
                code, timed_out = proc.returncode, False
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
                code, timed_out = -9, True

    duration = round(time.monotonic() - started, 3)
    base = {"mode": mode, "withheld_env": withheld, "duration_s": duration}
    if timed_out:
        return {**base, "ok": False, "error": f"timed out after {conf['timeout']:.0f}s and was killed"}
    parsed = _parse_output(stdout)
    if parsed is None:
        reason = "killed (memory or CPU limit)" if code in (-9, 137, -24, 152) else f"exited with code {code}"
        tail = (stderr or "").strip()[-600:]
        return {**base, "ok": False, "error": f"no result: the sandboxed process {reason}", "stderr": tail}
    return {**base, **parsed}
