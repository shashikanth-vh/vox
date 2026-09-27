"""Sarvam Doc AI results: both the Markdown-per-page shape and the layout-block shape."""

from app.knowledge import sarvam_client


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _fetch(monkeypatch, body):
    monkeypatch.setattr(sarvam_client.requests, "get", lambda *a, **k: _Resp(body))
    pages, _usage = sarvam_client._fetch_results("https://sarvam.test", "job-1")
    return pages


def test_block_pages_become_markdown_in_reading_order(monkeypatch):
    pages = _fetch(monkeypatch, {"documents": [{"pages": [{
        "page_num": 2,
        "blocks": [
            {"text": "Net debt: Rs 42.0 Cr", "layout_tag": "paragraph", "reading_order": 3},
            {"text": "FACILITY SUMMARY", "layout_tag": "headline", "reading_order": 1},
            {"text": "Revenue FY25: Rs 120.5 Cr", "layout_tag": "paragraph", "reading_order": 2},
            {"text": "  ", "layout_tag": "paragraph", "reading_order": 4},
        ],
    }]}]})
    assert [p.page_number for p in pages] == [2]
    assert pages[0].markdown == ("## FACILITY SUMMARY\n\nRevenue FY25: Rs 120.5 Cr\n\n"
                                 "Net debt: Rs 42.0 Cr")


def test_markdown_content_pages_still_read(monkeypatch):
    pages = _fetch(monkeypatch, {"documents": [{"pages": [
        {"page_number": 2, "content": " second "},
        {"page_number": 1, "content": "first"},
    ]}]})
    assert [(p.page_number, p.markdown) for p in pages] == [(1, "first"), (2, "second")]


def test_digitise_requests_an_output_format_sarvam_accepts():
    import inspect
    default = inspect.signature(sarvam_client.digitise_document).parameters["output_format"].default
    assert default in {"html", "md", "json"}
