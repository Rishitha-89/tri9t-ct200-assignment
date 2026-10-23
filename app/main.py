import os, re, json, uuid, hashlib, difflib
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF 1.x compatibility
    import fitz
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import create_engine, String, Integer, Text, ForeignKey, DateTime, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE / 'ct200.db'}")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase): pass

class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_key: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    nodes: Mapped[list["Node"]] = relationship(back_populates="version_record", cascade="all, delete-orphan")

class Node(Base):
    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    logical_id: Mapped[str] = mapped_column(String, index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), index=True)
    heading: Mapped[str] = mapped_column(String, default="")
    level: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String, default="paragraph")
    body_text: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    page_start: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String, index=True)
    path_key: Mapped[str] = mapped_column(String, index=True)
    version_record: Mapped[DocumentVersion] = relationship(back_populates="nodes")

class Selection(Base):
    __tablename__ = "selections"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    source_json: Mapped[str] = mapped_column(Text)  # immutable node/version/text/hash snapshots

class Generation(Base):
    __tablename__ = "generations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    selection_id: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    provider: Mapped[str] = mapped_column(String)
    output_json: Mapped[str] = mapped_column(Text)
    source_json: Mapped[str] = mapped_column(Text)

Base.metadata.create_all(engine)
app = FastAPI(title="CT-200 Document Traceability API", version="0.1.0")

def sha(s: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", s).strip().encode("utf-8")).hexdigest()

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\uFB01", "fi").replace("\uFB02", "fl")).strip()

# Deliberately scoped to CT-200's numbered headings; uncertain material remains paragraph text.
HEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,5})\.?\s+(.+?)\s*$")
def parse_text_pages(page_texts: list[str]) -> list[dict]:
    records, stack = [], []
    table_lines, table_page, in_error_table = [], None, False

    def flush_table_row(page_no):
        nonlocal table_lines, table_page
        if table_lines:
            parent = stack[-1] if stack else None
            code = table_lines[0]
            records.append({"heading": code, "level": len(stack) + 1, "kind": "table_row",
                            "body_text": clean(" ".join(table_lines[1:])),
                            "parent_temp": parent, "page_start": table_page or page_no})
            table_lines, table_page = [], None

    for page_no, text in enumerate(page_texts, 1):
        if len(text.strip()) < 30:
            continue
        lines = [clean(value) for value in text.splitlines()]
        current_para = []

        def flush_para():
            nonlocal current_para
            if current_para:
                body = clean(" ".join(current_para))
                if body:
                    parent = stack[-1] if stack else None
                    records.append({"heading": "", "level": len(stack) + 1, "kind": "paragraph",
                                    "body_text": body, "parent_temp": parent, "page_start": page_no})
                current_para = []

        for line in lines:
            if in_error_table:
                heading_match = HEADING_RE.match(line)
                if heading_match and len(line) < 115:
                    flush_table_row(page_no)
                    in_error_table = False
                elif re.fullmatch(r"E\d+", line):
                    flush_table_row(page_no)
                    table_lines, table_page = [line], page_no
                    continue
                else:
                    if table_lines and line:
                        table_lines.append(line)
                    continue
            if not line:
                flush_para()
                continue
            if line == "Code":
                flush_para()
                in_error_table = True
                continue
            match = HEADING_RE.match(line)
            is_heading = bool(match and len(line) < 115 and not line.startswith(
                ("1. Normal:", "2. Elevated:", "3. Hypertension", "4. Hypertension", "5. Hypertensive")))
            if is_heading:
                flush_para()
                number, title = match.group(1), match.group(2).strip()
                level = number.count(".") + 1
                while stack and stack[-1]["level"] >= level:
                    stack.pop()
                parent = stack[-1] if stack else None
                node = {"heading": f"{number} {title}", "level": level, "kind": "heading",
                        "body_text": "", "parent_temp": parent, "page_start": page_no, "number": number}
                records.append(node)
                stack.append(node)
            elif re.match(r"^\d+\.\s+", line) or re.match(r"^[A-Z]\d?\s{1,}", line):
                flush_para()
                parent = stack[-1] if stack else None
                records.append({"heading": "", "level": len(stack) + 1, "kind": "list_item",
                                "body_text": line, "parent_temp": parent, "page_start": page_no})
            else:
                current_para.append(line)
        flush_para()
    flush_table_row(max(len(page_texts), 1))
    # Convert temporary parent object references into stable record indexes.
    index = {id(rec): i for i, rec in enumerate(records)}
    for i, rec in enumerate(records):
        p = rec.get("parent_temp")
        rec["parent_index"] = index.get(id(p)) if p else None
        rec.pop("parent_temp", None)
    return records

