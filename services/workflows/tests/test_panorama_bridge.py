"""The zip half of the Company-360 indexing bridge: what travels, what is
refused, and that every refusal carries its reason."""
from __future__ import annotations

import io
import zipfile

from app.panorama_bridge import unpack_zip


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_readable_members_travel_and_paths_are_flattened():
    blob = _zip({
        "folder/CIBIL Report.pdf": b"%PDF-1.4 fake",
        "Aadhar front.jpg": b"\xff\xd8\xff fake",
        # A crafted path must never travel anywhere but its basename.
        "../../etc/passwd.pdf": b"%PDF evil",
    })
    files, skipped = unpack_zip(blob, "CIBIL Consent.zip")
    assert [(n, s) for n, _, s in files] == [
        ("CIBIL Report.pdf", ".pdf"), ("Aadhar front.jpg", ".jpg"),
        ("passwd.pdf", ".pdf")]
    assert skipped == []


def test_unreadable_members_are_skipped_with_reasons():
    blob = _zip({"MOA.docx": b"PK docx", "notes.txt": b"hello",
                 "scan.pdf": b"%PDF ok"})
    files, skipped = unpack_zip(blob, "MOA pack.zip")
    assert [n for n, _, _ in files] == ["scan.pdf"]
    reasons = {s["file"]: s["reason"] for s in skipped}
    assert "MOA pack.zip/MOA.docx" in reasons
    assert ".docx" in reasons["MOA pack.zip/MOA.docx"]
    assert "MOA pack.zip/notes.txt" in reasons


def test_a_nested_zip_stays_closed_and_garbage_is_named():
    blob = _zip({"inner.zip": _zip({"deep.pdf": b"%PDF"})})
    files, skipped = unpack_zip(blob, "outer.zip")
    assert files == []
    assert skipped[0]["file"] == "outer.zip/inner.zip"

    files, skipped = unpack_zip(b"this is not a zip", "broken.zip")
    assert files == []
    assert skipped == [{"file": "broken.zip",
                        "reason": "not a readable zip archive"}]


def test_member_count_guard_refuses_a_bomb_shape():
    blob = _zip({f"p{i}.pdf": b"%PDF" for i in range(51)})
    files, skipped = unpack_zip(blob, "bomb.zip")
    assert files == []
    assert "51 files" in skipped[0]["reason"]


def test_already_indexed_matches_only_this_company_prefix():
    from app.panorama_bridge import already_indexed

    listing = {"items": [
        {"id": "a1", "filename": "Aadhi Vishwa Energy — UDYAM.pdf"},
        {"id": "a2", "filename": "Aadhi Vishwa Energy — kyc.zip/pan.pdf"},
        {"id": "b1", "filename": "Aadhi Vishwa Energy LLP — UDYAM.pdf"},
        {"id": "c1", "name": "Other Co — UDYAM.pdf"},
        {"filename": "Aadhi Vishwa Energy — no-id.pdf"},
        "junk",
    ]}
    got = already_indexed(listing, "Aadhi Vishwa Energy")
    assert got == {"UDYAM.pdf": "a1", "kyc.zip/pan.pdf": "a2"}
    assert already_indexed({}, "Aadhi Vishwa Energy") == {}
    assert already_indexed({"items": None}, "X") == {}
