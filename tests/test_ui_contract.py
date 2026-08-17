import re
from pathlib import Path


def customer_text() -> str:
    return Path("frontend/index.html").read_text(encoding="utf-8") + Path("frontend/app.js").read_text(encoding="utf-8")


def test_customer_page_has_no_competitor_or_internal_model_names():
    text = customer_text()
    banned = ["DeepSeek", "Claude", "OpenAI", "Qwen", "InternVL", "特征工程", "权重", "梯度", "logistic", "算法模型"]
    assert all(word.lower() not in text.lower() for word in banned)


def test_customer_page_exposes_multimodal_input_and_review_queue():
    text = Path("frontend/index.html").read_text(encoding="utf-8")
    for word in ["图片", "视频", "音频", "文档", "待复核", "无法判断"]:
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
