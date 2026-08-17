import re
from pathlib import Path


def customer_text() -> str:
    return Path("frontend/index.html").read_text(encoding="utf-8") + Path("frontend/app.js").read_text(encoding="utf-8")


def test_customer_page_has_no_competitor_or_internal_model_names():
    text = customer_text()
    banned = ["DeepSeek", "Claude", "OpenAI", "Qwen", "InternVL", "特征工程", "权重", "梯度", "logistic", "算法模型"]
    assert all(word.lower() not in text.lower() for word in banned)


def test_customer_page_exposes_complete_product_workflow():
    text = Path("frontend/index.html").read_text(encoding="utf-8")
    for word in [
        "图片", "视频", "音频", "文档", "批量导入", "待复核", "待更新", "无法判断",
        "负责人", "业务优先级", "更新资料", "导出报告", "复制链接", "数据留存", "工作空间",
    ]:
        assert word in text


def test_first_release_has_no_product_version_aliases():
    paths = [
        *Path("guanchao").glob("*.py"),
        Path("frontend/index.html"),
        Path("frontend/app.js"),
        Path("README.md"),
        Path("pyproject.toml"),
    ]
    pattern = re.compile(r"(?i)(^|[^a-z0-9])v[12]([^a-z0-9]|$)")
    for path in paths:
        assert not pattern.search(path.read_text(encoding="utf-8")), path


def test_switching_cases_discards_stale_polling_results():
    text = Path("frontend/app.js").read_text(encoding="utf-8")
    assert "state.caseId && state.caseId !== id && state.polling" in text
    assert text.count("runId !== state.runId || caseId !== state.caseId") >= 2


def test_review_queue_supports_continuous_keyboard_review():
    text = customer_text()
    for decision in ["confirm_ordinary", "uncertain", "confirm_marketing"]:
        assert decision in text
    for key in ["event.key === '1'", "event.key === '2'", "event.key === '3'"]:
        assert key in text


def test_batch_and_monitoring_are_real_api_workflows_not_static_copy():
    text = Path("frontend/app.js").read_text(encoding="utf-8")
    for endpoint in ["/api/cases/batch", "/api/monitoring?due_only=true", "/target", "/report?output=markdown", "/api/members", "/api/workspace/settings"]:
        assert endpoint in text


def test_browser_cannot_choose_or_forge_workspace_identity():
    js = Path("frontend/app.js").read_text(encoding="utf-8")
    html = Path("frontend/index.html").read_text(encoding="utf-8")
    assert "/api/session" in js
    assert "X-Guanchao-Actor" not in js
    assert "localStorage" not in js
    assert "actorSelect" not in js + html
    assert "当前身份" in html and "浏览器不能自行切换" in html


def test_customer_workspace_has_collaborative_notes_separate_from_agent_chat():
    html = Path("frontend/index.html").read_text(encoding="utf-8")
    js = Path("frontend/app.js").read_text(encoding="utf-8")
    assert "协作备注" in html
    assert "/comments" in js
    assert "caseNoteInput" in js
