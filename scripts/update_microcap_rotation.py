from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.microcap_rotation import (initialize_simulation,import_research,update_simulation,save_evidence)
from services.microcap_rotation_policy import trading,now
from services.microcap_rotation_store import process_lock

def main():
    parser=argparse.ArgumentParser(description="微盘20 ABCD收盘后模拟；不连接真实交易")
    parser.add_argument("--target")
    parser.add_argument("--evidence-template",help="生成待核验模板，不标记通过")
    parser.add_argument("--preflight",action="store_true",help="只读预检，不联网不写账")
    parser.add_argument("--refresh",action="store_true",help="仅补必要数据")
    parser.add_argument("--enable",action="store_true",help="显式启用四个独立22万元账户")
    parser.add_argument("--import-research",type=Path)
    parser.add_argument("--evidence",type=Path)
    parser.add_argument("--scheduled",action="store_true")
    args=parser.parse_args()
    try:
        if args.evidence_template:
            from services.microcap_rotation_data import evidence_template
            from core.paths import OUTPUT_DIR
            template=evidence_template(args.evidence_template)
            OUTPUT_DIR.mkdir(parents=True,exist_ok=True)
            dest=OUTPUT_DIR/("microcap_rotation_evidence_template_"+args.evidence_template+".json")
            dest.write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding="utf-8")
            print("待核验模板："+str(dest))
            return 0
        if args.preflight and (args.enable or args.import_research or args.evidence):
            parser.error("只读预检不能同时启用账户或导入数据")
        if args.scheduled and not trading(now().date().isoformat()):
            print("非交易日，已跳过。")
            return 0
        if args.enable:
            initialize_simulation()
        if args.import_research:
            report=import_research(args.import_research)
            print(f"历史档案已导入；18×4项复算，原表差异{len(report['differences'])}项。")
        if args.evidence:
            with process_lock():
                print("证据已保存："+save_evidence(args.evidence.read_text(encoding="utf-8-sig")))
        if args.import_research and not (args.enable or args.target or args.refresh or args.preflight or args.scheduled):
            return 0
        result=update_simulation(target=args.target,preflight=args.preflight,refresh=args.refresh)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result.get("gaps") else 0
    except Exception as exc:
        print("更新失败："+str(exc),file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
