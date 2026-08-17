import json

from guanchao.detection import MarketingDetector
from guanchao.domain import AccountSnapshot
from guanchao.semantic import SemanticEvidenceGateway, SemanticSignal


class StubSemanticGateway:
    enabled = True

    def inspect(self, account, media_text=""):
        return SemanticSignal(
            values={
                "call_to_action": 0.95,
                "cross_post_pressure": 0.88,
                "commercial_language": 0.72,
            },
            post_ids={"call_to_action": ["p1", "p2"], "cross_post_pressure": ["p1"]},
            grounded_fraction=1.0,
        )


def _soft_account():
    return AccountSnapshot.from_dict(
        {
            "platform": "weibo",
            "handle": "soft",
            "bio": "日常分享",
            "posts": [
                {"id": "p1", "text": "问的人很多，我把入口放置顶了。"},
                {"id": "p2", "text": "想要同款的自己看主页第一条。"},
                {"id": "p3", "text": "不逐个回复啦，需要的照着固定内容找。"},
                {"id": "p4", "text": "今天继续整理大家问得多的东西。"},
            ],
        }
    )


def test_grounded_semantic_teacher_can_strengthen_but_not_replace_owned_score():
    account = _soft_account()
    plain = MarketingDetector().analyze(account)
    assisted = MarketingDetector(semantic_gateway=StubSemanticGateway()).analyze(account)
    assert assisted.marketing_likelihood > plain.marketing_likelihood
    assert 0.0 <= assisted.marketing_likelihood <= 1.0
    assert any("p1" in evidence.post_ids for evidence in assisted.evidence)


def test_semantic_gateway_discards_unverifiable_model_quotes(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "signals": {
                                        "call_to_action": {
                                            "score": 0.99,
                                            "quotes": ["这句话根本不在原始资料里"],
                                        }
                                    }
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setenv("GUANCHAO_SEMANTIC_ENDPOINT", "http://teacher.local")
    monkeypatch.setattr("guanchao.semantic.httpx.Client", Client)
    gateway = SemanticEvidenceGateway()
    signal = gateway.inspect(_soft_account())
    assert signal.values == {}
    assert not signal.usable
