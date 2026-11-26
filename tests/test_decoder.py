"""Tests for the bundled Milesight payload decoders.

Skipped where dukpy is not installed.
"""

# pytest idioms: fixtures as arguments, tests reach into internals and
# import optional packages late, tables of gateway answers stay on one line.
# pylint: disable=missing-function-docstring,redefined-outer-name,protected-access
# pylint: disable=import-outside-toplevel,unused-argument,line-too-long

import pytest

from milesight_gateway_api import decoder

dukpy = pytest.importorskip("dukpy")


def test_the_catalog_lists_models_with_decoders_and_fields():
    """Every model names a bundled decoder script."""
    models = decoder.models()
    assert len(models) > 100
    assert models["wt101"]["name"] == "WT101"
    assert models["em300_th"]["fields"]["temperature"]["unit"] == "°C"


def test_a_time_unit_is_the_one_the_encoder_takes():
    """codec.json says seconds for the WS203's intervals; its encoder reads minutes."""
    fields = decoder.models()["ws203"]["fields"]
    assert fields["report_interval"]["unit"] == "min"
    assert fields["collection_interval"]["unit"] == "min"
    # The encoder refuses what would be a plain value in seconds.
    with pytest.raises(decoder.EncodeError, match="between 1 and 1440"):
        decoder.encode("ws203", {"report_interval": 3600})


def test_a_range_is_the_one_the_user_guide_documents():
    """The VS321 takes 2 to 1440 min; its encoder would let 1 through."""
    field = decoder.models()["vs321"]["fields"]["report_interval"]
    assert (field["min"], field["max"]) == (2, 1440)


# Upstream examples that belong to another model.
FOREIGN_EXAMPLES = {"uc11_t1": "codec 1.5.72 ships the AM102 example"}


@pytest.mark.parametrize("model_id", sorted(decoder.models()))
def test_every_model_decodes_milesights_example(model_id):
    """Each decoder runs under Duktape and returns values for its example."""
    example = decoder.models()[model_id]["example"]
    if not example:
        pytest.skip("no example uplink")
    if model_id in FOREIGN_EXAMPLES:
        pytest.skip(FOREIGN_EXAMPLES[model_id])

    assert decoder.decode(model_id, bytes.fromhex(example))


@pytest.mark.parametrize(
    ("model_id", "payload", "expected"),
    [
        (
            "ws203",
            "01754003674400046871050000",
            {"battery": 64, "temperature": 6.8, "humidity": 56.5, "occupancy": 0},
        ),
        (
            "wt101",
            "0175630367e6000592640b90000009900000",
            {
                "battery": 99,
                "temperature": 23,
                "valve_opening": 100,
                "motor_position": 0,
                "motor_stroke": 0,
            },
        ),
    ],
)
def test_real_uplinks_decode_like_on_the_gateway(model_id, payload, expected):
    """Payloads captured from devices decode to the gateway's values."""
    assert decoder.decode(model_id, bytes.fromhex(payload)) == expected


def test_a_rejected_payload_is_a_decode_error():
    """A decoder that throws becomes DecodeError, not a dukpy exception."""
    with pytest.raises(decoder.DecodeError):
        decoder.decode("am102", bytes.fromhex("ff99"))


def test_an_unknown_model_is_refused():
    """Without a decoder there is nothing to run."""
    with pytest.raises(decoder.UnknownModelError):
        decoder.decode("xx999", b"\x01")


def test_the_eui_prefix_names_a_model_only_when_unique():
    """A prefix shared by several models proves nothing."""
    assert decoder.model_for_eui("24E1247140000001") == "wt101"
    shared = next(
        prefix
        for prefix in (m["deveui"] for m in decoder.models().values())
        if sum(m["deveui"] == prefix for m in decoder.models().values()) > 1
    )
    assert decoder.model_for_eui(shared + "0000000") is None
    assert decoder.model_for_eui("2CF7F12000000003") is None


@pytest.mark.parametrize(
    ("model_id", "payload", "expected"),
    [
        ("wt101", {"report_status": 1}, "ff2800"),
        ("wt101", {"target_temperature": 21, "temperature_tolerance": 1}, "ffb1150a00"),
        ("wt101", {"temperature_control": {"enable": 0}}, "ffb300"),
        ("ws203", {"report_interval": 600}, "ff8e005802"),
    ],
)
def test_commands_encode_like_milesights_encoder(model_id, payload, expected):
    """Bytes as Node gives them for the same encoder and payload."""
    assert decoder.encode(model_id, payload).hex() == expected


def test_a_rejected_command_names_the_reason():
    """The encoder's own message, without the JavaScript stack."""
    with pytest.raises(
        decoder.EncodeError, match="temperature_tolerance must be a number$"
    ):
        decoder.encode("wt101", {"target_temperature": 21})


def test_only_the_models_in_use_are_unpacked(monkeypatch):
    """load() takes a model's two scripts out of the archive, nothing else."""
    monkeypatch.setattr(decoder, "_scripts", {})
    assert not decoder.is_loaded("wt101")

    decoder.load(["wt101"])

    assert decoder.is_loaded("wt101")
    assert set(decoder._scripts) == {"wt101-decoder.js", "wt101-encoder.js"}


def test_dotted_keys_nest_for_the_encoder():
    """nest() undoes flatten()."""
    flat = {"a": 1, "b.c": 2, "b.d.e": 3}
    assert decoder.nest(flat) == {"a": 1, "b": {"c": 2, "d": {"e": 3}}}
    assert decoder.flatten(decoder.nest(flat)) == flat


def test_nested_values_flatten_to_dotted_keys():
    """Objects become dotted keys; lists are history, not state."""
    assert decoder.flatten(
        {"a": 1, "b": {"c": 2, "d": {"e": 3}}, "history": [{"a": 0}]}
    ) == {"a": 1, "b.c": 2, "b.d.e": 3}
