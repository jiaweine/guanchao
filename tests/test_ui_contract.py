from pathlib import Path


def test_customer_page_has_no_competitor_or_internal_model_names():
    text=Path('frontend/index.html').read_text(encoding='utf-8')+Path('frontend/app.js').read_text(encoding='utf-8')
    banned=['DeepSeek','Claude','OpenAI','Qwen','InternVL','特征工程','权重','梯度','logistic','算法模型']
    assert all(word.lower() not in text.lower() for word in banned)


def test_customer_page_exposes_multimodal_input():
    text=Path('frontend/index.html').read_text(encoding='utf-8')
    for word in ['图片','视频','音频','文档']: assert word in text
