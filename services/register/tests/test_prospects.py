"""The prospect universe — import engine, merge policy, RBAC split, promotion, round trip.

The workbook fixtures reproduce the launch lists' real pathologies: per-file
header spellings, Google-Sheets formula remnants as literal text, an in-file
duplicate (the Tds-G case), the same company on two lists (the Neuron case),
raw sheets sitting next to the curated "Eligible" cut, and multi-valued
contact cells."""
from __future__ import annotations

import io
from collections.abc import AsyncIterator

import openpyxl
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.security import clear_tenant_cache
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.main import create_app
from app.seed.loader import ensure_tenant

ADMIN = {"X-User-Email": "admin@evamfinance.com", "X-User-Roles": "Admin"}
RM = {"X-User-Email": "rm@evamfinance.com", "X-User-Roles": "BDRM"}


def _book(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    default = wb.active
    for i, (title, rows) in enumerate(sheets.items()):
        ws = default if i == 0 else wb.create_sheet()
        ws.title = title
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _solar_file() -> bytes:
    """Header spellings from the real Solar list; a RAW sheet the chooser must skip."""
    return _book({
        "Eligible SOLAR Data Sectorwise": [
            ["SNo.", "Company Name", "Domain Name", "Overview", "CIN/LLPIN",
             "EVAM SECTOR", "Company Emails", "Company Phone Numbers",
             "Annual Revenue (INR Crores)", "Annual Net Profit (INR Cr)",
             "Annual EBITDA (INR Cr)", "Total Funding (INR Cr)"],
            [1, "InSolare Energy", "insolare.com", "Provider of renewable energy",
             "U45206GJ2008PLC155375", "EPC", None, None, 441.4489, 17.4238,
             37.3952, 175.913964],
            [2, "Neuron Energy", "neuronenergy.in", "Lithium-ion platform",
             "U31909MH2022PTC388295", "OEM", "sales@neuronenergy.in,\ninfo@neuronenergy.in",
             "+91-18001022139", 106.5357, 0.7135, 6.6938, 86.454234],
            # A formula remnant where the sector should be — scrubbed, row survives.
            [3, "Purshotam Group", "purshotamgroup.com", "Solar module manufacture",
             "U45400DL2020PTC368323", "=IFERROR(__xludf.DUMMYFUNCTION(x))",
             "info@purshotamgroup.com", "+91-1147049127", 1722.0, 43.26, 90.95, 0.0],
            # No company name at all — skipped, reported.
            [4, None, "ghost.example", "orphan row", None, "EPC", None, None,
             None, None, None, None],
        ],
        "SOLAR RAW DATA": [
            ["SNo.", "Company Name", "Domain Name"],
            [1, "Should Not Be Read", "raw.example"],
        ],
    })


def _ess_file() -> bytes:
    """ESS spellings; Neuron repeats (cross-file merge), Tds-G twice (in-file dupe)."""
    return _book({
        "Eligible ESS Data Sectorwise": [
            ["SNo.", "Company Name", "Domain Name", "CIN", "Overview", "Founded Year",
             "EVAM SECTOR", "Company Phone Numbers", "Company Emails",
             "Annual Revenue (INR Cr)", "Annual Net Profit (INR Cr)",
             "Annual EBITDA (INR Cr)"],
            [1, "Neuron Energy", "neuronenergy.in", "U31909MH2022PTC388295",
             "Lithium-ion platform", 2018, "Battery Cells & Core Chemistry",
             "+91-18001022139", "sales@neuronenergy.in", 106.5357, 0.7135, 6.6938],
            [2, "Tds-G", "tds-g.co.in", "U29309GJ2017FTC098669",
             "Manufacturer of lithium-ion", 2019, "Battery Cells & Core Chemistry",
             None, None, 1506.214, -48.347, 357.084],
            [3, "Tds-G", "tds-g.co.in", "U29309GJ2017FTC098669",
             "Manufacturer of lithium-ion", 2019, "Battery Cells & Core Chemistry",
             None, None, 1506.214, -48.347, 357.084],
            [4, "Statcon Energiaa", "energiaa.in", "U31100DL1991PTC045130",
             "Vertically integrated company", 1991, "ESS Integration",
             "+91-1203819600", "info@energiaa.in", 204.0425, 7.2971, 13.4238],
        ],
    })


def _upload(name: str, content: bytes) -> tuple:
    return ("files", (name, content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))


@pytest_asyncio.fixture
async def reg() -> AsyncIterator[AsyncClient]:
    clear_tenant_cache()
    s = get_settings()
    init_engine(s)
    sm = get_sessionmaker()
    async with sm() as session:
        await ensure_tenant(session, "EVAM", "Evam Finance")
        await session.commit()
    app = create_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"X-API-Key": get_settings().all_api_keys()[0],
                                        "X-Tenant": "EVAM"}) as c:
            yield c
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(
                text("TRUNCATE prospects, leads, entities CASCADE"))
            await session.commit()
        await dispose_engine()


