"""Rejected Docker environment config must not become log payloads."""

import logging
from typing import Any

import pytest

from tools.environments import docker


PAYLOAD = "SYNTHETIC_VALIDATION_PAYLOAD_73d8"


class Unprintable:
    def __repr__(self):
        raise AssertionError("Rejected configuration must never be formatted")


@pytest.mark.parametrize(
    "normalize,value,expected,category",
    [
        (docker._normalize_env_dict, [PAYLOAD], {}, "not a dict"),
        (docker._normalize_env_dict, {PAYLOAD + "!": "ignored"}, {}, "invalid docker_env key"),
        (docker._normalize_env_dict, {PAYLOAD: [PAYLOAD]}, {}, "non-string docker_env value"),
        (docker._normalize_forward_env_names, [{PAYLOAD: PAYLOAD}], [], "non-string docker_forward_env"),
        (docker._normalize_forward_env_names, [PAYLOAD + "!"], [], "invalid docker_forward_env"),
        (docker._normalize_env_dict, Unprintable(), {}, "not a dict"),
        (docker._normalize_env_dict, {Unprintable(): "ignored"}, {}, "invalid docker_env key"),
        (docker._normalize_env_dict, {"KEY": Unprintable()}, {}, "non-string docker_env value"),
        (docker._normalize_forward_env_names, [Unprintable()], [], "non-string docker_forward_env"),
    ],
    ids=["non-dict", "invalid-key", "complex-value", "forward-non-string", "forward-invalid",
         "unprintable-container", "unprintable-key", "unprintable-value", "unprintable-forward"],
)
def test_rejected_config_warnings_do_not_format_payloads(normalize, value, expected, category, caplog):
    with caplog.at_level(logging.WARNING, logger=docker.logger.name):
        assert normalize(value) == expected

    records = [record for record in caplog.records if record.name == docker.logger.name]
    assert len(records) == 1
    assert category in records[0].getMessage()
    assert PAYLOAD not in records[0].getMessage()
    assert not records[0].args
    assert records[0].exc_info is None
    assert records[0].stack_info is None


def test_env_normalization_preserves_valid_values_and_input(caplog):
    values = {" TEXT ": PAYLOAD, "INT": 7, "FLOAT": 1.5, "BOOL": False, "EMPTY": ""}
    before = dict(values)
    with caplog.at_level(logging.WARNING, logger=docker.logger.name):
        assert docker._normalize_env_dict(values) == {
            "TEXT": PAYLOAD, "INT": "7", "FLOAT": "1.5", "BOOL": "False", "EMPTY": "",
        }
        assert docker._normalize_env_dict(None) == {}
        assert docker._normalize_env_dict({}) == {}
    assert values == before
    assert not caplog.records


def test_forward_normalization_preserves_order_deduplication_and_input(caplog):
    names = [" FIRST ", "SECOND", "FIRST", "", "  "]
    before = list(names)
    with caplog.at_level(logging.WARNING, logger=docker.logger.name):
        assert docker._normalize_forward_env_names(names) == ["FIRST", "SECOND"]
        assert docker._normalize_forward_env_names(None) == []
    assert names == before
    assert not caplog.records


def test_invalid_entries_do_not_discard_valid_siblings(caplog):
    # Configuration is untyped at this boundary; deliberately mix valid names
    # with the malformed values the normalizer promises to reject.
    names: list[Any] = ["GOOD", "bad key", {}, "ALSO_GOOD"]
    with caplog.at_level(logging.WARNING, logger=docker.logger.name):
        assert docker._normalize_env_dict({"GOOD": PAYLOAD, "bad key": "ignored", "BAD": []}) == {"GOOD": PAYLOAD}
        assert docker._normalize_forward_env_names(names) == ["GOOD", "ALSO_GOOD"]
    assert PAYLOAD not in caplog.text
