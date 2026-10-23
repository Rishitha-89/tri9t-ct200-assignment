import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    main.Base.metadata.create_all(test_engine)
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=test_engine, expire_on_commit=False))
    yield TestClient(main.app)
    test_engine.dispose()


def test_hash_normalizes_whitespace():
    assert main.sha("battery   life") == main.sha("battery\nlife")


def test_changed_requirement_changes_hash():
    assert main.sha("Auto-deflates within 2 seconds") != main.sha("Auto-deflates within 1.5 seconds")


def test_generated_cases_schema_rejects_invalid_priority():
    with pytest.raises(Exception):
        main.GeneratedCases.model_validate({"test_cases": [{
            "title": "Case", "source_node_ids": ["n1"], "preconditions": ["Ready"],
            "steps": ["Run"], "expected_result": "Matches source", "priority": "urgent"
        }] * 3})


def test_mock_generation_has_at_least_three_cases():
    source = [{"node_id": "n1", "heading": "4.1 Overpressure Protection", "body_text": "vent within 2 seconds"}]
    result = main.create_cases(source)
    assert 3 <= len(result["test_cases"]) <= 5


def test_skipped_heading_level_attaches_to_nearest_existing_parent():
    records = main.parse_text_pages(["1 Parent\n1.1.1 Deep subsection\nA paragraph with enough text to retain it."])
    assert records[1]["parent_index"] == 0
    assert records[1]["level"] == 3


def test_out_of_order_numbering_is_preserved_as_siblings():
    records = main.parse_text_pages(["3.4 Later-numbered heading\n3.3 Earlier-numbered heading"])
    assert [record["heading"] for record in records] == ["3.4 Later-numbered heading", "3.3 Earlier-numbered heading"]
    assert records[0]["parent_index"] is None
    assert records[1]["parent_index"] is None


def test_duplicate_headings_have_distinct_paths():
    records = main.parse_text_pages(["2 Repeated heading\n2 Repeated heading"])
    keys = main.path_keys(records)
    assert len(keys) == 2
    assert keys[0] != keys[1]


def test_heading_stack_survives_page_break():
    records = main.parse_text_pages(["4 Parent section\nA first paragraph with enough words to remain in the parse.",
                                     "A continuation paragraph on the next page with enough words to remain in the parse."])
    assert records[1]["parent_index"] == 0
    assert records[2]["parent_index"] == 0
    assert records[2]["page_start"] == 2


def test_error_code_table_rows_are_individual_nodes():
    pdf = Path(__file__).resolve().parents[1] / "data" / "ct200_manual_v2.pdf"
    rows = [record for record in main.parse_pdf(pdf) if record["kind"] == "table_row"]
    e3 = next(record for record in rows if record["heading"] == "E3")
    e6 = next(record for record in rows if record["heading"] == "E6")
    assert "1.5 seconds" in e3["body_text"]
    assert "Bluetooth sync failure" in e6["body_text"]
    assert e3["page_start"] == 4


