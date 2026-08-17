from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable

from .detection import Calibration
from .domain import FeatureVector
from .policy import PolicyProfile


@dataclass(slots=True)
class LabeledExample:
    features: FeatureVector
    label: int
    group: str = ""


@dataclass(slots=True)
class EvolutionReport:
    accepted: bool
    baseline_score: float
    candidate_score: float
    worst_fold_delta: float
    examples: int
    reason: str
    calibration: Calibration
    policy_profile: PolicyProfile

    def to_dict(self) -> dict[str, object]:
        return {"accepted":self.accepted,"baseline_score":self.baseline_score,"candidate_score":self.candidate_score,"worst_fold_delta":self.worst_fold_delta,"examples":self.examples,"reason":self.reason,"calibration":self.calibration.to_dict(),"policy_profile":self.policy_profile.to_dict()}


class EvolutionEngine:
    """Cross-validated, regression-gated evolution of calibration and bounded harness behavior."""

    def evolve(self, current: Calibration, examples: list[LabeledExample], policy: PolicyProfile | None = None) -> EvolutionReport:
        policy = policy or PolicyProfile()
        if len(examples) < 10:
            return EvolutionReport(False,0,0,0,len(examples),"至少需要 10 条人工复核记录",current,policy)
        if len({x.label for x in examples}) < 2:
            return EvolutionReport(False,0,0,0,len(examples),"复核记录需要同时包含两类结果",current,policy)
        folds=self._folds(examples,5)
        baseline_scores=[]; candidate_scores=[]; fold_deltas=[]; candidates=[]
        for i, holdout in enumerate(folds):
            train=[x for j,fold in enumerate(folds) if j!=i for x in fold]
            if not holdout or len({x.label for x in holdout})<2: continue
            baseline=self._metric(current,holdout)
            local_candidates=[self._gradient_candidate(current,train,step) for step in (0.08,0.14,0.22,0.30)]
            scored=[(self._metric(c,holdout),c) for c in local_candidates]
            score,best=max(scored,key=lambda x:x[0])
            baseline_scores.append(baseline); candidate_scores.append(score); fold_deltas.append(score-baseline); candidates.append(best)
        if len(fold_deltas)<2:
            return EvolutionReport(False,0,0,0,len(examples),"有效回放折数不足",current,policy)
        best=self._median_candidate(candidates,current)
        baseline=sum(baseline_scores)/len(baseline_scores); candidate=sum(candidate_scores)/len(candidate_scores); worst=min(fold_deltas)
        global_ok=self._no_class_regression(current,best,examples)
        accepted=(candidate>=baseline+0.004 and worst>=-0.015 and global_ok)
        profile=self._policy_candidate(policy,current,best,examples) if accepted else policy
        reason="候选在多折回放中稳定提升，且未出现类别回归，已接纳" if accepted else "候选没有通过多折回放、最差折或类别回归门槛"
        return EvolutionReport(accepted,baseline,candidate,worst,len(examples),reason,best if accepted else current,profile)

    @staticmethod
    def _folds(examples:list[LabeledExample],k:int)->list[list[LabeledExample]]:
        folds=[[] for _ in range(k)]
        for item in examples:
            fingerprint=item.group or "|".join(f"{v:.5f}" for v in item.features.asdict().values())+f"|{item.label}"
            bucket=int(hashlib.sha256(fingerprint.encode()).hexdigest()[:8],16)%k; folds[bucket].append(item)
        return folds

    def _gradient_candidate(self,current:Calibration,examples:Iterable[LabeledExample],step:float)->Calibration:
        examples=list(examples)
        if not examples:return current
        grad_bias=0.0; grad={k:0.0 for k in current.weights}
        for item in examples:
            p=self._predict(current,item.features); error=p-item.label; grad_bias+=error
            for key in grad: grad[key]+=error*getattr(item.features,key,0.0)
        scale=1/len(examples); bias=_clip(current.bias-step*grad_bias*scale,-4.5,1.0); weights={}
        for key,old in current.weights.items():
            proposed=old-step*grad[key]*scale
            proposed=min(-0.05,proposed) if key=="authentic_variation" else max(0.02,proposed)
            weights[key]=_clip(proposed,-2.5,2.5)
        return Calibration(bias,weights)

    @staticmethod
    def _median_candidate(candidates:list[Calibration],fallback:Calibration)->Calibration:
        if not candidates:return fallback
        ordered=sorted(candidates,key=lambda c:c.bias); chosen=ordered[len(ordered)//2]
        return chosen

    def _metric(self,c:Calibration,examples:list[LabeledExample])->float:
        probs=[self._predict(c,x.features) for x in examples]; labels=[x.label for x in examples]; preds=[int(p>=.5) for p in probs]
        tpr=self._recall(preds,labels,1); tnr=self._recall(preds,labels,0); balanced=(tpr+tnr)/2
        brier=sum((p-y)**2 for p,y in zip(probs,labels))/len(labels); return balanced-.30*brier

    def _no_class_regression(self,old:Calibration,new:Calibration,examples:list[LabeledExample])->bool:
        labels=[x.label for x in examples]; oldp=[int(self._predict(old,x.features)>=.5) for x in examples]; newp=[int(self._predict(new,x.features)>=.5) for x in examples]
        return all(self._recall(newp,labels,k)+.04>=self._recall(oldp,labels,k) for k in (0,1))

    def _policy_candidate(self,p:PolicyProfile,old:Calibration,new:Calibration,examples:list[LabeledExample])->PolicyProfile:
        old_probs=[self._predict(old,x.features) for x in examples]; new_probs=[self._predict(new,x.features) for x in examples]
        false_pos=sum(1 for x,prob in zip(examples,new_probs) if x.label==0 and prob>=.5)
        false_neg=sum(1 for x,prob in zip(examples,new_probs) if x.label==1 and prob<.5)
        total=max(1,len(examples)); pressure=(false_pos-false_neg)/total
        return PolicyProfile(
            challenge_confidence=_clip(p.challenge_confidence+pressure*.08,.66,.86),
            stability_confidence=_clip(p.stability_confidence+abs(pressure)*.04,.72,.88),
            min_pattern_posts=p.min_pattern_posts,
            min_stability_posts=p.min_stability_posts,
            verdict_evidence_floor=3 if false_pos/total>.16 else 2,
        )

    @staticmethod
    def _recall(preds:list[int],labels:list[int],klass:int)->float:
        idx=[i for i,y in enumerate(labels) if y==klass]
        return .5 if not idx else sum(1 for i in idx if preds[i]==klass)/len(idx)
    @staticmethod
    def _predict(c:Calibration,f:FeatureVector)->float:
        linear=c.bias+sum(c.weights[k]*getattr(f,k,0.0) for k in c.weights); return 1/(1+math.exp(-max(-20,min(20,linear))))

def _clip(value:float,low:float,high:float)->float:return max(low,min(high,value))
