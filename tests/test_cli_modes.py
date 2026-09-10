from __future__ import annotations

import pytest

from smart_finqa.cli import _apply_cli_overrides, build_parser
from smart_finqa.config import AppConfig


def test_parser_has_mode_and_full_data() -> None:
    parser = build_parser()
    args = parser.parse_args(["--mode", "task2", "--full-data", "true", "--workers", "6", "--incremental", "false"])
    assert args.mode == "task2"
    assert args.full_data is True
    assert args.workers == 6
    assert args.incremental is False


def test_parser_defaults_do_not_override_config() -> None:
    args = build_parser().parse_args([])
    config = AppConfig(mode="task3", ingest_workers=7, full_data=True)

    result = _apply_cli_overrides(config, args)

    assert result.mode == "task3"
    assert result.ingest_workers == 7
    assert result.full_data is True


def test_parser_rejects_invalid_boolean() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--full-data", "maybe"])
