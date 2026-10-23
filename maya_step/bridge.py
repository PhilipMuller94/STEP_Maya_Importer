"""Run the CAD worker without contaminating Maya's Python/DLL environment."""

import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .archive import read_archive

ROOT = Path(__file__).resolve().parent.parent


def python_executable():
    explicit = os.environ.get("STEP_IMPORT_PYTHON")
    if explicit:
        candidate = Path(explicit).expanduser()
    else:
        from maya import cmds
        setting = "mayaStepPython"
        saved = cmds.optionVar(query=setting) if cmds.optionVar(exists=setting) else ""
        candidate = (Path(saved).expanduser() if saved else
                     ROOT / ".venv-step" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    if not candidate.is_file():
        raise RuntimeError(
            "STEP converter Python not found: {}. Create .venv-step using the README, "
            "or choose a Python executable in the STEP import dialog.".format(candidate))
    return str(candidate.resolve())


def _cache_file(source, options, executable, shape=False):
    stat = source.stat()
    signature = {"source": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                 "python": executable, "format": "step-mm-v1"}
    if not shape:
        signature.update(linear=options.linear_deflection, angular=options.angular_deflection)
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode("utf-8"))
    files = [ROOT / "requirements-converter.txt"]
    if not shape:
        files += [ROOT / "maya_step" / "converter.py", ROOT / "maya_step" / "archive.py"]
    for path in files:
        digest.update(path.read_bytes())
    return ROOT / "target" / "step-cache" / (digest.hexdigest() + (".brepbin" if shape else ".stepmesh"))


def convert_file(source, options, progress=None, status=None):
    source = Path(source).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in (".step", ".stp"):
        raise ValueError("Choose an existing .step or .stp file")
    executable = python_executable()
    cached = _cache_file(source, options, executable)
    cached.parent.mkdir(parents=True, exist_ok=True)
    if options.use_cache and cached.is_file():
        try:
            payload = read_archive(cached)
        except (OSError, ValueError):
            pass  # Incomplete/obsolete cache entries are regenerated atomically.
        else:
            if progress:
                progress()
            if status:
                status("Using cached geometry...", 0.88)
            payload["cache_hit"] = True
            return payload
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory(prefix="work_", dir=cached.parent) as temporary:
        output = Path(temporary) / "mesh.stepmesh"
        status_file = Path(temporary) / "status.json"
        log = Path(temporary) / "converter.log"
        command = [executable, str(ROOT / "maya_step" / "converter.py"), str(source),
                   str(output), "--linear", str(options.linear_deflection),
                   "--angular", str(options.angular_deflection), "--binary", "--status", str(status_file)]
        if options.use_cache:
            command += ["--shape-cache", str(_cache_file(source, options, executable, shape=True))]
        with log.open("wb") as stream:
            process = subprocess.Popen(
                command, cwd=temporary, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                if progress is None:
                    process.wait(timeout=options.timeout)
                else:
                    deadline = time.monotonic() + options.timeout
                    while process.poll() is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, options.timeout)
                        progress()
                        if status and status_file.is_file():
                            try:
                                state = json.loads(status_file.read_text(encoding="utf-8"))
                                status(state["message"], state["fraction"])
                            except (OSError, ValueError, KeyError):
                                pass
                        try:
                            process.wait(timeout=min(0.1, remaining))
                        except subprocess.TimeoutExpired:
                            pass
            except subprocess.TimeoutExpired:
                raise RuntimeError("STEP conversion exceeded {} seconds".format(options.timeout)) from None
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if process.returncode != 0 or not output.is_file():
            detail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise RuntimeError("STEP converter failed (exit {}).\n{}".format(process.returncode, detail))
        # Validate the complete record layout before committing a cache file.
        payload = read_archive(output, lazy=options.use_cache)
        if options.use_cache:
            output.replace(cached)
            payload["meshes"].path = cached
        payload["cache_hit"] = False
        return payload
