"""Body colours are visual metadata. Never write to a speaker from this module."""
import json
import os
import re
import tempfile
import threading
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent / "static/model-colors.json").read_text(encoding="utf-8"))
LOCK = threading.RLock()

# Keep the original color= field, but do not turn a missing command or a failed read into black.
COLOR_PROBE = r'''
if ! command -v yandex-getcolor >/dev/null 2>&1; then
  echo color=
  echo color_read=unavailable
elif model_body_color=$(yandex-getcolor 2>/dev/null); then
  printf 'color=%s\n' "$model_body_color"
  if [ -n "$model_body_color" ]; then echo color_read=available; else echo color_read=empty; fi
else
  echo color=
  echo color_read=error
fi
'''


IDENTITY_PROBE = r'''
if command -v device_id >/dev/null 2>&1; then
  model_color_output=$(device_id -r 2>/dev/null)
else
  model_color_output=$(LD_LIBRARY_PATH=/system/vendor/quasar/libs /system/vendor/quasar/maind --device-id 2>/dev/null)
fi
printf '%s\n' "$model_color_output" | while IFS= read -r model_color_line; do
  printf 'model_color_identity_line=%s\n' "$model_color_line"
done
'''


def probe_identity(output):
    """Проверяем все строки ответа, включая ID перед завершающими логами maind."""
    prefix = "model_color_identity_line="
    return identity("\n".join(line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)))


def palette(model):
    if not isinstance(model, str):
        raise ValueError("неизвестная 3D-модель")
    family = CATALOG["aliases"].get(model, model)
    if family not in CATALOG["palettes"]:
        raise ValueError("неизвестная 3D-модель")
    return CATALOG["palettes"][family]


def normalize(model, value):
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    key = {"gray": "grey"}.get(key, key)
    return key if key in palette(model) else None


def identity(value):
    """Accept only native v1/v2 IDs, never a hostname, IP, diagnostic line or cached version."""
    if not isinstance(value, str):
        return None
    for line in value.splitlines():
        candidate = re.sub(r"^Device ID:\s*", "", line.strip(), flags=re.I)
        if re.fullmatch(r"(?:[A-Za-z0-9]{14}|[A-Fa-f0-9]{32})", candidate):
            return candidate
    return None


def diagnostic(raw, read):
    if read == "error":
        return {"state": "error", "reason": "command_failed"}
    if read == "unavailable":
        return {"state": "unknown", "reason": "command_missing"}
    if not isinstance(raw, str) or not raw.strip():
        return {"state": "unknown", "reason": "empty"}
    # Palette compatibility is checked separately for the actual visual model.
    return {"state": "available", "reason": None}


class ColorStore:
    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "devices": {}}
        except (OSError, ValueError) as exc:
            raise ValueError("не удалось прочитать сохранённые цвета") from exc
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("devices"), dict)
                or any(not isinstance(v, dict) for v in value["devices"].values())):
            raise ValueError("неверный формат сохранённых цветов")
        return value

    def selection(self, device, model):
        palette(model)
        with LOCK:
            entry = self._load()["devices"].get(device, {})
            selected = entry.get(model)
            if selected is not None and normalize(model, selected) != selected:
                raise ValueError("сохранённый цвет отсутствует в палитре")
            return model in entry, selected

    def save(self, device, model, color):
        colors = palette(model)
        if not identity(device) or device != identity(device):
            raise ValueError("не удалось определить идентификатор колонки")
        if color is not None and (not isinstance(color, str) or color not in colors):
            raise ValueError("цвет отсутствует в палитре этой модели")
        with LOCK:
            data = self._load()
            # An explicit null records a return to auto; old browser preferences must not resurrect.
            data["devices"].setdefault(device, {})[model] = color
            tmp = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".model-colors-", delete=False) as f:
                    tmp = Path(f.name)
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except OSError as exc:
                raise ValueError("не удалось сохранить цвет на сервере панели") from exc
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)

    def result(self, device, model, raw, detection):
        colors = palette(model)
        configured, selected = self.selection(device, model) if device else (False, None)
        detected = normalize(model, raw) if detection["state"] == "available" else None
        reason = detection["reason"]
        if detection["state"] == "available" and detected is None:
            reason = "unsupported"
        return {"model": model, "device_id": device, "detected": detected,
                "selected": selected, "configured": configured,
                "effective": selected or detected,
                "source": "user" if selected else "device" if detected else "unknown",
                "reason": reason, "palette": colors}