def test_actual_pdf_versioning_pinning_and_staleness(client):
    v1 = client.post("/documents/ingest", json={"filename": "ct200_manual.pdf"})
    assert v1.status_code == 200, v1.text
    assert v1.json()["version"] == 1
    sections = client.get("/documents/ct200/sections", params={"version": 1}).json()
    assert sections and all(section["parent_id"] is None for section in sections)
    assert client.get(f"/nodes/{sections[0]['id']}").status_code == 200

    v1_matches = client.get("/nodes/search", params={"q": "300 measurement cycles", "version": 1}).json()
    source = next(node for node in v1_matches if "300 measurement cycles" in node["body_text"])
    original_text = source["body_text"]
    selection = client.post("/selections", json={"name": "Battery requirement", "node_ids": [source["id"]]}).json()
    generated = client.post(f"/selections/{selection['selection_id']}/generate")
    assert generated.status_code == 200, generated.text
    generation_id = generated.json()["generation_id"]
    assert generated.json()["provider"] == "mock-fallback"
    second_generation = client.post(f"/selections/{selection['selection_id']}/generate").json()
    assert second_generation["generation_id"] != generation_id

    v2 = client.post("/documents/ingest", json={"filename": "ct200_manual_v2.pdf"})
    assert v2.status_code == 200, v2.text
    assert v2.json()["version"] == 2

    pinned = client.get(f"/selections/{selection['selection_id']}").json()
    assert pinned["sources"][0]["body_text"] == original_text
    assert pinned["sources"][0]["version"] == 1

    latest_nodes = client.get("/nodes/search", params={"q": "", "version": 2}).json()
    mixed_selection = client.post("/selections", json={"name": "Mixed", "node_ids": [source["id"], latest_nodes[0]["id"]]})
    assert mixed_selection.status_code == 400
    latest_text = "\n".join(node["heading"] + " " + node["body_text"] for node in latest_nodes)
    for phrase in ("250 measurement cycles", "10%", "30 mmHg", "1.5 seconds", "E6", "5.3 Data Export"):
        assert phrase.casefold() in latest_text.casefold(), phrase
    assert "2 seconds" in latest_text
    parsed_v2 = main.parse_pdf(Path(__file__).resolve().parents[1] / "data" / "ct200_manual_v2.pdf")
    assert any("within 2 seconds" in item["body_text"] for item in parsed_v2 if item["kind"] == "paragraph")
    assert any(item["heading"] == "E3" and "1.5 seconds" in item["body_text"] for item in parsed_v2)

    changes = client.get("/documents/ct200/changes", params={"from_version": 1, "to_version": 2}).json()
    assert changes["counts"]["changed"] > 0
    assert changes["counts"]["added"] > 0
    battery_change = next(item for item in changes["changes"]
                          if item["old_text"] and "300 measurement cycles" in item["old_text"])
    assert battery_change["status"] == "changed"
    assert "250 measurement cycles" in battery_change["new_text"]
    e3_change = next(item for item in changes["changes"] if item["old_heading"] == "E3")
    assert e3_change["status"] == "changed"
    assert "2 seconds" in e3_change["old_text"] and "1.5 seconds" in e3_change["new_text"]
    e6_change = next(item for item in changes["changes"] if item["new_heading"] == "E6")
    assert e6_change["status"] == "added"
    data_export = next(item for item in changes["changes"]
                       if item["new_heading"] == "5.3 Data Export")
    assert data_export["status"] == "added"
    for before, after in (("15%", "10%"), ("40 mmHg", "30 mmHg")):
        changed = next(item for item in changes["changes"]
                       if item["old_text"] and before in item["old_text"])
        assert changed["status"] == "changed"
        assert after in changed["new_text"]
    assert client.get("/generations", params={"selection_id": selection["selection_id"]}).status_code == 200
    assert client.get("/generations", params={"node_id": source["id"]}).status_code == 200

    generation = client.get(f"/generations/{generation_id}").json()
    assert generation["staleness"]["is_stale"] is True
    assert generation["staleness"]["sources"][0]["status"] == "changed"
    assert generation["sources"][0]["body_text"] == original_text
    assert "clinical impact" in generation["staleness"]["meaning"]

    with main.SessionLocal() as db:
        latest_version = db.scalar(main.select(main.DocumentVersion).where(
            main.DocumentVersion.document_key == "ct200",
            main.DocumentVersion.version == 2))
        latest_battery = db.scalar(main.select(main.Node).where(
            main.Node.version_id == latest_version.id,
            main.Node.logical_id == source["logical_id"]))
        db.delete(latest_battery)
        db.commit()
    removed_generation = client.get(f"/generations/{second_generation['generation_id']}").json()
    assert removed_generation["staleness"]["is_stale"] is True
    assert removed_generation["staleness"]["sources"][0]["status"] == "removed_or_unmatched"


def test_llm_malformed_response_retries_and_validates_source_refs(monkeypatch):
    import json
    import requests

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    source = [{"node_id": "source-1", "heading": "Requirement", "body_text": "The device shows E1."}]
    payload = {"test_cases": [{"title": f"Case {index}", "source_node_ids": ["source-1"],
                               "preconditions": ["Ready"], "steps": ["Run test"],
                               "expected_result": "Behavior matches cited source.", "priority": "medium"}
                              for index in range(3)]}

    class Response:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": self.content}}]}

    responses = iter([Response("not json"), Response(json.dumps(payload))])
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(requests, "post", fake_post)
    result, provider = main.call_llm(source)
    assert len(calls) == 2
    assert provider == "openai-compatible"
    assert len(result["test_cases"]) == 3