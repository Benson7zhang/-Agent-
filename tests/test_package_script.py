from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = ROOT / "package.sh"


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_package_script_keeps_empty_output_dirs_and_excludes_runtime_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "demo_project"
    project_dir.mkdir()

    shutil.copy2(PACKAGE_SCRIPT, project_dir / "package.sh")

    required_files = {
        "run_pipeline.py": "print('ok')\n",
        "run.sh": "#!/bin/bash\n",
        "README.md": "# demo\n",
        "requirements.txt": "pytest\n",
        "config.example.yaml": "mode: all\n",
        "config.production.yaml": "mode: all\n",
        ".gitignore": "__pycache__/\n",
        "smart_finqa/__init__.py": "",
        "tests/test_dummy.py": "def test_dummy():\n    assert True\n",
    }
    for rel_path, content in required_files.items():
        _write(project_dir / rel_path, content)

    _write(project_dir / "outputs/run_log.json", "{}\n")
    _write(project_dir / "outputs/finance.db", "db\n")
    _write(project_dir / "result/B001_1.jpg", "image\n")
    _write(project_dir / "样例数据/附件4：问题汇总.xlsx", "sample\n")
    _write(project_dir / "数据/全量数据/附件4：问题汇总.xlsx", "formal\n")
    _write(project_dir / "__pycache__/x.pyc", "cache\n")
    _write(project_dir / ".pytest_cache/README.md", "cache\n")

    proc = subprocess.run(
        ["bash", "package.sh"],
        cwd=project_dir,
        check=False,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr

    zip_files = list(project_dir.glob("demo_project_submit_*.zip"))
    assert len(zip_files) == 1

    with ZipFile(zip_files[0]) as zf:
        names = set(zf.namelist())

    assert "demo_project/README.md" in names
    assert "demo_project/run.sh" in names
    assert "demo_project/run_pipeline.py" in names
    assert "demo_project/smart_finqa/__init__.py" in names
    assert "demo_project/tests/test_dummy.py" in names
    assert "demo_project/outputs/" in names
    assert "demo_project/result/" in names

    assert "demo_project/outputs/run_log.json" not in names
    assert "demo_project/outputs/finance.db" not in names
    assert "demo_project/result/B001_1.jpg" not in names
    assert "demo_project/样例数据/附件4：问题汇总.xlsx" not in names
    assert "demo_project/数据/全量数据/附件4：问题汇总.xlsx" not in names
    assert "demo_project/__pycache__/x.pyc" not in names
    assert "demo_project/.pytest_cache/README.md" not in names
