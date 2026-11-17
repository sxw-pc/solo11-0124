"""Command line entry points for three business subsystems."""
import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .adoption import adopt
from .assessment import assess
from .settlement import settle
from .report import report

def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest="command",required=True)
    i=sub.add_parser("init");i.add_argument("--db",required=True)
    for name in ("adopt","assess","settle"):
        cmd=sub.add_parser(name);cmd.add_argument("--db",required=True);cmd.add_argument("--input",required=True)
    r=sub.add_parser("report");r.add_argument("--db",required=True);r.add_argument("--out")
    d=sub.add_parser("demo");d.add_argument("--workdir")
    sc=sub.add_parser("slot-config");sc.add_argument("--db",required=True);sc.add_argument("--slot",required=True);sc.add_argument("--section",required=True);sc.add_argument("--capacity",required=True,type=int)
    sh=sub.add_parser("slot-claim");sh.add_argument("--db",required=True);sh.add_argument("--id",required=True);sh.add_argument("--slot",required=True);sh.add_argument("--booking",required=True);sh.add_argument("--tier",required=True);sh.add_argument("--people",required=True,type=int)
    sr=sub.add_parser("slot-release");sr.add_argument("--db",required=True);sr.add_argument("--id",required=True)
    a=p.parse_args()
    if a.command=="init":
        db=connect(a.db);db.close();print(a.db)
    elif a.command=="adopt":print(json.dumps(adopt(a.db,a.input),ensure_ascii=False))
    elif a.command=="assess":print(json.dumps(assess(a.db,a.input),ensure_ascii=False))
    elif a.command=="settle":print(json.dumps(settle(a.db,a.input),ensure_ascii=False))
    elif a.command=="report":print(json.dumps(report(a.db,a.out),ensure_ascii=False))
    elif a.command=="slot-config":
        from .slots import configure
        print(configure(a.db,a.slot,a.section,a.capacity))
    elif a.command=="slot-claim":
        from .slots import claim
        print(claim(a.db,a.id,a.slot,a.booking,a.tier,a.people))
    elif a.command=="slot-release":
        from .slots import release
        print(release(a.db,a.id))
    else:
        work=Path(a.workdir or tempfile.mkdtemp(prefix="source-case-"));work.mkdir(parents=True,exist_ok=True)
        fixture=Path(__file__).resolve().parent.parent/"fixtures"
        db=work/"state.db"
        config=__import__(__package__+".policy",fromlist=["DEMO"]).DEMO
        print(adopt(db,fixture/config["adopt"]))
        print(assess(db,fixture/config["assess"]))
        print(settle(db,fixture/config["settle"]))
        print(work/"report.json")
        report(db,work/"report.json")
if __name__=="__main__":main()