async def test_the_engine_reads_the_real_lists_shapes(reg: AsyncClient):
    """Preview: sheet choice, scrubbing, in-file dupes, cross-file merge — and
    NOTHING written."""
    r = await reg.post("/v1/prospects/import?mode=preview", headers=ADMIN,
                       files=[_upload("EVAM Solar Companies List.xlsx", _solar_file()),
                              _upload("EVAM ESS Companies List.xlsx", _ess_file())])
    assert r.status_code == 200, r.text
    body = r.json()
    counts = body["counts"]
    # Solar: InSolare, Neuron, Purshotam (ghost row skipped). ESS: Neuron (merges
    # cross-file), Tds-G (once — second row is the in-file duplicate), Statcon.
    assert counts["new"] == 5
    assert counts["in_file_duplicates"] == 2   # Tds-G repeat + Neuron's ESS row
    assert counts["skipped"] == 1
    assert counts["merged"] == 0 and counts["conflicts"] == 0
    files = {f["file"]: f for f in body["files"]}
    assert files["EVAM Solar Companies List.xlsx"]["vertical"] == "Solar"
    assert files["EVAM Solar Companies List.xlsx"]["sheet"].startswith("Eligible")
    assert files["EVAM ESS Companies List.xlsx"]["vertical"] == "ESS"
    neuron = next(c for c in body["new_sample"] if c["name"] == "Neuron Energy")
    assert sorted(neuron["verticals"]) == ["ESS", "Solar"]
    assert "OEM" in neuron["sub_sectors"]
    assert "Battery Cells & Core Chemistry" in neuron["sub_sectors"]
    # Purshotam's remnant sector was scrubbed, not stored as a formula string.
    purshotam = next(c for c in body["new_sample"] if c["name"] == "Purshotam Group")
    assert purshotam["sub_sectors"] == []
    # Preview wrote nothing.
    listing = await reg.get("/v1/prospects", headers=ADMIN)
    assert listing.json()["items"] == []


async def test_apply_is_idempotent_and_merge_never_overwrites(reg: AsyncClient):
    r = await reg.post("/v1/prospects/import?mode=apply", headers=ADMIN,
                       files=[_upload("EVAM Solar Companies List.xlsx", _solar_file())])
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 3

    listing = (await reg.get("/v1/prospects?with_total=true", headers=ADMIN)).json()
    assert listing["total"] == 3
    by_name = {p["name"]: p for p in listing["items"]}
    assert by_name["Neuron Energy"]["prospect_no"].startswith("P-")
    assert by_name["Neuron Energy"]["status"] == "uncontacted"

    # The desk edits the register: a revenue correction by hand.
    neuron_id = by_name["Neuron Energy"]["id"]
    patched = await reg.patch(f"/v1/prospects/{neuron_id}", headers=ADMIN,
                              json={"revenue_cr": 110.0})
    assert patched.status_code == 200, patched.text

    # Re-import: the ESS list repeats Neuron with the FILE's revenue. The import
    # adds the ESS tags and reports the revenue conflict — it never overwrites.
    r2 = await reg.post("/v1/prospects/import?mode=apply", headers=ADMIN,
                        files=[_upload("EVAM ESS Companies List.xlsx", _ess_file())])
    body = r2.json()
    assert body["created"] == 2               # Tds-G, Statcon
    assert body["counts"]["merged"] == 1      # Neuron gained tags + founded_year
    assert any(c["field"] == "revenue_cr" for c in body["conflicts"])
    neuron = (await reg.get(f"/v1/prospects/{neuron_id}", headers=ADMIN)).json()
    assert float(neuron["revenue_cr"]) == 110.0          # the human's value stood
    assert sorted(neuron["verticals"]) == ["ESS", "Solar"]
    assert neuron["founded_year"] == 2018                # blank was filled

    # Same files again: fully idempotent — nothing new, nothing to merge.
    r3 = await reg.post("/v1/prospects/import?mode=apply", headers=ADMIN,
                        files=[_upload("EVAM Solar Companies List.xlsx", _solar_file()),
                               _upload("EVAM ESS Companies List.xlsx", _ess_file())])
    assert r3.json()["created"] == 0
    # The one standing difference (the hand-edited revenue) keeps being REPORTED,
    # never resolved.
    assert all(c["field"] == "revenue_cr" for c in r3.json()["conflicts"])


