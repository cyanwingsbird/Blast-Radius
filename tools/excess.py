import json,re,sys
sys.path.insert(0,'/home/claude/work/repo'); import selector
S,OUT=sys.argv[1],sys.argv[2]
T=json.load(open('/home/claude/sample/sample/tests.json'))['tests']; ms={t['number']:round(t['seconds']*1000) for t in T}
A=json.load(open(S+'/answers.json'))['commits']
L=open(OUT+'.err').read().split('\n')
cpu={}
for l in L:
    m=re.match(r'ran (\S+): (\d+)/(\d+) in \S+ cpu (\S+)',l)
    if m: cpu[m.group(1)]=float(m.group(4))
rows=[]
for l in open(OUT):
    d=json.loads(l); w=[x for x in d['workers'] if x]
    c=(max(sum(ms[n] for n in b) for b in w)+2000*len(w))/1000 if w else 0
    f=set(A[d['commit']]['failing']); kept={x for b in w for x in b}
    o=selector.best_split({n:ms[n] for n in f})[0]/1000
    miss=bool(f-kept)
    rows.append((round(c-o+cpu.get(d['commit'],0)+300*miss,2),d['commit'],round(c,2),round(o,2),cpu.get(d['commit'],0),len(kept),len(f)))
rows.sort(reverse=True)
print('excess %.1f sim %.1f oracle %.1f cpu %.1f'%(sum(r[0] for r in rows),sum(r[2] for r in rows),sum(r[3] for r in rows),sum(r[4] for r in rows)))
for r in rows[:int(sys.argv[3]) if len(sys.argv)>3 else 10]: print(r)
print([l for l in L if 'warm' in l or 'done' in l])