def parse_pdf(pdf_path: Path) -> list[dict]:
    doc = fitz.open(pdf_path)
    page_texts = []
    for page in doc:
        text = page.get_text("text")
        if len(text.strip()) < 30:
            # OCR is an optional fallback for pages without usable native text.
            try:
                import pytesseract
                from PIL import Image
                import io
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                text = pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png"))))
            except Exception:
                pass
        page_texts.append(text)
    return parse_text_pages(page_texts)

def path_keys(records):
    # Keep paragraph identity independent of its wording so edits can be matched across versions.
    paths = []
    sibling_counts = {}
    for i, r in enumerate(records):
        parent = r["parent_index"]
        prefix = paths[parent] + "/" if parent is not None else ""
        if r["kind"] == "heading":
            # Numbered headings keep their identity when only the title changes.
            base = "heading:" + r.get("number", clean(r["heading"]).casefold())
        else:
            base = r["kind"]
        counter_key = (parent, base)
        sibling_counts[counter_key] = sibling_counts.get(counter_key, 0) + 1
        # Ordinals prevent duplicate headings/items under the same parent from colliding.
        paths.append(prefix + base + f"[#{sibling_counts[counter_key]}]")
    return paths

def ingest_file(path: Path, document_key="ct200"):
    if not path.exists(): raise FileNotFoundError(path)
    records = parse_pdf(path)
    keys = path_keys(records)
    with SessionLocal() as db:
        existing = db.scalars(select(DocumentVersion).where(DocumentVersion.document_key == document_key).order_by(DocumentVersion.version)).all()
        version_num = (existing[-1].version + 1) if existing else 1
        version = DocumentVersion(document_key=document_key, version=version_num, filename=path.name)
        db.add(version); db.flush()
        prev_nodes = db.scalars(select(Node).where(Node.version_id == existing[-1].id)).all() if existing else []
        prev_by_path = {}
        for n in prev_nodes:
            prev_by_path.setdefault(n.path_key, []).append(n)
        ids = []
        for i, r in enumerate(records):
            body = r["body_text"]
            heading = r["heading"]
            content = heading + "\n" + body
            key = keys[i]
            candidates = prev_by_path.get(key, [])
            # Same structural path is the preferred match; duplicate paths are handled by consuming one match.
            matched = candidates.pop(0) if candidates else None
            logical_id = matched.logical_id if matched else str(uuid.uuid4())
            node = Node(logical_id=logical_id, version_id=version.id, heading=heading, level=r["level"],
                        kind=r["kind"], body_text=body, parent_id=None, page_start=r["page_start"],
                        content_hash=sha(content), path_key=key)
            db.add(node); db.flush()
            ids.append(node.id)
        for i, r in enumerate(records):
            if r["parent_index"] is not None:
                node = db.get(Node, ids[i]); node.parent_id = ids[r["parent_index"]]
        db.commit()
        return {"document_key": document_key, "version": version_num, "version_id": version.id,
                "filename": path.name, "nodes_created": len(records),
                "warnings": ["Table structure is represented through extracted text; verify complex tables manually.",
                             "OCR runs only when a page has very little extractable text; requires Tesseract."]}

def node_dict(n, db, include_children=True):
    out = {"id": n.id, "logical_id": n.logical_id, "version": db.get(DocumentVersion, n.version_id).version,
           "heading": n.heading, "level": n.level, "kind": n.kind, "body_text": n.body_text,
           "parent_id": n.parent_id, "page_start": n.page_start, "content_hash": n.content_hash}
    if include_children:
        children = db.scalars(select(Node).where(Node.parent_id == n.id)).all()
        out["children"] = [node_dict(c, db, True) for c in children]
    return out

class IngestRequest(BaseModel):
    filename: str
class SelectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    node_ids: list[str] = Field(min_length=1)
class TestCase(BaseModel):
    title: str = Field(min_length=1)
    source_node_ids: list[str] = Field(min_length=1)
    preconditions: list[str] = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    priority: str = Field(pattern="^(low|medium|high)$")
class GeneratedCases(BaseModel):
    test_cases: list[TestCase] = Field(min_length=3, max_length=5)

def create_cases(source):
    # Explicitly labelled deterministic fallback for local demonstration/testing, not an LLM result.
    cases = []
    for s in source[:3]:
        title = s["heading"] or (s["body_text"][:55] or "Document requirement")
        text = (s["heading"] + " " + s["body_text"]).strip()
        cases.append({"title": f"Verify: {title}", "source_node_ids": [s["node_id"]],
                      "preconditions": ["Use a test device or simulator configured for the cited requirement."],
                      "steps": ["Set up the conditions stated in the source text.", "Execute the relevant operation.", "Record the displayed behavior and measured result."],
                      "expected_result": "Observed behavior conforms to the cited source requirement; do not infer a threshold absent from the source.",
                      "priority": "high" if re.search(r"pressure|error|safety|deflat|alarm", text, re.I) else "medium"})
    while len(cases) < 3:
        cases.append({"title": "Verify source traceability", "source_node_ids": [source[0]["node_id"]],
                      "preconditions": ["The selected source node is loaded."],
                      "steps": ["Retrieve the selected node.", "Compare the displayed text with the pinned snapshot."],
                      "expected_result": "The retrieved source text matches the selection snapshot exactly.", "priority": "medium"})
    return {"test_cases": cases[:5]}

def call_llm(source):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key: return create_cases(source), "mock-fallback"
    import requests
    prompt = ("Generate 3 to 5 concrete QA test case ideas from the source only. Do not invent thresholds. "
              "Return JSON object {test_cases:[{title,source_node_ids,preconditions,steps,expected_result,priority}]}. "
              "priority must be low, medium, or high. Source:\n" + json.dumps(source, ensure_ascii=False))
    endpoint = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    last_error = None
    for _ in range(2):
        try:
            res = requests.post(endpoint, headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages":[{"role":"user","content":prompt}],
                      "response_format":{"type":"json_object"}}, timeout=25)
            res.raise_for_status()
            data = res.json()["choices"][0]["message"]["content"]
            parsed = json.loads(data)
            validated = GeneratedCases.model_validate(parsed)
            allowed_ids = {item["node_id"] for item in source}
            if any(node_id not in allowed_ids for case in validated.test_cases for node_id in case.source_node_ids):
                raise ValueError("Generated case references a node outside the pinned selection")
            return validated.model_dump(), "openai-compatible"
        except Exception as e:
            last_error = str(e)
    raise RuntimeError("LLM output/provider failed validation after 2 attempts: " + str(last_error))

@app.get("/")
def root(): return {"service":"CT-200 Document Traceability API", "docs":"/docs"}

@app.post("/documents/ingest")
def ingest(req: IngestRequest):
    filename = Path(req.filename).name
    path = DATA / filename
    try: return ingest_file(path)
    except FileNotFoundError: raise HTTPException(404, f"PDF not found in data/: {filename}")
    except Exception as e: raise HTTPException(500, f"Ingestion failed: {e}")

@app.get("/documents/{document_key}/versions")
def versions(document_key: str = "ct200"):
    with SessionLocal() as db:
        rows = db.scalars(select(DocumentVersion).where(DocumentVersion.document_key == document_key).order_by(DocumentVersion.version)).all()
        return [{"version":v.version,"filename":v.filename,"created_at":v.created_at.isoformat()} for v in rows]

@app.get("/documents/{document_key}/sections")
def sections(document_key: str, version: int = Query(default=1, ge=1)):
    with SessionLocal() as db:
        v = db.scalar(select(DocumentVersion).where(DocumentVersion.document_key==document_key, DocumentVersion.version==version))
        if not v: raise HTTPException(404, "Document version not found; ingest it first")
        nodes = db.scalars(select(Node).where(Node.version_id==v.id, Node.kind=="heading", Node.parent_id==None)).all()
        return [node_dict(n, db, False) for n in nodes]

