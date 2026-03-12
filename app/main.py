from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services import (
    ExtractedMaterial,
    GenerationResult,
    build_report_content,
    extract_material,
    export_docx,
    recommend_laws,
)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
EXPORT_DIR = BASE_DIR / "data" / "exports"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="案件审查报告智能生成应用", version="0.1.0")


class TemplatePayload(BaseModel):
    case_type: str
    name: str
    sections: List[str]
    required_fields: List[str] = Field(default_factory=list)
    optional_phrases: List[str] = Field(default_factory=list)
    layout_rules: Dict[str, str] = Field(default_factory=dict)


class ReferencePayload(BaseModel):
    title: str
    tags: Dict[str, str]
    content: str


class LawItem(BaseModel):
    title: str
    article: str
    keywords: List[str]
    source: str
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None


class GeneratePayload(BaseModel):
    case_id: str
    case_type: str
    template_id: str
    reference_ids: List[str]
    law_item_ids: List[str]


class CorrectionPayload(BaseModel):
    extracted: ExtractedMaterial


DB = {
    "templates": {},
    "references": {},
    "laws": {},
    "cases": {},
    "reports": {},
    "logs": [],
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/templates")
def create_template(payload: TemplatePayload) -> Dict:
    template_id = _new_id("tpl")
    data = payload.model_dump()
    data.update({"id": template_id, "active": True, "version": 1})
    DB["templates"][template_id] = data
    return data


@app.get("/templates")
def list_templates(case_type: Optional[str] = None) -> List[Dict]:
    items = list(DB["templates"].values())
    if case_type:
        items = [i for i in items if i["case_type"] == case_type]
    return items


@app.post("/references")
def create_reference(payload: ReferencePayload) -> Dict:
    ref_id = _new_id("ref")
    data = payload.model_dump()
    data["id"] = ref_id
    DB["references"][ref_id] = data
    return data


@app.get("/references")
def list_references() -> List[Dict]:
    return list(DB["references"].values())


@app.post("/laws")
def create_law(payload: LawItem) -> Dict:
    law_id = _new_id("law")
    data = payload.model_dump()
    data["id"] = law_id
    DB["laws"][law_id] = data
    return data


@app.get("/laws")
def list_laws() -> List[Dict]:
    return list(DB["laws"].values())


@app.post("/cases")
def create_case(title: str, prosecutor: str, case_type: str) -> Dict:
    case_id = _new_id("case")
    data = {
        "id": case_id,
        "title": title,
        "prosecutor": prosecutor,
        "case_type": case_type,
        "materials": [],
        "indictment": None,
        "extracted": None,
        "versions": [],
    }
    DB["cases"][case_id] = data
    return data


@app.post("/cases/{case_id}/materials")
async def upload_material(case_id: str, files: List[UploadFile] = File(...)) -> Dict:
    case = DB["cases"].get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")

    saved_files = []
    for file in files:
        suffix = Path(file.filename).suffix or ".bin"
        target = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
        content = await file.read()
        target.write_bytes(content)
        extracted = extract_material(target)
        case["materials"].append({"path": str(target), "name": file.filename, "extracted": extracted.model_dump()})
        saved_files.append({"name": file.filename, "path": str(target), "confidence": extracted.ocr_confidence})

    DB["logs"].append({"event": "upload_material", "case_id": case_id, "count": len(saved_files), "time": datetime.utcnow().isoformat()})
    return {"saved": saved_files}


@app.post("/cases/{case_id}/indictment")
async def upload_indictment(case_id: str, file: UploadFile = File(...)) -> Dict:
    case = DB["cases"].get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")

    suffix = Path(file.filename).suffix or ".bin"
    target = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    target.write_bytes(await file.read())
    extracted = extract_material(target)
    case["indictment"] = {"name": file.filename, "path": str(target), "extracted": extracted.model_dump()}
    return case["indictment"]


@app.post("/cases/{case_id}/extraction/correct")
def correct_extraction(case_id: str, payload: CorrectionPayload) -> Dict:
    case = DB["cases"].get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="case not found")
    case["extracted"] = payload.extracted.model_dump()
    return {"status": "updated", "case_id": case_id}


@app.post("/reports/generate")
def generate_report(payload: GeneratePayload) -> Dict:
    case = DB["cases"].get(payload.case_id)
    template = DB["templates"].get(payload.template_id)
    if not case or not template:
        raise HTTPException(status_code=404, detail="case or template not found")

    references = [DB["references"][r] for r in payload.reference_ids if r in DB["references"]]
    law_items = [DB["laws"][l] for l in payload.law_item_ids if l in DB["laws"]]

    merged_text = "\n".join(
        [json.dumps(i["extracted"], ensure_ascii=False) for i in case.get("materials", [])]
        + ([json.dumps(case["indictment"]["extracted"], ensure_ascii=False)] if case.get("indictment") else [])
    )
    rec_laws = recommend_laws(merged_text, law_items)
    result: GenerationResult = build_report_content(case, template, references, rec_laws)

    report_id = _new_id("rpt")
    report_data = result.model_dump()
    report_data.update({"id": report_id, "case_id": payload.case_id, "created_at": datetime.utcnow().isoformat()})
    DB["reports"][report_id] = report_data
    case["versions"].append(report_id)

    DB["logs"].append({"event": "generate_report", "case_id": payload.case_id, "report_id": report_id, "time": datetime.utcnow().isoformat()})
    return report_data


@app.get("/reports/{report_id}")
def get_report(report_id: str) -> Dict:
    report = DB["reports"].get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="report not found")
    return report


@app.post("/reports/{report_id}/export")
def export_report(report_id: str) -> Dict:
    report = DB["reports"].get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="report not found")

    filename = f"{report_id}.docx"
    target = EXPORT_DIR / filename
    export_docx(report, target)
    report["export_path"] = str(target)
    return {"path": str(target), "filename": filename}


@app.get("/reports/{report_id}/download")
def download_report(report_id: str):
    report = DB["reports"].get(report_id)
    if not report or not report.get("export_path"):
        raise HTTPException(status_code=404, detail="export not found")
    return FileResponse(report["export_path"], filename=Path(report["export_path"]).name)


@app.get("/logs")
def logs() -> List[Dict]:
    return DB["logs"]
