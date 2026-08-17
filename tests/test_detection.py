from guanchao.detection import MarketingDetector
from guanchao.domain import AccountSnapshot
from guanchao.sample_data import creator_target,demo_target


def test_marketing_sample_is_high_signal():
    r=MarketingDetector().analyze(AccountSnapshot.from_dict(demo_target()))
    assert r.marketing_likelihood>.70
    assert r.confidence>.70
    assert r.label in {"明显营销倾向","高度营销化"}
    assert r.stability>.45


def test_creator_sample_stays_low():
    r=MarketingDetector().analyze(AccountSnapshot.from_dict(creator_target()))
    assert r.marketing_likelihood<.45
    assert r.label in {"更像普通创作者","存在部分营销信号"}
    assert any(e.direction=="against" for e in r.evidence)


def test_tiny_sample_reports_missing_evidence():
    r=MarketingDetector().analyze(AccountSnapshot.from_dict({"platform":"douyin","handle":"tiny","posts":[{"text":"今天随手拍了一段路上的猫。"}]}))
    assert r.confidence<.60 and r.missing


def test_chinese_short_word_does_not_create_cta_false_positive():
    raw={"platform":"weibo","handle":"film_notes","bio":"胶片与城市散步","posts":[{"text":"冲洗店老板说这卷有点欠曝，我自己倒挺喜欢。"},{"text":"今天在河边走了很久，风很大。"},{"text":"这周没有拍照，只是把旧底片重新整理了一遍。"}]}
    r=MarketingDetector().analyze(AccountSnapshot.from_dict(raw)); assert r.features.call_to_action==0.0


def test_multimodal_evidence_can_raise_media_signal_without_becoming_the_verdict():
    account=AccountSnapshot.from_dict(creator_target()); d=MarketingDetector(); plain=d.analyze(account); media=d.analyze(account,"画面字幕：直播间限时福利，券后39元，点击橱窗下单")
    assert media.features.media_commerciality>plain.features.media_commerciality
    assert 0<=media.marketing_likelihood<=1


def test_stability_probe_detects_one_post_dominance():
    raw={"platform":"weibo","handle":"mixed","bio":"生活记录","posts":[{"text":"今天去公园散步，我自己很喜欢这条路。"},{"text":"下雨，在家看了一下午书。"},{"text":"晚饭做坏了，不过也算新体验。"},{"text":"闭眼入！直播间券后39元，点击主页橱窗直接下单，限时福利库存不多。"}]}
    r=MarketingDetector().analyze(AccountSnapshot.from_dict(raw)); assert r.stability<.92


def test_bad_posts_shape_is_rejected_cleanly():
    try: AccountSnapshot.from_dict({"platform":"weibo","handle":"bad","posts":{"text":"x"}})
    except TypeError as exc: assert "posts must be a list" in str(exc)
    else: raise AssertionError("expected TypeError")
