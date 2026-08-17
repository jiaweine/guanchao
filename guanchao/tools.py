from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .detection import MarketingDetector
from .domain import AccountSnapshot, Evidence, ToolResult


@dataclass(slots=True)
class ToolSpec:
    name: str
    risk: str
    description: str
    handler: Callable[[dict], ToolResult]


class ToolRegistry:
    def __init__(self, detector: MarketingDetector):
        self.detector=detector; self._tools:dict[str,ToolSpec]={}; self._register_defaults()
    def get(self,name:str)->ToolSpec:return self._tools[name]
    def names(self)->list[str]:return list(self._tools)
    def _register(self,name:str,risk:str,description:str,handler:Callable[[dict],ToolResult])->None:self._tools[name]=ToolSpec(name,risk,description,handler)
    def _register_defaults(self)->None:
        self._register("workspace.inspect","read","Inspect available evidence",self._workspace)
        self._register("profile.read","read","Read profile evidence",self._profile)
        self._register("media.inspect","read","Inspect multimodal evidence",self._media)
        self._register("content.scan","read","Scan recent content",self._content)
        self._register("pattern.compare","simulation","Compare content patterns",self._patterns)
        self._register("peer.compare","simulation","Compare accounts in the same batch",self._peers)
        self._register("stability.probe","simulation","Probe verdict stability",self._stability)
        self._register("evidence.challenge","simulation","Search for counter-evidence",self._challenge)
        self._register("verdict.compose","read","Compose evidence-backed verdict",self._verdict)

    @staticmethod
    def _primary(state:dict)->AccountSnapshot:return AccountSnapshot.from_dict(state["targets"][0])
    @staticmethod
    def _media_text(state:dict)->str:return "\n".join(str(a.get("extracted_text") or "") for a in state.get("assets") or [] if a.get("status")=="ready")

    def _workspace(self,state:dict)->ToolResult:
        targets=[AccountSnapshot.from_dict(x) for x in state.get("targets") or []]
        if not targets:return ToolResult("workspace.inspect",False,"没有可检查的账号资料",error="empty_workspace")
        post_count=sum(len(t.posts) for t in targets); assets=state.get("assets") or []; ready=sum(1 for a in assets if a.get("status")=="ready")
        missing=[]
        if post_count<3:missing.append("近期内容不足")
        if all(not t.bio for t in targets):missing.append("缺少主页简介")
        if assets and ready<len(assets):missing.append("部分素材尚未完成读取")
        return ToolResult("workspace.inspect",True,f"已读取 {len(targets)} 个账号、{post_count} 条内容和 {len(assets)} 份素材。",{"target_count":len(targets),"post_count":post_count,"asset_count":len(assets),"asset_ready":ready,"missing":missing})

    def _profile(self,state:dict)->ToolResult:
        account=self._primary(state); features,evidence,_=self.detector.extract(account,self._media_text(state)); selected=[e for e in evidence if e.key in {"profile_commerciality","contact_pressure","identity_consistency"}]
        return ToolResult("profile.read",True,"主页身份和长期经营线索已核对。",{"handle":account.handle,"followers":account.followers,"verified":account.verified},selected)

    def _media(self,state:dict)->ToolResult:
        assets=state.get("assets") or []; ready=[a for a in assets if a.get("status")=="ready"]; pending=[a for a in assets if a.get("status")!="ready"]
        text=self._media_text(state)
        if not assets:return ToolResult("media.inspect",True,"当前没有附加素材。",{"asset_count":0,"ready":0,"pending":0})
        account=self._primary(state); features,evidence,_=self.detector.extract(account,text); selected=[e for e in evidence if e.key in {"media_commerciality","identity_consistency"}]
        if ready and not selected:
            selected=[Evidence("media_context","素材未出现强商业线索","已读取的图片、视频、音频或文档没有提取到集中转化信息。",0.38,"against",asset_ids=[a["id"] for a in ready[:3]])]
        else:
            for e in selected:e.asset_ids=[a["id"] for a in ready[:3]]
        summary=f"已读取 {len(ready)} 份素材"+(f"，{len(pending)} 份仍待补充解析。" if pending else "并完成交叉核对。")
        return ToolResult("media.inspect",True,summary,{"asset_count":len(assets),"ready":len(ready),"pending":len(pending),"media_commerciality":features.media_commerciality},selected)

    def _content(self,state:dict)->ToolResult:
        account=self._primary(state); result=self.detector.analyze(account,self._media_text(state)); selected=[e for e in result.evidence if e.key in {"commercial_language","call_to_action","contact_pressure","cross_post_pressure","authentic_variation","disclosure_signal"}]
        return ToolResult("content.scan",True,f"已检查 {len(account.posts)} 条近期内容，并建立基础判断。",{"marketing_likelihood":result.marketing_likelihood,"covert_promotion_risk":result.covert_promotion_risk,"confidence":result.confidence,"stability":result.stability,"features":result.features.asdict(),"missing":result.missing},selected)

    def _patterns(self,state:dict)->ToolResult:
        result=self.detector.analyze(self._primary(state),self._media_text(state)); selected=[e for e in result.evidence if e.key in {"template_reuse","cadence_burst","engagement_pattern"}]
        return ToolResult("pattern.compare",True,"内容结构、发布时间和互动形态已对照。",{"template_reuse":result.features.template_reuse,"cadence_burst":result.features.cadence_burst,"engagement_pattern":result.features.engagement_pattern},selected)

    def _peers(self,state:dict)->ToolResult:
        rows=[]; media=self._media_text(state)
        for account in [AccountSnapshot.from_dict(x) for x in state.get("targets") or []]:
            r=self.detector.analyze(account,media); rows.append({"handle":account.handle,"label":r.label,"marketing_likelihood":r.marketing_likelihood,"confidence":r.confidence})
        rows.sort(key=lambda x:x["marketing_likelihood"],reverse=True)
        return ToolResult("peer.compare",True,f"已完成 {len(rows)} 个账号的同批对照。",{"accounts":rows})

    def _stability(self,state:dict)->ToolResult:
        account=self._primary(state); stability,swing=self.detector.stability_probe(account,self._media_text(state)); evidence=[]
        if swing>.20:evidence.append(Evidence("stability_swing","结论受少数内容影响","移除单条高影响内容后，营销倾向发生明显变化。",min(1.0,swing*2.4),"context"))
        elif len(account.posts)>=4:evidence.append(Evidence("stability_pass","结论对单条内容不敏感","逐条移除近期内容后，整体判断保持相对稳定。",min(1.0,.45+stability*.45),"context"))
        return ToolResult("stability.probe",True,"已完成逐条剔除复核，检查结论是否被少数内容主导。",{"stability":stability,"max_swing":swing},evidence)

    def _challenge(self,state:dict)->ToolResult:
        result=self.detector.analyze(self._primary(state),self._media_text(state)); against=[e for e in result.evidence if e.direction=="against"]; supports=[e for e in result.evidence if e.direction=="supports"]
        summary="找到能够削弱当前判断的反向线索，已纳入最终判断。" if against else ("没有找到足够强的反向线索，但资料缺口会降低把握度。" if result.missing else "反向核查未发现足以推翻当前判断的线索。")
        return ToolResult("evidence.challenge",True,summary,{"supports":len(supports),"against":len(against),"missing":result.missing,"confidence":result.confidence},against[:4])

    def _verdict(self,state:dict)->ToolResult:
        result=self.detector.analyze(self._primary(state),self._media_text(state)); missing=list(result.missing)
        assets=state.get("assets") or []
        if any(a.get("status")!="ready" for a in assets):missing.append("部分素材尚未完成读取")
        payload={"label":result.label,"summary":result.summary,"marketing_likelihood":result.marketing_likelihood,"covert_promotion_risk":result.covert_promotion_risk,"confidence":result.confidence,"stability":result.stability,"missing":list(dict.fromkeys(missing)),"features":result.features.asdict()}
        return ToolResult("verdict.compose",True,result.summary,payload,result.evidence)
