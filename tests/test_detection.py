from guanchao.detection import MarketingDetector
from guanchao.domain import AccountSnapshot
from guanchao.sample_data import creator_target, demo_target


def test_marketing_sample_is_high_signal():
    result = MarketingDetector().analyze(AccountSnapshot.from_dict(demo_target()))
    assert result.marketing_likelihood > 0.72
    assert result.confidence > 0.7
    assert result.label in {"明显营销倾向", "高度营销化"}
    assert any(e.key in {"call_to_action", "commercial_language", "cross_post_pressure"} for e in result.evidence)


def test_creator_sample_stays_low():
    result = MarketingDetector().analyze(AccountSnapshot.from_dict(creator_target()))
    assert result.marketing_likelihood < 0.45
    assert result.label in {"更像普通创作者", "存在部分营销信号"}
    assert any(e.direction == "against" for e in result.evidence)


def test_tiny_sample_reports_missing_evidence():
    raw = {
        "platform": "douyin",
        "handle": "tiny",
        "posts": [{"text": "今天随手拍了一段路上的猫。"}],
    }
    result = MarketingDetector().analyze(AccountSnapshot.from_dict(raw))
    assert result.confidence < 0.6
    assert result.missing

def test_chinese_short_word_does_not_create_cta_false_positive():
    raw = {
        "platform": "weibo",
        "handle": "film_notes",
        "bio": "胶片与城市散步",
        "posts": [
            {"text": "冲洗店老板说这卷有点欠曝，我自己倒挺喜欢。"},
            {"text": "今天在河边走了很久，风很大。"},
            {"text": "这周没有拍照，只是把旧底片重新整理了一遍。"},
        ],
    }
    result = MarketingDetector().analyze(AccountSnapshot.from_dict(raw))
    assert result.features.call_to_action == 0.0
