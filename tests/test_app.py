from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_full_flow_generates_and_exports_report(tmp_path):
    tpl = client.post(
        "/templates",
        json={
            "case_type": "一审",
            "name": "一审模板",
            "sections": ["案件基本情况", "事实认定", "证据分析", "法律适用", "程序审查"],
            "required_fields": ["案件编号", "承办人"],
        },
    )
    assert tpl.status_code == 200
    template_id = tpl.json()["id"]

    ref = client.post(
        "/references",
        json={"title": "参考文书A", "tags": {"案由": "盗窃"}, "content": "经审查认为，事实清楚，证据确实充分。"},
    )
    assert ref.status_code == 200

    law = client.post(
        "/laws",
        json={
            "title": "中华人民共和国刑法",
            "article": "第二百六十四条",
            "keywords": ["盗窃", "财物"],
            "source": "国家法律法规数据库",
        },
    )
    assert law.status_code == 200

    case_resp = client.post("/cases", params={"title": "张三盗窃案", "prosecutor": "李检", "case_type": "一审"})
    assert case_resp.status_code == 200
    case_id = case_resp.json()["id"]

    material_content = "2024年1月1日张三盗窃财物。证据包括证人证言、书证。已立案并移送审查起诉。"
    upload = client.post(
        f"/cases/{case_id}/materials",
        files=[("files", ("material.txt", material_content, "text/plain"))],
    )
    assert upload.status_code == 200

    indictment = client.post(
        f"/cases/{case_id}/indictment",
        files={"file": ("indictment.txt", "侦查机关认为构成盗窃罪。", "text/plain")},
    )
    assert indictment.status_code == 200

    gen = client.post(
        "/reports/generate",
        json={
            "case_id": case_id,
            "case_type": "一审",
            "template_id": template_id,
            "reference_ids": [ref.json()["id"]],
            "law_item_ids": [law.json()["id"]],
        },
    )
    assert gen.status_code == 200
    report = gen.json()
    assert report["title"].startswith("张三盗窃案")
    assert len(report["sections"]) == 5

    export_resp = client.post(f"/reports/{report['id']}/export")
    assert export_resp.status_code == 200
    assert export_resp.json()["filename"].endswith(".docx")
