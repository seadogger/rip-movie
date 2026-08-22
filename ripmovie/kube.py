"""Thin kubectl wrapper. Services are ClusterIP-only, so the Mac reaches Nextcloud/Jellyfin
by exec-ing into their pods rather than over a public URL.
"""
from __future__ import annotations

import subprocess
from typing import Optional


class KubeError(Exception):
    pass


def _run(argv: list[str], timeout: int = 60, input_bytes: Optional[bytes] = None) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout, input=input_bytes,
        )
    except FileNotFoundError as e:
        raise KubeError("kubectl not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise KubeError(f"kubectl timed out: {' '.join(argv)}") from e
    if proc.returncode != 0:
        raise KubeError(proc.stderr.decode("utf-8", "replace").strip() or f"kubectl failed: {argv}")
    return proc.stdout.decode("utf-8", "replace")


def pod_name(namespace: str, selector: str, context: Optional[str] = None) -> str:
    argv = ["kubectl"]
    if context:
        argv += ["--context", context]
    argv += ["-n", namespace, "get", "pod", "-l", selector,
             "--field-selector=status.phase=Running", "-o",
             "jsonpath={.items[0].metadata.name}"]
    name = _run(argv).strip()
    if not name:
        raise KubeError(f"no running pod for selector {selector!r} in ns {namespace!r}")
    return name


def exec_in(namespace: str, pod: str, argv: list[str], container: Optional[str] = None,
            context: Optional[str] = None, timeout: int = 120) -> str:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += ["-n", namespace, "exec", pod]
    if container:
        cmd += ["-c", container]
    cmd += ["--", *argv]
    return _run(cmd, timeout=timeout)


def exec_popen(namespace: str, pod: str, argv: list[str], container: Optional[str] = None,
               context: Optional[str] = None):
    """Start a pod command and return its Popen (stdout=PIPE) so the output can be streamed/piped —
    e.g. `cat <file>` into ffmpeg's stdin to extract a few frames without pulling the whole file."""
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += ["-n", namespace, "exec", pod]
    if container:
        cmd += ["-c", container]
    cmd += ["--", *argv]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)


def exec_stdin_file(namespace: str, pod: str, argv: list[str], local_path: str,
                    container: Optional[str] = None, context: Optional[str] = None,
                    timeout: int = 7200) -> str:
    """Run a pod command with a local file streamed to its stdin (no tar / no full-file buffering)."""
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += ["-n", namespace, "exec", "-i", pod]
    if container:
        cmd += ["-c", container]
    cmd += ["--", *argv]
    try:
        with open(local_path, "rb") as fh:
            proc = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise KubeError(f"local file not found: {local_path}") from e
    except subprocess.TimeoutExpired as e:
        raise KubeError(f"stream into pod timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise KubeError(proc.stderr.decode("utf-8", "replace").strip() or "stream into pod failed")
    return proc.stdout.decode("utf-8", "replace")


def exec_stdout_file(namespace: str, pod: str, argv: list[str], local_path: str,
                     container: Optional[str] = None, context: Optional[str] = None,
                     timeout: int = 14400) -> str:
    """Run a pod command and stream its stdout straight to a local file (no full-file buffering).
    Used to pull a library master back out of Nextcloud for re-upscaling (`cat <path>`)."""
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += ["-n", namespace, "exec", pod]
    if container:
        cmd += ["-c", container]
    cmd += ["--", *argv]
    try:
        with open(local_path, "wb") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError as e:
        raise KubeError("kubectl not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise KubeError(f"pull from pod timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise KubeError(proc.stderr.decode("utf-8", "replace").strip() or "pull from pod failed")
    return local_path


def reachable(context: Optional[str] = None) -> bool:
    try:
        argv = ["kubectl"]
        if context:
            argv += ["--context", context]
        argv += ["version", "-o", "json"]
        _run(argv, timeout=15)
        return True
    except KubeError:
        return False
