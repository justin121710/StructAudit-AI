import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 規範原文 PDF：有本機的 rc-member-designer 時直接使用；測試一律不從網路下載
RCMD = Path(os.getenv("RCMD_DIR", "/home/user/justin121710/rc-member-designer"))
if (RCMD / "specs").is_dir():
    os.environ.setdefault("SPEC_PDF_DIR", str(RCMD))
os.environ["STRUCTAUDIT_OFFLINE"] = "1"
