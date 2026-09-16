from pathlib import Path

import pytest

from pe_review_agent.config import ReviewSettings
from pe_review_agent.repos.manager import _parse_changed_lines
from pe_review_agent.repos.tools import RepositoryToolExecutor


def test_parse_changed_lines_tracks_only_revision_additions() -> None:
    diff = """\
diff --git a/fw/a.c b/fw/a.c
--- a/fw/a.c
+++ b/fw/a.c
@@ -2,2 +2,3 @@
 same
-old
+new
+extra
"""
    lines = _parse_changed_lines(diff)
    assert [(line.path, line.line, line.text) for line in lines] == [
        ("fw/a.c", 3, "new"),
        ("fw/a.c", 4, "extra"),
    ]


def test_repository_tools_block_path_escape(tmp_path: Path) -> None:
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings())
    with pytest.raises(ValueError, match="escapes workspace"):
        executor.read_file("../secret")


def test_repository_read_file_is_line_numbered(tmp_path: Path) -> None:
    target = tmp_path / "fw.c"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    executor = RepositoryToolExecutor(tmp_path, ReviewSettings())
    assert executor.read_file("fw.c", start_line=2, end_line=3) == "2: two\n3: three"