@app.get("/nodes/search")
def search(q: str, version: int = Query(default=1, ge=1)):
    with SessionLocal() as db:
        v = db.scalar(select(DocumentVersion).where(DocumentVersion.document_key=="ct200", DocumentVersion.version==version))
        if not v: raise HTTPException(404, "Version not found")
        rows = db.scalars(select(Node).where(Node.version_id==v.id)).all()
        ql=q.casefold()
        return [node_dict(n, db, False) for n in rows if ql in (n.heading+" "+n.body_text).casefold()]

@app.get("/nodes/{node_id}")
def get_node(node_id: str):
    with SessionLocal() as db:
        n=db.get(Node,node_id)
        if not n: raise HTTPException(404,"Node not found")
        return node_dict(n,db,True)

@app.get("/nodes/{node_id}/changes")
def changes(node_id: str, from_version: int=1, to_version: int=2):
    with SessionLocal() as db:
        n=db.get(Node,node_id)
        if not n: raise HTTPException(404,"Node not found")
        current_v=db.get(DocumentVersion,n.version_id)
        versions=db.scalars(select(DocumentVersion).where(DocumentVersion.document_key==current_v.document_key,
            DocumentVersion.version.in_([from_version,to_version]))).all()
        by_num={v.version:v for v in versions}
        if from_version not in by_num or to_version not in by_num: raise HTTPException(404,"Requested version missing")
        old=db.scalar(select(Node).where(Node.version_id==by_num[from_version].id,Node.logical_id==n.logical_id))
        new=db.scalar(select(Node).where(Node.version_id==by_num[to_version].id,Node.logical_id==n.logical_id))
        if not old or not new: return {"status":"removed_or_added","logical_id":n.logical_id,"from_exists":bool(old),"to_exists":bool(new)}
        return {"status":"unchanged" if old.content_hash==new.content_hash else "changed",
                "logical_id":n.logical_id,"from_version":from_version,"to_version":to_version,
                "old_heading":old.heading,"new_heading":new.heading,
                "old_text":old.heading + ("\n" + old.body_text if old.body_text else ""),
                "new_text":new.heading + ("\n" + new.body_text if new.body_text else ""),
                "diff":list(difflib.unified_diff(old.body_text.split(),new.body_text.split(),fromfile="v1",tofile="v2",lineterm=""))[:30]}

@app.get("/documents/{document_key}/changes")
def document_changes(document_key: str, from_version: int = Query(ge=1), to_version: int = Query(ge=1)):
    with SessionLocal() as db:
        versions = db.scalars(select(DocumentVersion).where(
            DocumentVersion.document_key == document_key,
            DocumentVersion.version.in_([from_version, to_version]))).all()
        by_number = {version.version: version for version in versions}
        if from_version not in by_number or to_version not in by_number:
            raise HTTPException(404, "Requested version missing")
        old_nodes = db.scalars(select(Node).where(Node.version_id == by_number[from_version].id)).all()
        new_nodes = db.scalars(select(Node).where(Node.version_id == by_number[to_version].id)).all()
        old_by_logical = {node.logical_id: node for node in old_nodes}
        new_by_logical = {node.logical_id: node for node in new_nodes}
        changes_out = []
        for logical_id in old_by_logical.keys() | new_by_logical.keys():
            old, new = old_by_logical.get(logical_id), new_by_logical.get(logical_id)
            status = "removed" if new is None else "added" if old is None else (
                "unchanged" if old.content_hash == new.content_hash else "changed")
            changes_out.append({"logical_id": logical_id, "status": status,
                                "from_node_id": old.id if old else None,
                                "to_node_id": new.id if new else None,
                                "old_heading": old.heading if old else None,
                                "new_heading": new.heading if new else None,
                                "old_text": (old.heading + "\n" + old.body_text).strip() if old else None,
                                "new_text": (new.heading + "\n" + new.body_text).strip() if new else None})
        return {"document_key": document_key, "from_version": from_version,
                "to_version": to_version, "changes": changes_out,
                "counts": {status: sum(item["status"] == status for item in changes_out)
                           for status in ("unchanged", "changed", "added", "removed")}}

