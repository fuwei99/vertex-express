from __future__ import annotations

import atexit
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional


from .codec import SOCKS_INBOUND_PORT, build_config

LATEST_URL = "https://github.com/SagerNet/sing-box/releases/latest"
DL_BASE = "https://github.com/SagerNet/sing-box/releases/download/"
PKG_NAME = "sing-box"
APP_DIR = Path(__file__).resolve().parents[2]
BIN_DIR = APP_DIR / "bin"
BIN_PATH = BIN_DIR / ("netcore.exe" if os.name == "nt" else "netcore")
CONFIG_PATH = APP_DIR / "worker-config.json"
LOG_PATH = APP_DIR / "worker.log"


class WorkerManager:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._active_uri: Optional[str] = None
        self._active_name: Optional[str] = None
        self._port = SOCKS_INBOUND_PORT
        atexit.register(self.stop)

    def find_binary(self) -> Optional[str]:
        candidates = [str(BIN_PATH)]
        if os.name != "nt":
            candidates.extend(["/usr/local/bin/netcore", "/tmp/netcore"])
        for p in candidates:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return shutil.which("netcore") or shutil.which("sing-box")

    def _resolve_latest_version(self) -> str:
        req = urllib.request.Request(LATEST_URL, headers={"User-Agent": "python-urllib"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            final = resp.url
        tag = final.rstrip("/").rsplit("/", 1)[-1]
        if not tag.startswith("v"):
            raise RuntimeError(f"Failed to resolve version from {final}")
        return tag

    def _build_download_url(self, tag: str) -> str:
        version = tag.lstrip("v")
        arch = platform.machine().lower()
        if arch in ("x86_64", "amd64"):
            arch_suffix = "amd64"
        elif arch in ("aarch64", "arm64"):
            arch_suffix = "arm64"
        else:
            raise RuntimeError(f"Unsupported architecture: {arch}")
        if os.name == "nt":
            filename = f"{PKG_NAME}-{version}-windows-{arch_suffix}.zip"
        elif platform.system().lower() == "darwin":
            filename = f"{PKG_NAME}-{version}-darwin-{arch_suffix}.tar.gz"
        else:
            filename = f"{PKG_NAME}-{version}-linux-{arch_suffix}.tar.gz"
        return f"{DL_BASE}{tag}/{filename}"

    def ensure_binary(self) -> str:
        existing = self.find_binary()
        if existing:
            return existing
        tag = self._resolve_latest_version()
        url = self._build_download_url(tag)
        req = urllib.request.Request(url, headers={"User-Agent": "python-urllib"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        payload: Optional[bytes] = None
        if url.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    base = os.path.basename(name)
                    if base.lower() in ("sing-box.exe", "sing-box", "netcore.exe", "netcore"):
                        payload = zf.read(name)
                        break
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                for member in tf.getmembers():
                    if member.isfile() and os.path.basename(member.name) in ("sing-box", "netcore"):
                        f = tf.extractfile(member)
                        if f is not None:
                            payload = f.read()
                            break
        if payload is None:
            raise RuntimeError("Worker binary not found in release package")
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        with open(BIN_PATH, "wb") as f:
            f.write(payload)
        try:
            BIN_PATH.chmod(BIN_PATH.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception:
            pass
        return str(BIN_PATH)

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def proxy_url(self) -> str:
        return f"socks5://127.0.0.1:{self._port}"

    def status(self) -> dict[str, Any]:
        binary = self.find_binary()
        return {
            "binary_available": bool(binary),
            "binary_path": binary or "",
            "running": self.is_running,
            "active_uri": self._active_uri or "",
            "active_name": self._active_name or "",
            "socks_port": self._port,
            "proxy_url": self.proxy_url if self.is_running else "",
        }

    def stop(self) -> None:
        p = self._proc
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait(timeout=2)
            finally:
                self._proc = None
                self._active_uri = None
                self._active_name = None
        
        # Windows 强力清理
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/F", "/IM", "netcore.exe", "/T"], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except: pass

    def start_with_uri(self, uri: str, name: str = "", port: int = SOCKS_INBOUND_PORT) -> str:
        self.stop() # 先彻底清理
        binary = self.ensure_binary()
        cfg = build_config(uri, socks_port=port)
        
        # 确保目录存在
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        
        log_f = open(LOG_PATH, "ab")
        cmd = [str(binary), "run", "-c", str(CONFIG_PATH)]
        try:
            # 使用 CREATE_NO_WINDOW 隐藏黑窗口，并确保完全独立的进程组
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt":
                flags |= 0x08000000 # CREATE_NO_WINDOW
            
            proc = subprocess.Popen(cmd, stdout=log_f, stderr=log_f, creationflags=flags)
        except Exception as exc:
            log_f.close()
            raise RuntimeError(f"Worker start failed: {exc}")
        time.sleep(1)
        if proc.poll() is not None:
            raise RuntimeError(f"Worker exited immediately (exit code {proc.returncode}).\n{_tail_file(str(LOG_PATH), 60)}")
        self._proc = proc
        self._active_uri = uri
        self._active_name = name
        self._port = port
        return self.proxy_url


def _tail_file(path: str, n: int = 40) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-n:])
    except Exception:
        return ""


worker = WorkerManager()
