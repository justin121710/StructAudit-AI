"""產生示範用結構摘要 Excel：python examples/make_sample.py"""

from pathlib import Path

import pandas as pd

OUT = Path(__file__).with_name("sample_structure.xlsx")

drift = pd.DataFrame({
    "樓層": ["RF", "8F", "7F", "6F", "5F", "4F", "3F", "2F"],
    "樓高(m)": [3.2, 3.2, 3.2, 3.2, 3.2, 3.2, 3.6, 4.5],
    "樓層剪力 Vx (tf)": [128, 245, 348, 436, 510, 569, 612, 641],
    "層間位移角 X": [0.0021, 0.0029, 0.0034, 0.0038, 0.0041, 0.0044, 0.0047, 0.0056],
})
beams = pd.DataFrame({
    "樓層": ["2F", "2F", "3F", "3F", "5F", "RF"],
    "構件編號": ["B1", "G3", "B1", "G3", "G5", "B2"],
    "斷面(cm)": ["40x70", "50x80", "40x70", "50x80", "45x75", "35x60"],
    "配筋比 (%)": [1.42, 2.71, 1.38, 2.12, 1.65, 0.85],
    "應力比 D/C": [0.82, 0.97, 0.79, 0.91, 0.88, 0.55],
})
columns = pd.DataFrame({
    "樓層": ["1F", "1F", "2F", "4F", "6F", "8F"],
    "構件編號": ["C1", "C2", "C1", "C1", "C3", "C3"],
    "斷面(cm)": ["80x80", "70x70", "80x80", "70x70", "60x60", "60x60"],
    "主筋比 (%)": [2.45, 3.10, 2.10, 1.65, 0.92, 1.05],
    "軸力比": [0.38, 0.42, 0.33, 0.27, 0.18, 0.09],
    "應力比 D/C": [0.91, 1.06, 0.84, 0.72, 0.61, 0.43],
})
combos = pd.DataFrame({
    "編號": ["U1", "U2", "U3", "U4", "U5", "U6"],
    "載重組合": ["1.4D", "1.2D+1.6L", "1.2D+1.0L+1.0EX", "1.2D+1.0L-1.0EX",
             "1.2D+1.0L+1.0EY", "1.2D+1.0L-1.0EY"],
})
info = pd.DataFrame({
    "項目": ["結構系統", "混凝土 fc' (kgf/cm2)", "鋼筋 fy (kgf/cm2)", "地震分區", "用途係數 I", "基礎型式"],
    "內容": ["RC 特殊抗彎矩構架", 350, 4200, "臺北盆地 T1 微分區", 1.0, "筏式基礎（模型採固接）"],
})

with pd.ExcelWriter(OUT) as w:
    info.to_excel(w, sheet_name="設計參數", index=False)
    drift.to_excel(w, sheet_name="層間位移角", index=False)
    beams.to_excel(w, sheet_name="梁檢核", index=False)
    columns.to_excel(w, sheet_name="柱檢核", index=False)
    combos.to_excel(w, sheet_name="載重組合", index=False)
print(f"已產生 {OUT}")