async def test_the_desk_works_the_row_the_curators_own_the_data(reg: AsyncClient):
    await reg.post("/v1/prospects/import?mode=apply", headers=ADMIN,
                   files=[_upload("EVAM Solar Companies List.xlsx", _solar_file())])
    pid = (await reg.get("/v1/prospects", headers=ADMIN)).json()["items"][0]["id"]

    # An RM works the row: status + remarks — allowed.
    ok = await reg.patch(f"/v1/prospects/{pid}", headers=RM,
                         json={"status": "contacted",
                               "remarks": "Met at REI Expo; open to a WC discussion."})
    assert ok.status_code == 200, ok.text
    # The curated master data is not theirs: refused, loudly.
    no = await reg.patch(f"/v1/prospects/{pid}", headers=RM,
                         json={"cin": "U00000MH2020PTC000000"})
    assert no.status_code == 403
    # Nor is the import door.
    no_import = await reg.post("/v1/prospects/import?mode=preview", headers=RM,
                               files=[_upload("EVAM Solar Companies List.xlsx",
                                              _solar_file())])
    assert no_import.status_code == 403
    # The curator may do both.
    yes = await reg.patch(f"/v1/prospects/{pid}", headers=ADMIN,
                          json={"cin": "U00000MH2020PTC000001"})
    assert yes.status_code == 200, yes.text


async def test_create_lead_is_repeatable_and_births_the_master(reg: AsyncClient):
    await reg.post("/v1/prospects/import?mode=apply", headers=ADMIN,
                   files=[_upload("EVAM Solar Companies List.xlsx", _solar_file())])
    items = (await reg.get("/v1/prospects", headers=ADMIN)).json()["items"]
    insolare = next(p for p in items if p["name"] == "InSolare Energy")

    r1 = await reg.post(f"/v1/prospects/{insolare['id']}/create-lead", headers=RM,
                        json={"rm": "Pallavi Patel", "notes": "WC discussion"})
    assert r1.status_code == 200, r1.text
    first = r1.json()
    assert first["lead_no"].startswith("LD-")
    assert first["company_outcome"] == "created"      # the master was born here
    assert first["entity_id"] is not None
    assert first["lead_count"] == 1

    # A second ask months later: SAME company, SAME master, one more lead.
    r2 = await reg.post(f"/v1/prospects/{insolare['id']}/create-lead", headers=RM,
                        json={"notes": "Capex line for the new fab"})
    second = r2.json()
    assert second["lead_count"] == 2
    assert second["company_outcome"] == "linked"
    assert second["entity_id"] == first["entity_id"]

    p = (await reg.get(f"/v1/prospects/{insolare['id']}", headers=ADMIN)).json()
    assert p["status"] == "lead_created"
    assert len(p["lead_ids"]) == 2
    lead = (await reg.get(f"/v1/leads/{first['lead_id']}", headers=ADMIN)).json()
    assert lead["company"] == "InSolare Energy"
    assert lead["source"] == "Prospecting"
    assert lead["entity_id"] == first["entity_id"]


