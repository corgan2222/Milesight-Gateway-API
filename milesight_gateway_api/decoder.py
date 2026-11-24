"""
Decode uplinks and encode downlinks with Milesight's own payload codecs.

The codecs are the JavaScript files of github.com/Milesight-IoT/codec (MIT),
bundled as one xz archive under codecs/ and refreshed with
scripts/update_codecs.py. They are the same scripts a Milesight gateway runs,
executed here by dukpy (Duktape, ES5). dukpy is an optional dependency:
install the "decoder" extra.

Only the scripts of models in use are unpacked and compiled: load() takes
them out of the archive, or the first decode() or encode() of a model does.

Enumerations come back as raw numbers; the catalog names them.
"""

from __future__ import annotations

import io
import json
import lzma
import tarfile
import threading
from collections.abc import Iterable
from functools import cache, lru_cache
from importlib import resources
from typing import Any

_CODECS = "codecs"
_ARCHIVE = "scripts.tar.xz"


class DecodeError(ValueError):
    """The decoder rejected the payload."""


class EncodeError(ValueError):
    """The encoder rejected the command, usually a value out of range."""


class UnknownModelError(KeyError):
    """No bundled codec for this model."""


def _resource(name: str) -> bytes:
    return (resources.files(__package__) / _CODECS / name).read_bytes()


# Unpacked scripts by file name. Unpacking may run in a worker thread while
# another thread decodes; the lock keeps two unpacks from racing.
_scripts: dict[str, str] = {}
_unpack_lock = threading.Lock()


def _unpack(names: set[str]) -> None:
    """Take these scripts out of the archive in one pass."""
    with _unpack_lock:
        wanted = names - _scripts.keys()
        if not wanted:
            return
        with tarfile.open(fileobj=io.BytesIO(_resource(_ARCHIVE)), mode="r:xz") as tar:
            for member in tar:
                if member.name in wanted:
                    _scripts[member.name] = (
                        tar.extractfile(member).read().decode("utf-8")
                    )
                    wanted.discard(member.name)
                    if not wanted:
                        break


def _read(name: str) -> str:
    if name not in _scripts:
        _unpack({name})
    return _scripts[name]


def _model_scripts(model_id: str) -> set[str]:
    model = models().get(model_id) or {}
    return {name for name in (model.get("decoder"), model.get("encoder")) if name}


def load(model_ids: Iterable[str]) -> None:
    """
    Unpack the decoder and encoder of these models, in one pass.

    It reads the package's archive; an event loop runs it in a worker thread
    before the first uplink of a model arrives.
    """
    names: set[str] = set()
    for model_id in model_ids:
        names |= _model_scripts(model_id)
    if names:
        _unpack(names)


def is_loaded(model_id: str) -> bool:
    """Whether decoding this model needs no file access any more."""
    return _model_scripts(model_id) <= _scripts.keys()


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    """Return the bundled index: codec version, date and models by ID."""
    return json.loads(lzma.decompress(_resource("models.json.xz")))


def models() -> dict[str, dict[str, Any]]:
    """Return the known models by ID (``am102``, ``em300_th``, ...)."""
    return catalog()["models"]


def model_for_eui(dev_eui: str) -> str | None:
    """
    Guess the model from the devEUI, or None.

    Milesight devEUIs start with a per-model prefix. The longest matching
    prefix wins; a tie between models returns None, since the EUI alone
    cannot tell them apart.
    """
    eui = dev_eui.replace(":", "").lower()
    best: list = []
    length = 0
    for model_id, model in models().items():
        prefix = str(model.get("deveui") or "").lower()
        if not prefix or not eui.startswith(prefix):
            continue
        if len(prefix) > length:
            best, length = [model_id], len(prefix)
        elif len(prefix) == length:
            best.append(model_id)
    return best[0] if len(best) == 1 else None


@cache
def _interpreter() -> Any:
    """
    One interpreter for all decoders.

    Creating one reads dukpy's runtime files, so it happens once, in
    preload() if the caller cares. Each decoder runs inside its own function
    scope (see _load), so the scripts' identical global names do not collide.
    """
    try:
        import dukpy  # pylint: disable=import-outside-toplevel  # optional extra
    except ImportError as error:
        raise RuntimeError(
            "Decoding needs dukpy: pip install 'Milesight-Gateway-API[decoder]'"
        ) from error
    interpreter = dukpy.JSInterpreter()
    interpreter.evaljs("var __decoders = {}; null;")
    return interpreter


_loaded: set[str] = set()


def _load(script: str, function: str) -> Any:
    interpreter = _interpreter()
    if script not in _loaded:
        # The trailing null: dukpy returns the last value as JSON, and an
        # assignment of a function has none.
        interpreter.evaljs(
            "__decoders[dukpy.name] = (function () {\n"
            + _read(script)
            + f"\n;return {function};\n}})(); null;",
            name=script,
        )
        _loaded.add(script)
    return interpreter


def preload(model_ids: Iterable[str] = ()) -> None:
    """
    Read the catalog, start the interpreter and unpack these models.

    All three touch files. An event loop that must not block on file access
    calls this in a worker thread first.
    """
    catalog()
    _interpreter()
    load(model_ids)


def decode(model_id: str, data: bytes) -> dict[str, Any]:
    """Decode one uplink payload of the given model into a dict."""
    model = models().get(model_id)
    if model is None:
        raise UnknownModelError(model_id)
    try:
        result = _load(model["decoder"], "milesightDeviceDecode").evaljs(
            "__decoders[dukpy.name](dukpy.bytes)",
            name=model["decoder"],
            bytes=list(data),
        )
    except Exception as error:  # dukpy raises its own JSRuntimeError
        raise DecodeError(f"{model_id}: {error}") from error
    return result if isinstance(result, dict) else {}


def encode(model_id: str, payload: dict[str, Any]) -> bytes:
    """
    Encode a command or setting for a device into downlink bytes (FPort 85).

    The payload uses the decoder's vocabulary, nested as the encoder expects
    it: ``{"temperature_control": {"enable": 1}}``. nest() builds that from
    dotted keys. Enumerations take their raw numbers.
    """
    model = models().get(model_id)
    if model is None or not model.get("encoder"):
        raise UnknownModelError(model_id)
    try:
        result = _load(model["encoder"], "milesightDeviceEncode").evaljs(
            "__decoders[dukpy.name](dukpy.payload)",
            name=model["encoder"],
            payload=payload,
        )
    except Exception as error:  # dukpy raises its own JSRuntimeError
        # The first line is the encoder's message; the rest is a JS stack.
        message = str(error).splitlines()[0].removeprefix("Error: ")
        raise EncodeError(f"{model_id}: {message}") from error
    if not result:
        raise EncodeError(f"{model_id}: nothing to send for {sorted(payload)}")
    return bytes(int(b) & 0xFF for b in result)


def nest(flat: dict[str, Any]) -> dict[str, Any]:
    """Turn dotted keys into nested objects, the inverse of flatten()."""
    nested: dict[str, Any] = {}
    for key, value in flat.items():
        node = nested
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return nested


def flatten(decoded: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """
    Flatten nested objects into dotted keys, as models.json names them.

    Lists (history records, schedules) are left out: they are no current
    state.
    """
    flat: dict[str, Any] = {}
    for key, value in decoded.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, f"{name}."))
        elif not isinstance(value, list):
            flat[name] = value
    return flat
