from pathlib import Path


def test_local_file_input_explicitly_accepts_mov_files():
    index_html = Path("static/index.html").read_text(encoding="utf-8")

    assert ".mov" in index_html
    assert "video/quicktime" in index_html