async def test_chips_filter_facets_and_export_round_trip(reg: AsyncClient):
    await reg.post("/v1/prospects/import?mode=apply", headers=ADMIN,
                   files=[_upload("EVAM Solar Companies List.xlsx", _solar_file()),
                          _upload("EVAM ESS Companies List.xlsx", _ess_file())])

    # Chip membership filters (multi-select = ANY of).
    solar = (await reg.get("/v1/prospects?verticals=Solar", headers=ADMIN)).json()
    assert {p["name"] for p in solar["items"]} == {"InSolare Energy", "Neuron Energy",
                                                   "Purshotam Group"}
    either = (await reg.get("/v1/prospects?verticals=Solar,ESS", headers=ADMIN)).json()
    assert len(either["items"]) == 5
    oem = (await reg.get("/v1/prospects?sub_sectors=OEM", headers=ADMIN)).json()
    assert {p["name"] for p in oem["items"]} == {"Neuron Energy"}

    facets = (await reg.get("/v1/prospects/facets", headers=ADMIN)).json()
    assert facets["total"] == 5
    assert facets["verticals"]["Solar"] == 3 and facets["verticals"]["ESS"] == 3
    assert facets["statuses"]["uncontacted"] == 5
    scoped = (await reg.get("/v1/prospects/facets?verticals=ESS",
                            headers=ADMIN)).json()
    assert "EPC" not in scoped["sub_sectors"]

    # Export → re-import: the round trip is the contract. Nothing new, nothing
    # merged, no conflicts — byte-different file, identical universe.
    exported = await reg.get("/v1/prospects/export-xlsx", headers=ADMIN)
    assert exported.status_code == 200
    again = await reg.post("/v1/prospects/import?mode=preview", headers=ADMIN,
                           files=[_upload("prism-prospects.xlsx", exported.content)])
    body = again.json()
    assert body["counts"]["new"] == 0
    assert body["counts"]["merged"] == 0
    assert body["counts"]["conflicts"] == 0


async def test_manual_create_and_master_edit_carry_the_imports_invariants(reg: AsyncClient):
    """The Add-prospect form's contract: RBAC'd create, register-assigned P-code,
    a canonical name_key that keeps the row visible to the import's dedupe, a
    CIN refusal that names the holder, and a rename that moves the key along."""
    # Creation is curated-data territory: the desk is refused, Admin lands.
    denied = await reg.post("/v1/prospects", headers=RM,
                            json={"name": "Acme Solar Private Limited"})
    assert denied.status_code == 403
    r = await reg.post("/v1/prospects", headers=ADMIN, json={
        "name": "Acme Solar Private Limited", "domain": "acmesolar.in",
        "cin": "U40100MH2015PTC999999", "verticals": ["Solar"],
        "sub_sectors": ["EPC"], "state": "Maharashtra", "revenue_cr": 12.5})
    assert r.status_code == 201, r.text
    made = r.json()
    assert made["prospect_no"].startswith("P-")

    # The dedupe key was settled server-side, not left NULL like a naive create
    # would — so the next Excel drop MERGES into this row instead of forking it.
    sm = get_sessionmaker()
    from app.models import Prospect
    async with sm() as session:
        key = (await session.execute(
            select(Prospect.name_key).where(Prospect.id == made["id"]))).scalar()
    assert key  # canonical, non-empty
    book = _book({"Eligible Solar": [
        ["Company Name", "Domain Name", "EVAM SECTOR",
         "Annual Revenue (INR Cr)", "City"],
        ["Acme Solar Pvt Ltd", "acmesolar.in", "Developer", 12.5, "Pune"],
    ]})
    preview = (await reg.post("/v1/prospects/import?mode=preview", headers=ADMIN,
                              files=[_upload("solar.xlsx", book)])).json()
    assert preview["counts"]["new"] == 0
    assert preview["counts"]["merged"] == 1

    # A second row for the same CIN is refused BY NAME, not with a raw 500.
    dup = await reg.post("/v1/prospects", headers=ADMIN, json={
        "name": "Acme Solar (duplicate)", "cin": "U40100MH2015PTC999999"})
    assert dup.status_code == 422
    assert made["prospect_no"] in dup.json()["error"]["detail"]

    # A rename moves the canonical key with it; delete is the standard soft-delete.
    ren = await reg.patch(f"/v1/prospects/{made['id']}", headers=ADMIN,
                          json={"name": "Zenith Solar Private Limited"})
    assert ren.status_code == 200, ren.text
    async with sm() as session:
        key2 = (await session.execute(
            select(Prospect.name_key).where(Prospect.id == made["id"]))).scalar()
    assert key2 and key2 != key
    gone = await reg.delete(f"/v1/prospects/{made['id']}", headers=ADMIN)
    assert gone.status_code == 204
    listing = (await reg.get("/v1/prospects?with_total=true", headers=ADMIN)).json()
    assert all(p["id"] != made["id"] for p in listing["items"])
