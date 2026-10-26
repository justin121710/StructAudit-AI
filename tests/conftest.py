import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 規範原文 PDF：有本機的 rc-member-designer 時直接使用；測試一律不從網路下載
RCMD = Path(os.getenv("RCMD_DIR", "/home/user/justin121710/rc-member-designer"))
if (RCMD / "specs").is_dir():
    os.environ.setdefault("SPEC_PDF_DIR", str(RCMD))
os.environ["STRUCTAUDIT_OFFLINE"] = "1"


@pytest.fixture(scope="session")
def scanned_pdf() -> bytes:
    """沒有文字層的 PDF：把示範計算書前 3 頁轉成圖片再存成 PDF（模擬掃描或以影像方式列印）。"""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(Path(__file__).resolve().parent.parent / "examples" / "sample_C1_calc.pdf"))
    imgs = [doc[i].render(scale=1.0).to_pil().convert("RGB") for i in range(3)]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:], resolution=72)
    return buf.getvalue()
