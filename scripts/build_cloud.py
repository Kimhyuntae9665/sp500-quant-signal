"""Build only public frontend and sanitized market snapshots for Cloudflare."""
from pathlib import Path
import argparse
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.export_cloud import export_snapshot
from quant_engine.service import SignalService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", choices=("none", "quick", "full"), default="none")
    parser.add_argument("--history-limit", type=int, default=0)
    parser.add_argument("--portfolio-refresh", action="store_true")
    args = parser.parse_args()
    destination = ROOT / "output" / "cloud-site"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "web", destination, dirs_exist_ok=True)
    # Cloud math shares the documented formulas, but does not execute gs-quant.
    lab = destination / "gs-lab.html"
    content = lab.read_text(encoding="utf-8").replace(
        "GS Quant 오픈소스 함수와 Yahoo/yfinance 벤더 데이터를 사용하는 비공식 개인 연구 프로젝트입니다.",
        "클라우드 버전은 공개된 계산식을 JavaScript로 계산하고 Yahoo 편의 데이터를 사용합니다. Python gs-quant를 실행하지 않습니다.",
    )
    lab.write_text(content, encoding="utf-8")
    manifest = export_snapshot(SignalService(), destination / "data", refresh=args.refresh, history_limit=args.history_limit, portfolio_refresh=args.portfolio_refresh)
    print(f"Cloud build: {destination}; {manifest['counts']}")


if __name__ == "__main__":
    main()
