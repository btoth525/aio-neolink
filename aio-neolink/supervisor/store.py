"""Camera persistence.

Cameras are stored as a JSON list in /data/cameras.json so they survive add-on
restarts and updates. Passwords are stored here too — /data is private to the add-on,
but treat the file as a secret (chmod 600). A future hardening step is to push
credentials into HA's secrets store instead.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.environ.get("AIO_NEOLINK_DATA", "/data"))
CAMERAS_FILE = DATA_DIR / "cameras.json"


@dataclass
class Camera:
    """One camera definition.

    `name` is the RTSP path Frigate connects to: rtsp://host:8554/<name>.
    `uid` is only used for battery cameras (UDP discovery); wired cameras use
    address + port instead.
    """
    name: str
    address: Optional[str] = None      # e.g. "192.168.1.236" (wired cameras)
    port: int = 9000                    # Baichuan "Basic Service Port", almost always 9000
    uid: Optional[str] = None          # battery cameras only (UDP)
    username: str = "admin"
    password: str = ""
    stream: str = "both"               # "mainStream" | "subStream" | "both"
    enabled: bool = True
    # Free-form capability cache populated by the control plane on probe.
    capabilities: dict = field(default_factory=dict)

    def rtsp_paths(self) -> list[str]:
        if self.stream == "both":
            return [self.name, f"{self.name}/subStream"]
        return [self.name]


class CameraStore:
    def __init__(self, path: Path = CAMERAS_FILE) -> None:
        self.path = path
        self._cameras: dict[str, Camera] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._cameras = {}
            return
        raw = json.loads(self.path.read_text() or "[]")
        self._cameras = {c["name"]: Camera(**c) for c in raw}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(c) for c in self._cameras.values()], indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # --- CRUD ---------------------------------------------------------------------
    def list(self) -> list[Camera]:
        return list(self._cameras.values())

    def get(self, name: str) -> Optional[Camera]:
        return self._cameras.get(name)

    def upsert(self, camera: Camera) -> Camera:
        self._cameras[camera.name] = camera
        self.save()
        return camera

    def delete(self, name: str) -> bool:
        existed = self._cameras.pop(name, None) is not None
        if existed:
            self.save()
        return existed
