from __future__ import annotations

import argparse
import json
import tempfile

from .harness import AgentHarness
from .sample_data import demo_target
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local Guanchao investigation")
    parser.add_argument("goal", nargs="?", default="判断这个账号是不是长期营销运营号，并给出证据")
    args = parser.parse_args()
    with tempfile.NamedTemporaryFile(suffix=".db") as db:
        store = Store(db.name)
        case = store.create_case("本地演示", args.goal, [demo_target()])
        run = AgentHarness(store).execute_inline(case["id"], args.goal)
        print(json.dumps(run["state"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
