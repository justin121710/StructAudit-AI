"""埋錯測試集執行器：比較不同審查設定（輪數、模型組合）的抓錯率、誤報與費用。

用法（在儲存庫根目錄）：
    python3 -m bench.run_bench --calc examples/sample_C1_calc.pdf --passes 1 3 \\
        --slots gemini:gemini-2.5-flash anthropic:claude-sonnet-5-5 openai:gpt-5-mini
    python3 -m bench.run_bench --offline            # 離線示範（MockClient），只驗證流程，不代表模型能力

API Key 取自環境變數 GEMINI_API_KEY／ANTHROPIC_API_KEY／OPENAI_API_KEY。
每個設定 × (乾淨計算書 + 每個 Seed) 各跑一次審查；--seeds S01 S03 可只跑部分 Seed，--repeat 2 重複取平均。
輸出 bench/results/<時間>.json 與 .md：
  抓錯率＝抓到的 Seed 數 ÷ 適用的 Seed 數（自動初判，報告附每項命中的原文供人工確認）；
  乾淨計算書的中、高風險項目列為「誤報候選」（示範計算書本身可能有真正的問題，須人工判斷）；
  費用與時間取自審查紀錄。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from agents.llm_client import MockClient, create_client
from bench.seeds import SEEDS, applicable, score, seeded_calc, session_findings
from orchestrator import ReviewOrchestrator
from utils.calcbook import read_calcbook

KEY_ENV = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "claude": "ANTHROPIC_API_KEY",
           "openai": "OPENAI_API_KEY"}


def factories(slots: list[str], offline: bool):
    if offline or not slots:
        return MockClient
    out = []
    for s in slots:
        prov, _, model = s.partition(":")
        key = os.getenv(KEY_ENV.get(prov, ""), "")
        if not key:
            raise SystemExit(f"缺少 {KEY_ENV.get(prov, prov)}（槽位 {s}）")
        out.append(lambda p=prov, m=model or None, k=key: create_client(p, model=m, api_key=k))
    return out


def cost_of(session) -> float:
    """新台幣；離線示範與未設定單價的模型為 0。"""
    c = session.cost()
    return round(float(c.usd or 0.0) * float(c.fx), 2) if c else 0.0


def run(calc_path: str, passes: list[int], slots: list[str], offline: bool, seed_ids: list[str] | None = None,
        repeat: int = 1, out_dir: str = "bench/results", log=print) -> dict:
    base = read_calcbook(calc_path)
    text = "\n".join(base.pages)
    seeds = [s for s in SEEDS if (not seed_ids or s.id in seed_ids) and applicable(s, text)]
    skipped = [s.id for s in SEEDS if (not seed_ids or s.id in seed_ids) and s not in seeds]
    fac = factories(slots, offline)
    configs = []
    for n in passes:
        cfg = {"passes": n, "slots": slots or ["offline"], "runs": []}
        for k in range(repeat):
            t0 = time.time()
            clean = ReviewOrchestrator(fac, passes=n).run(base, None, "")
            fp = [asdict(f) for f in session_findings(clean) if f.risk != "低風險"]
            res = []
            for sd in seeds:
                t1 = time.time()
                s = ReviewOrchestrator(fac, passes=n).run(seeded_calc(base, sd), None, "")
                r = score(sd, s)
                r.cost_twd, r.seconds = cost_of(s), round(time.time() - t1, 1)
                res.append(asdict(r))
                log(f"[{n} 輪 #{k+1}] {sd.id} {'抓到' if r.detected else '漏掉'}  {r.risk}  NT${r.cost_twd:.1f}")
            cfg["runs"].append({"clean_flags": fp, "clean_cost_twd": cost_of(clean), "seeds": res,
                                "seconds": round(time.time() - t0, 1)})
        det = [x["detected"] for run in cfg["runs"] for x in run["seeds"]]
        cfg["detect_rate"] = round(sum(det) / len(det), 3) if det else None
        cfg["mean_cost_twd"] = round(sum(x["cost_twd"] for run in cfg["runs"] for x in run["seeds"]) / max(1, len(det)), 1)
        cfg["mean_clean_flags"] = round(sum(len(run["clean_flags"]) for run in cfg["runs"]) / repeat, 2)
        configs.append(cfg)
    report = {"calc": calc_path, "at": datetime.now().strftime("%Y-%m-%d %H:%M"), "offline": offline or not slots,
              "seeds": [{"id": s.id, "title": s.title, "kind": s.kind, "clause": s.clause} for s in seeds],
              "skipped": skipped, "configs": configs}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stem = Path(out_dir) / datetime.now().strftime("%Y%m%d-%H%M%S")
    stem.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    report["files"] = [str(stem.with_suffix(".json")), str(stem.with_suffix(".md"))]
    return report


def markdown(rep: dict) -> str:
    L = [f"# 埋錯測試結果（{rep['at']}）", "", f"計算書：`{rep['calc']}`" + ("　**離線示範，不代表模型能力**" if rep["offline"] else ""), ""]
    L += ["| 設定 | 抓錯率 | 每次審查費用（NT$） | 乾淨計算書的中高風險項目（誤報候選） |", "|---|---|---|---|"]
    for c in rep["configs"]:
        rate = "—" if c["detect_rate"] is None else f"{c['detect_rate']*100:.0f} %"
        L.append(f"| {c['passes']} 輪；{'、'.join(c['slots'])} | {rate} | {c['mean_cost_twd']} | {c['mean_clean_flags']} |")
    L += ["", "## 各 Seed", "", "| Seed | 錯誤 | " + " | ".join(f"{c['passes']} 輪" for c in rep["configs"]) + " |",
          "|---|---|" + "---|" * len(rep["configs"])]
    for sd in rep["seeds"]:
        cells = []
        for c in rep["configs"]:
            hits = [x for run in c["runs"] for x in run["seeds"] if x["seed"] == sd["id"]]
            cells.append(f"{sum(h['detected'] for h in hits)}/{len(hits)}")
        L.append(f"| {sd['id']} | {sd['title']} | " + " | ".join(cells) + " |")
    L += ["", "## 命中原文（人工確認用）", ""]
    for c in rep["configs"]:
        for run in c["runs"]:
            for x in run["seeds"]:
                if x["detected"]:
                    L.append(f"- {c['passes']} 輪 {x['seed']}（{x['risk']}）：{x['evidence']}")
    if rep["skipped"]:
        L += ["", f"不適用這份計算書的 Seed：{'、'.join(rep['skipped'])}"]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calc", default="examples/sample_C1_calc.pdf")
    ap.add_argument("--passes", type=int, nargs="+", default=[1, 3])
    ap.add_argument("--slots", nargs="*", default=[], help="provider:model，最多 3 個（輪流擔任三個角色）")
    ap.add_argument("--seeds", nargs="*", default=None)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    rep = run(a.calc, a.passes, a.slots, a.offline, a.seeds, a.repeat)
    print("\n".join(rep["files"]))


if __name__ == "__main__":
    main()
