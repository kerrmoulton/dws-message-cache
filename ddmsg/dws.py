"""Verified DWS v1.0.54 adapter. No shell, HTTP calls, or runtime skill discovery."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def executable():
    found = os.environ.get("DDMSG_DWS") or shutil.which("dws")
    if not found:
        raise RuntimeError("DWS not found. Install DWS or set DDMSG_DWS to its native executable")
    path = Path(found).resolve()
    binary = "dws.exe" if os.name == "nt" else "dws"
    candidates = [path.parent.parent / "vendor" / binary,
                  path.parent / "node_modules" / "dingtalk-workspace-cli" / "vendor" / binary]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    if path.suffix.lower() in (".cmd", ".bat"):
        raise RuntimeError("Cannot safely invoke this DWS batch shim. Set DDMSG_DWS to vendor/dws.exe")
    return str(path)


class Client:
    def __init__(self, profile, binary=None):
        self.profile, self.binary = profile, binary
        self.deadline = None

    def command(self, args, fmt="json"):
        return [self.binary or executable(), *args, "--profile", self.profile, "--format", fmt]

    def execute(self, args, structured=True):
        error = ""
        for attempt in range(2):
            remaining = 45 if self.deadline is None else min(45, self.deadline - time.monotonic())
            if remaining <= 0:
                raise RuntimeError("Collector time budget exhausted; incomplete source retains its watermark")
            try:
                result = subprocess.run(self.command(args + (["--verbose"] if attempt else [])),
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=remaining,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode:
                    raise RuntimeError((result.stderr + result.stdout)[-3000:])
                if not structured:
                    # download-media v1.0.54 prints INFO lines even with --format json.
                    return {"success": True}
                value = json.loads(result.stdout)
                if not isinstance(value, dict) or value.get("success") is False or value.get("errorCode") or "error" in value:
                    raise RuntimeError(json.dumps(value, ensure_ascii=False)[-3000:])
                return value
            except (subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError) as exc:
                error = str(exc)
        raise RuntimeError("DWS read failed: " + error)

    def __call__(self, args, profile=None):
        if profile and profile != self.profile:
            raise RuntimeError("Profile mismatch")
        return self.execute(args)

    def download(self, cid, mid, resource, path):
        return self.execute(["chat", "message", "download-media", "--type", "mediaId",
            "--resource-id", resource, "--message-id", mid, "--open-conversation-id", cid,
            "--output", str(path)], structured=False)
