"""埋錯測試集（bench/）：Seed 都能植入示範計算書、判定規則、離線執行器。"""
import json
from types import SimpleNamespace as NS

from bench.run_bench import run
from bench.seeds import SEEDS, applicable, hit, score, seeded_calc
from utils.calcbook import read_calcbook

CALC = "examples/sample_C1_calc.pdf"


def test_every_seed_applies_to_sample():
    c = read_calcbook(CALC)
    text = "\n".join(c.pages)
    for sd in SEEDS:
        assert applicable(sd, text), sd.id
        s = seeded_calc(c, sd)
        new = "\n".join(s.pages)
        assert new != text and s.pdf is None and s.sha256.startswith("seed-"), sd.id
        assert len(s.pages) == len(c.pages)
        assert s.from_rcmd, "植入錯誤不能破壞表頭（仍是 rc-member-designer 計算書模式）"


def test_seed_ids_unique_and_documented():
    ids = [s.id for s in SEEDS]
    assert len(ids) == len(set(ids)) and len(ids) >= 9
    assert all(s.title and s.hit for s in SEEDS)


def test_hit_rules():
    s1 = next(s for s in SEEDS if s.id == "S01")
    assert hit(s1, ["剪力強度折減因數 ϕ 誤用 0.85，土木401-112 表 21.2.1 規定 0.75"])
    assert not hit(s1, ["箍筋間距不足"]), "只提到一組關鍵字不算"
    s3 = next(s for s in SEEDS if s.id == "S03")
    assert hit(s3, ["外 保 護 層 2.0 cm 不足"]), "比對時忽略空白"


def _session(items):
    its = [NS(title=t, finding=f, required_action="", code_clauses=[], final_solution=None,
              risk_level=NS(value=r)) for t, f, r in items]
    return NS(final=NS(items=its))


def test_score_reports_risk_and_evidence():
    s7 = next(s for s in SEEDS if s.id == "S07")
    r = score(s7, _session([("其他", "無關", "低風險"), ("D/C 不一致", "判定段 0.688 與表 0.788 不符", "中風險")]))
    assert r.detected and r.risk == "中風險" and "0.688" in r.evidence and r.n_items == 2
    assert not score(s7, _session([("其他", "無關", "低風險")])).detected


def test_offline_runner(tmp_path):
    rep = run(CALC, [1], [], True, ["S01", "S03"], out_dir=str(tmp_path), log=lambda *a: None)
    assert rep["offline"] and len(rep["configs"]) == 1
    cfg = rep["configs"][0]
    assert [x["seed"] for x in cfg["runs"][0]["seeds"]] == ["S01", "S03"]
    assert cfg["detect_rate"] is not None
    js = json.loads(open(rep["files"][0], encoding="utf-8").read())
    assert js["seeds"][0]["id"] == "S01"
    md = open(rep["files"][1], encoding="utf-8").read()
    assert "離線示範" in md and "| S01 |" in md
