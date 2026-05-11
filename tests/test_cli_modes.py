from __future__ import annotations

from run_pipeline import build_parser


def test_parser_has_mode_and_full_data() -> None:
    parser = build_parser()
    args = parser.parse_args(["--mode", "task2", "--full-data", "true", "--workers", "6", "--incremental", "false"])
    assert args.mode == "task2"
    assert args.full_data is True
    assert args.workers == 6
    assert args.incremental is False
