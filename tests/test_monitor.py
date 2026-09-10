from __future__ import annotations

import io

from smart_finqa.monitor import ProgressTracker


def test_progress_output_is_ascii_for_windows_terminals(monkeypatch) -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    monkeypatch.setattr("sys.stdout", stream)
    tracker = ProgressTracker(total=2, description="Parsing PDFs")

    tracker.update(force=True)
    tracker.finish()

    stream.flush()
