"""
Refresh the bundled Milesight payload codecs.

Downloads the release branch of github.com/Milesight-IoT/codec (MIT) and
writes milesight_gateway_api/codecs/:

- scripts.tar.xz, the decoder and encoder script of each model. The scripts
  repeat the same helper functions, so one solid xz archive takes 0.27 MB
  where the files take 6.5 MB (codec 1.5.72).
- models.json.xz, a compact index of each model's fields taken from its
  codec.json, with the value ranges the encoder documents.

Run it from the repository root; it needs no extra package.
"""

from __future__ import annotations

import io
import json
import lzma
import re
import shutil
import tarfile
import urllib.request
import zipfile
from pathlib import Path

SOURCE = "https://github.com/Milesight-IoT/codec/archive/refs/heads/release.zip"
TARGET = Path(__file__).resolve().parents[1] / "milesight_gateway_api" / "codecs"
INDEX = "vendors/milesight-iot/devices.json"
# JSDoc in the encoders: "@param {number} report_interval unit: minute, range: [1, 1440]"
_RANGE = re.compile(
    r"@param\s+\{number\}\s+([\w.]+)[^\n]*?range:\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]"
)


def _number(text: str) -> float | int | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def _field(obj: dict) -> dict:
    field = {"name": obj.get("name", obj["id"]), "type": obj.get("data_type", "")}
    for key, target in (
        ("unit", "unit"),
        ("access_mode", "access"),
        ("value_type", "value_type"),
        ("reference", "reference"),
    ):
        if obj.get(key):
            field[target] = obj[key]
    if obj.get("access_mode") in ("RW", "W") and str(obj.get("value", "")) != "":
        # Milesight's example value; stands in for a referenced field the
        # device has not reported yet.
        field["default"] = obj["value"]
    values = {
        str(v["value"]): v["name"]
        for v in obj.get("values") or []
        if isinstance(v, dict) and "value" in v and "name" in v
    }
    if values:
        field["values"] = values
    return field


def _ranges(encoder: str) -> dict[str, tuple]:
    """Value ranges by field ID, as the encoder's JSDoc states them."""
    ranges = {}
    for name, low, high in _RANGE.findall(encoder):
        low_value, high_value = _number(low), _number(high)
        if low_value is not None and high_value is not None:
            ranges[name] = (low_value, high_value)
    return ranges


def main() -> None:
    """Download the codec repository and rewrite the bundle."""
    with urllib.request.urlopen(SOURCE, timeout=120) as response:
        blob = response.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        _write(archive)


def _write(archive: zipfile.ZipFile) -> None:
    prefix = archive.namelist()[0].split("/")[0] + "/"

    def read(path: str) -> bytes:
        return archive.read(prefix + path)

    shutil.rmtree(TARGET, ignore_errors=True)
    TARGET.mkdir(parents=True)
    (TARGET / "LICENSE").write_bytes(read("LICENSE"))
    version = json.loads(read("version.json"))
    models = {}
    scripts: dict[str, bytes] = {}
    for device in json.loads(read(INDEX))["devices"]:
        script = device.get("decoder_script")
        if not script:
            continue
        name = Path(script).name
        scripts[name] = read(script)
        encoder_name, ranges = "", {}
        if device.get("encoder_script"):
            encoder = read(device["encoder_script"])
            encoder_name = Path(device["encoder_script"]).name
            scripts[encoder_name] = encoder
            ranges = _ranges(encoder.decode("utf-8", "replace"))
        codec: dict = {}
        if device.get("codec"):
            try:
                codec = json.loads(read(device["codec"]))
            except (KeyError, ValueError):
                codec = {}
        fields = {}
        for obj in codec.get("object") or []:
            if isinstance(obj, dict) and obj.get("id"):
                field = fields[obj["id"]] = _field(obj)
                if obj["id"] in ranges:
                    field["min"], field["max"] = ranges[obj["id"]]
        models[device["id"]] = {
            "name": device.get("name", device["id"]),
            "description": device.get("description", ""),
            "deveui": device.get("deveui", ""),
            "decoder": name,
            "encoder": encoder_name,
            # Milesight's example uplink; the tests decode it for every model.
            "example": str(codec.get("bytes") or "").lower(),
            "fields": fields,
        }
    index = json.dumps(
        {
            "version": version.get("version"),
            "date": version.get("date"),
            "models": models,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    (TARGET / "models.json.xz").write_bytes(lzma.compress(index, preset=9))
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in sorted(scripts):
            info = tarfile.TarInfo(name)
            info.size = len(scripts[name])
            tar.addfile(info, io.BytesIO(scripts[name]))
    (TARGET / "scripts.tar.xz").write_bytes(
        lzma.compress(tar_bytes.getvalue(), preset=9 | lzma.PRESET_EXTREME)
    )
    size = sum(p.stat().st_size for p in TARGET.iterdir())
    print(
        f"{len(models)} models, {len(scripts)} scripts, codec version"
        f" {version.get('version')}, bundle {size / 1e6:.2f} MB"
    )


if __name__ == "__main__":
    main()