@app.post("/selections")
def make_selection(req: SelectionRequest):
    with SessionLocal() as db:
        snapshots=[]
        for nid in req.node_ids:
            n=db.get(Node,nid)
            if not n: raise HTTPException(404,f"Unknown node ID: {nid}")
            v=db.get(DocumentVersion,n.version_id)
            snapshots.append({"node_id":n.id,"logical_id":n.logical_id,"version":v.version,
                "document_key":v.document_key,
                "heading":n.heading,"body_text":n.body_text,"content_hash":n.content_hash,"page_start":n.page_start})
        pins = {(item["document_key"], item["version"]) for item in snapshots}
        if len(pins) != 1:
            raise HTTPException(400, "A selection must be pinned to one document version")
        sel=Selection(name=req.name,source_json=json.dumps(snapshots,ensure_ascii=False))
        db.add(sel);db.commit()
        return {"selection_id":sel.id,"name":sel.name,"pinned_sources":snapshots}

@app.get("/selections/{selection_id}")
def get_selection(selection_id: str):
    with SessionLocal() as db:
        s=db.get(Selection,selection_id)
        if not s: raise HTTPException(404,"Selection not found")
        return {"selection_id":s.id,"name":s.name,"created_at":s.created_at.isoformat(),"sources":json.loads(s.source_json)}

@app.post("/selections/{selection_id}/generate")
def generate(selection_id: str):
    with SessionLocal() as db:
        s=db.get(Selection,selection_id)
        if not s: raise HTTPException(404,"Selection not found")
        source=json.loads(s.source_json)
        try: output,provider=call_llm(source)
        except Exception as e: raise HTTPException(502,str(e))
        # Validate mock and provider output alike before persistence.
        try: output=GeneratedCases.model_validate(output).model_dump()
        except ValidationError as e: raise HTTPException(502,f"Generated output invalid: {e}")
        g=Generation(selection_id=s.id,provider=provider,output_json=json.dumps(output,ensure_ascii=False),source_json=s.source_json)
        db.add(g);db.commit()
        return {"generation_id":g.id,"selection_id":s.id,"provider":provider,"test_cases":output["test_cases"],
                "notice":"mock-fallback means no real LLM provider was called" if provider=="mock-fallback" else None}

def generation_view(g, db):
    sources=json.loads(g.source_json)
    statuses=[]
    for src in sources:
        document_key = src.get("document_key", "ct200")
        latest=db.scalar(select(DocumentVersion).where(DocumentVersion.document_key==document_key).order_by(DocumentVersion.version.desc()))
        current=None
        if latest:
            current=db.scalar(select(Node).where(Node.version_id==latest.id,Node.logical_id==src["logical_id"]))
        if not current: status="removed_or_unmatched"
        elif current.content_hash==src["content_hash"]: status="unchanged"
        else: status="changed"
        statuses.append({"node_id":src["node_id"],"logical_id":src["logical_id"],"document_key":document_key,"source_version":src["version"],
                         "latest_version":latest.version if latest else None,"status":status,
                 "original_hash":src["content_hash"],"current_hash":current.content_hash if current else None})
    stale=any(x["status"]!="unchanged" for x in statuses)
    return {"generation_id":g.id,"selection_id":g.selection_id,"created_at":g.created_at.isoformat(),
            "provider":g.provider,"test_cases":json.loads(g.output_json)["test_cases"],
            "sources":sources,
            "staleness":{"is_stale":stale,"sources":statuses,
             "meaning":"Text-hash comparison only; changed wording does not establish clinical impact."}}

@app.get("/generations/{generation_id}")
def get_generation(generation_id: str):
    with SessionLocal() as db:
        g=db.get(Generation,generation_id)
        if not g: raise HTTPException(404,"Generation not found")
        return generation_view(g,db)

@app.get("/generations")
def list_generations(selection_id: Optional[str]=None, node_id: Optional[str]=None):
    with SessionLocal() as db:
        rows=db.scalars(select(Generation).where(Generation.selection_id==selection_id) if selection_id else select(Generation)).all()
        result=[]
        for g in rows:
            sources=json.loads(g.source_json)
            if node_id and not any(s["node_id"]==node_id or s["logical_id"]==node_id for s in sources): continue
            result.append(generation_view(g,db))
        return result
