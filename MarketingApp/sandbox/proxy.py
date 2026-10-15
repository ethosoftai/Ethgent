"""Turns a tool file from an installed pack into a callable that runs in the sandbox.

The tool's code is never executed in the agent's process: the name, docstring and parameters
the model sees are read from the source with ``ast``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Callable

from . import executor

_ANNOTATIONS = {"int": int, "float": float, "bool": bool, "str": str, "list": list, "dict": dict}


def _annotation(node: ast.AST | None) -> Any:
    if node is None:
        return inspect.Parameter.empty
    if isinstance(node, ast.Name):
        return _ANNOTATIONS.get(node.id, str)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):  # "int" as a string annotation
        return _ANNOTATIONS.get(node.value, str)
    return str


def _default(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def read_signature(source: str, name: str) -> tuple[inspect.Signature, str]:
    """Signature and docstring of the top-level function ``name``, without running the module."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            break
    else:
        raise ValueError(f"{name} fonksiyonu export edilmemis.")
    a = node.args
    params: list[inspect.Parameter] = []
    positional = list(a.posonlyargs) + list(a.args)
    defaults = [inspect.Parameter.empty] * (len(positional) - len(a.defaults)) + [_default(d) for d in a.defaults]
    for index, (arg, default) in enumerate(zip(positional, defaults)):
        kind = inspect.Parameter.POSITIONAL_ONLY if index < len(a.posonlyargs) else inspect.Parameter.POSITIONAL_OR_KEYWORD
        params.append(inspect.Parameter(arg.arg, kind, default=default, annotation=_annotation(arg.annotation)))
    if a.vararg:
        params.append(inspect.Parameter(a.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        value = inspect.Parameter.empty if default is None else _default(default)
        params.append(inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY, default=value, annotation=_annotation(arg.annotation)))
    if a.kwarg:
        params.append(inspect.Parameter(a.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(params), ast.get_docstring(node) or ""


def format_result(outcome: dict[str, Any]) -> Any:
    """What the model receives: the tool's own return value, or a readable error."""
    if outcome.get("ok"):
        return outcome.get("result")
    message = f"[Sandbox hata]: {outcome.get('error') or 'bilinmeyen hata'}"
    if outcome.get("withheld_env"):
        message += f" (ayrilmis env adlari verilmedi: {', '.join(outcome['withheld_env'])})"
    return message


def make_sandboxed_tool(
    name: str,
    path: Path,
    env_names: list[str] | None = None,
    env_values: Callable[[], dict[str, str]] | None = None,
    description: str = "",
    requires_approval: bool = False,
) -> Callable[..., Any]:
    """A function with the tool's name, docstring and signature that runs it in the sandbox.

    With ``requires_approval`` the function is async and asks the host's approval gate before it
    sends anything to the sandbox; without an approval nothing runs.
    """
    path = Path(path)
    signature, doc = read_signature(path.read_text(encoding="utf-8"), name)
    declared = list(env_names or [])

    def run(bound: inspect.BoundArguments, approved: bool) -> Any:
        values = env_values() if env_values else {}
        try:
            outcome = executor.run_tool(
                path, name, dict(bound.kwargs), declared, values, positional=list(bound.args), approved=approved
            )
        except executor.SandboxError as exc:
            return f"[Sandbox hata]: {exc}"
        return format_result(outcome)

    if requires_approval:

        async def sandboxed_tool(*args: Any, **kwargs: Any) -> Any:
            import asyncio

            from MarketingApp.environments.approval_runtime import describe_call, request_tool_approval

            try:
                bound = signature.bind(*args, **kwargs)
            except TypeError as exc:
                return f"[Sandbox hata]: {exc}"
            if not await request_tool_approval(name, describe_call(name, description, dict(bound.arguments))):
                return f"[Onay yok]: {name} onaylanmadi, calistirilmadi."
            return await asyncio.to_thread(run, bound, True)

    else:

        def sandboxed_tool(*args: Any, **kwargs: Any) -> Any:
            try:
                bound = signature.bind(*args, **kwargs)
            except TypeError as exc:
                return f"[Sandbox hata]: {exc}"
            return run(bound, False)

    sandboxed_tool.__name__ = name
    sandboxed_tool.__qualname__ = name
    sandboxed_tool.__doc__ = doc or description
    sandboxed_tool.__signature__ = signature  # type: ignore[attr-defined]
    sandboxed_tool.ethgent_sandboxed = True  # type: ignore[attr-defined]
    sandboxed_tool.ethgent_requires_approval = requires_approval  # type: ignore[attr-defined]
    return sandboxed_tool
