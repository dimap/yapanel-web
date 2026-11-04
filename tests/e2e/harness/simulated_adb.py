"""Simulated ADB client for E2E tests without physical hardware or adb server dependencies."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


class SimulatedAdb:
    """Deterministic ADB client for isolated testing."""

    def __init__(self, serial: str = "127.0.0.1:5555", root_access: bool = True):
        self.serial = serial
        self.root_access = root_access
        self.commands: List[str] = []
        self.pushes: List[Tuple[str, str]] = []
        self.pulls: List[Tuple[str, str]] = []
        self._needs_su: Optional[bool] = False if root_access else True
        self.command_handlers: Dict[str, Callable[[str], Tuple[str, str, int]]] = {}
        self.file_system: Dict[str, str] = {}

    def register_command_handler(self, prefix: str, handler: Callable[[str], Tuple[str, str, int]]):
        self.command_handlers[prefix] = handler

    def shell(self, command: str, timeout: float = 15.0, check: bool = True) -> Tuple[str, str, int]:
        """Execute a simulated adb shell command with CRLF normalization."""
        self.commands.append(command)

        # Check for registered custom command handlers
        for prefix, handler in self.command_handlers.items():
            if command.startswith(prefix):
                out, err, code = handler(command)
                # Ensure CRLF is normalized
                out = out.replace("\r\n", "\n")
                if check and code != 0:
                    raise RuntimeError(f"ADB command failed ({code}): {err}")
                return out, err, code

        # Built-in command emulation
        stdout = ""
        stderr = ""
        code = 0

        if "getprop ro.product.model" in command:
            stdout = "Yandex Station\n"
        elif "getprop ro.build.version.release" in command:
            stdout = "9\n"
        elif "id" in command:
            stdout = "uid=0(root) gid=0(root)\n" if self.root_access else "uid=2000(shell) gid=2000(shell)\n"
        elif "cat /proc/uptime" in command:
            stdout = "123456.78 98765.43\n"
        elif "which su" in command:
            stdout = "/system/bin/su\n" if self.root_access else ""
            code = 0 if self.root_access else 1
        elif command.startswith("echo "):
            stdout = command[5:].strip() + "\n"
        elif command.startswith("cat "):
            if "|" in command or "||" in command or "&&" in command or ";" in command:
                stdout = "OK\n"
            else:
                target = command.split()[-1]
                if target in self.file_system:
                    stdout = self.file_system[target]
                else:
                    stderr = f"cat: {target}: No such file or directory\n"
                    code = 1
        else:
            # Default success response
            stdout = "OK\n"

        stdout = stdout.replace("\r\n", "\n")
        if check and code != 0:
            raise RuntimeError(f"Command failed ({code}): {stderr}")
        return stdout, stderr, code

    def push(self, local: str | Path, remote: str, timeout: float = 120.0) -> bool:
        """Simulate adb push."""
        self.pushes.append((str(local), remote))
        if os.path.isfile(local):
            with open(local, "r", encoding="utf-8", errors="ignore") as f:
                self.file_system[remote] = f.read()
        return True

    def pull(self, remote: str, local: str | Path, timeout: float = 120.0) -> bool:
        """Simulate adb pull."""
        self.pulls.append((remote, str(local)))
        if remote in self.file_system:
            os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
            with open(local, "w", encoding="utf-8") as f:
                f.write(self.file_system[remote])
            return True
        return False
