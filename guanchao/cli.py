from __future__ import annotations
import json,sys,tempfile
from .api import create_app
from .sample_data import demo_target


def main()->None:
    goal=" ".join(sys.argv[1:]).strip() or "判断这个账号是否长期营销运营，并给出证据。"
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        app=create_app(f.name); store=app.state.store; harness=app.state.harness
        case=store.create_case("本地演示",goal,[demo_target()]); run=harness.execute_inline(case["id"],goal)
        print(json.dumps(run["state"],ensure_ascii=False,indent=2))
if __name__=="__main__":main()
