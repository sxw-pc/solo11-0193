import argparse
import json
import sys
from pathlib import Path
from .store import connect,ingest,report,audit

ROOT=Path(__file__).resolve().parent.parent
COMMANDS={'garden-import':'garden','leaf-import':'leaf','blend-import':'blend'}


def main(argv=None):
    p=argparse.ArgumentParser(prog='teablend');sub=p.add_subparsers(dest='command',required=True)
    for name in [*COMMANDS,'demo','report','audit','init']:
        q=sub.add_parser(name);q.add_argument('--db',default='teablend.sqlite')
        if name in COMMANDS:q.add_argument('--file',required=True)
    a=p.parse_args(argv)
    try:
        c=connect(a.db)
        if a.command in COMMANDS:
            result=ingest(c,COMMANDS[a.command],json.loads(Path(a.file).read_text(encoding='utf-8')))
        elif a.command=='demo':
            steps=[]
            for system,name in [('garden','garden-import-ok.json'),('leaf','leaf-import-ok.json'),('blend','blend-import-ok.json'),
                                ('garden','garden-import-bad.json'),('leaf','leaf-import-bad.json'),('blend','blend-import-bad.json')]:
                x=ingest(c,system,json.loads((ROOT/'fixtures'/name).read_text(encoding='utf-8')))
                steps.append({'fixture':name,**x,'decisions':len(report(c)['decisions'])})
            result={'module':str(Path(__file__).resolve()),'steps':steps,'report':report(c),'audit':audit(c)}
        elif a.command=='report':result=report(c)
        elif a.command=='audit':result=audit(c)
        else:result={'initialized':True,'module':str(Path(__file__).resolve())}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if a.command!='audit' or result['ok'] else 2
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(f'error: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
