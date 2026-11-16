"""Synthetic local scaling benchmark; never connects MT5 or modifies source tree."""
import sys,argparse
ap=argparse.ArgumentParser();ap.add_argument('repo');ap.add_argument('--sizes',default='0,500,2000');ap.add_argument('--active',action='store_true');args=ap.parse_args()
sys.path[:0]=[args.repo,args.repo+'/tests']
from test_tradfi_paper import Reader
from tradfi.paper import PaperSession
from pathlib import Path
import tempfile,json,time,statistics
out=[]
for n in map(int,args.sizes.split(',')):
 with tempfile.TemporaryDirectory() as d:
  r=Reader();s=PaperSession(Path(d)/'p.db',r,clock=lambda:r.now,monotonic=lambda:r.now);s.step()
  if args.active:
   s.action(dict(type='command',text='/start'));r.tick('4000','4000.10',seconds=30,bar=('3999','4000'))
   for _ in range(3):s.step()
   r.tick('4000.9','4001')
   for _ in range(3):s.step()
  p=s.exchange.p
  for i in range(n):
   cid='old-c'+str(i);oid='old-o'+str(i);pid='old-p'+str(i)
   p['order_history'].append(dict(id=oid,command_id=cid,status='FILLED',side='BUY',size='.01',price='4000',sl='3999',tp='4001',owned=True))
   for ent in ['IN','OUT']:
    p['deals'].append(dict(id=ent+pid,position_id=pid,order_id=oid,command_id=cid if ent=='IN' else '',entry=ent,side='BUY',volume='.01',price='4000',reason='EXPERT',profit='0',commission='0',swap='0',fee='0',sl='3999',tp='4001',time=r.now-3600))
   p['results'][cid]=dict(status='FILLED',order_id=oid)
   s.controller.state['commands'][cid]=dict(id=cid,kind='STOP',status='FILLED',side='BUY',size='.01',price='4000',sl='3999',tp='4001',result=p['results'][cid],order_id=oid)
  s.exchange.persist();s.controller.save('fixture')
  def bench(f):
   ts=[]
   for j in range(3):
    st=time.perf_counter();f();ts.append((time.perf_counter()-st)*1000)
   return round(statistics.median(ts),3)
  row=dict(n=n,active=args.active,snapshot_ms=bench(s.exchange.snapshot),persist_ms=bench(s.exchange.persist),view_ms=bench(s.view))
  def step():r.tick('4000.9','4001') if args.active else r.tick('4000','4000.10');s.step()
  row['step_ms']=bench(step);row['phase']=s.controller.state['phase'];row['positions']=len(p['positions']);row['orders']=len(p['orders']);out.append(row)
  s.close()
print(json.dumps(out,indent=2))
