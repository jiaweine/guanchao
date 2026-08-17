from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from typing import Any

from .detection import MarketingDetector
from .domain import RunEvent
from .policy import OwnedPolicy
from .store import Store
from .tools import ToolRegistry
from .verifier import ResultVerifier


class ActiveRunError(RuntimeError): pass


class AgentHarness:
    def __init__(self,store:Store):
        self.store=store; self.verifier=ResultVerifier(); self._guard=threading.RLock(); self._futures:dict[str,Future]={}; self._active_cases:dict[str,str]={}
        self._executor=ThreadPoolExecutor(max_workers=max(2,int(os.getenv("GUANCHAO_MAX_WORKERS","8"))),thread_name_prefix="guanchao")

    def start(self,case_id:str,message:str)->str:
        case=self.store.get_case(case_id)
        with self._guard:
            existing=self._active_cases.get(case_id)
            if existing:
                run=self.store.get_run(existing)
                if run["status"]=="running":raise ActiveRunError(existing)
                self._active_cases.pop(case_id,None)
            db_active=self.store.active_run_for_case(case_id)
            if db_active:raise ActiveRunError(db_active["id"])
            self.store.add_message(case_id,"user",message)
            assets=self.store.list_assets(case_id,include_text=True)
            state:dict[str,Any]={"goal":message or case["goal"],"targets":case["targets"],"assets":assets,"sample_size":len(case["targets"][0].get("posts") or []) if case["targets"] else 0,"completed_tools":[],"events":[RunEvent.create("plan","开始核查","正在判断哪些证据最值得先看。",status="working").asdict()],"evidence":[],"tool_outputs":{},"primary_result":{},"answer":None,"decision_count":0}
            run=self.store.create_run(case_id,state); self._active_cases[case_id]=run["id"]; future=self._executor.submit(self._execute,run["id"]); self._futures[run["id"]]=future
            return run["id"]

    def execute_inline(self,case_id:str,message:str)->dict[str,Any]:
        run_id=self.start(case_id,message); self.wait(run_id,10); return self.store.get_run(run_id)
    def wait(self,run_id:str,timeout:float=10)->None:
        with self._guard:future=self._futures.get(run_id)
        if future:
            try:future.result(timeout=timeout)
            except TimeoutError:return

    def _execute(self,run_id:str)->None:
        run=self.store.get_run(run_id); state=run["state"]; case_id=run["case_id"]
        detector=MarketingDetector(self.store.get_calibration()); policy=OwnedPolicy(self.store.get_policy_profile()); registry=ToolRegistry(detector)
        try:
            for _ in range(12):
                decision=policy.decide(state["goal"],state)
                if decision is None:break
                state["decision_count"]+=1; state["events"].append(RunEvent.create("decision","继续核查",decision.reason,decision.tool,"working").asdict()); self.store.update_run(run_id,state,"running")
                result=registry.get(decision.tool).handler(state); ok,reason=self.verifier.verify(result)
                if not ok:
                    state["events"].append(RunEvent.create("verify","这一步没有通过检查",reason,decision.tool,"error").asdict()); self.store.update_run(run_id,state,"failed"); return
                state["completed_tools"].append(decision.tool); state["tool_outputs"][decision.tool]=result.asdict(); self._merge_evidence(state,result.asdict()["evidence"])
                state["events"].append(RunEvent.create("tool",self._customer_title(decision.tool),result.summary,decision.tool,"done").asdict())
                if decision.tool=="content.scan":state["primary_result"]=result.payload
                if decision.tool=="stability.probe" and state.get("primary_result"):state["primary_result"]["stability"]=result.payload.get("stability",state["primary_result"].get("stability"))
                if decision.tool=="verdict.compose":
                    state["primary_result"]=result.payload; state["answer"]=result.payload["summary"]; self.store.add_message(case_id,"assistant",state["answer"])
                self.store.update_run(run_id,state,"running")
            if not state.get("answer"):
                state["answer"]="当前资料还不足以形成稳定判断，请补充更多近期内容或可核对素材。"; self.store.add_message(case_id,"assistant",state["answer"])
            state["events"].append(RunEvent.create("complete","核查完成","判断、证据和待补资料已经整理好。",status="done").asdict()); self.store.update_run(run_id,state,"completed")
        except Exception as exc:
            state["internal_error"]=type(exc).__name__; state["events"].append(RunEvent.create("error","执行未完成","本次核查中断，已保留已有记录，可以重新发起。",status="error").asdict()); self.store.update_run(run_id,state,"failed")
        finally:
            with self._guard:self._futures.pop(run_id,None); self._active_cases.pop(case_id,None)

    @staticmethod
    def _merge_evidence(state:dict[str,Any],items:list[dict[str,Any]])->None:
        seen={(e.get("key"),e.get("direction"),tuple(e.get("post_ids") or []),tuple(e.get("asset_ids") or [])) for e in state.get("evidence") or []}
        for item in items:
            key=(item.get("key"),item.get("direction"),tuple(item.get("post_ids") or []),tuple(item.get("asset_ids") or []))
            if key not in seen:state["evidence"].append(item); seen.add(key)

    @staticmethod
    def _customer_title(tool:str)->str:
        return {"workspace.inspect":"资料已整理","profile.read":"主页已核对","media.inspect":"素材已读取","content.scan":"近期内容已扫描","pattern.compare":"内容模式已对照","peer.compare":"同批账号已比较","stability.probe":"稳定性已检查","evidence.challenge":"反向线索已核查","verdict.compose":"判断已形成"}.get(tool,"步骤完成")
