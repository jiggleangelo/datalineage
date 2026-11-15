from collections import deque
from dataclasses import asdict,dataclass,field
from datetime import datetime,timezone
import json,re,time,uuid
from pathlib import Path
_DATASET_ID=re.compile(r'^[A-Za-z0-9][\w.-]{0,95}(?:/[\w.-]{1,96})?$');_NON_TEXT_TAG=re.compile(r'^task_categories:(?:image|audio|video)-',re.I)
@dataclass
class LicenceInfo:raw:str|None;id:str|None;category:str;source:str
@dataclass
class Edge:parent:str;child:str;evidence:str;confidence:str;traversed:bool=True
@dataclass
class Node:id:str;title:str;url:str|None;modality:str;declared:LicenceInfo;detected:list[LicenceInfo];resolved:bool;depth:int
@dataclass
class Flag:code:str;severity:str;node:str;message:str;path:list[str]
@dataclass
class Report:
 report_id:str;created_utc:str;roots:list[str];nodes:dict[str,Node];edges:list[Edge];flags:list[Flag];coverage:dict;limits_hit:list[str];tool_version:str;disclaimer:str;roles:dict[str,str]=field(default_factory=dict)
 def asdict(self):return asdict(self)
def canonical(v):return v if v.startswith(('hf:','ext:','unresolved:','local:')) else 'hf:'+v
def _placeholder(i,l):return Node(i,i.split(':',1)[-1],None,'unknown',LicenceInfo(None,None,'unknown','none'),[],False,l)
def _litigation(path):
 try:v=json.loads(path.read_text())
 except Exception:return []
 return [x for x in v if isinstance(x,dict) and x.get('kind') in {'removed','litigated'} and isinstance(x.get('match',[]),list)] if isinstance(v,list) else []
def report_from_dict(v):
 if not isinstance(v,dict):raise ValueError
 nodes={k:Node(**{**n,'declared':LicenceInfo(**n['declared']),'detected':[LicenceInfo(**x) for x in n.get('detected',[])]}) for k,n in v['nodes'].items()}
 return Report(v['report_id'],v['created_utc'],list(v['roots']),nodes,[Edge(**x) for x in v.get('edges',[])],[Flag(**x) for x in v.get('flags',[])],dict(v['coverage']),list(v.get('limits_hit',[])),v['tool_version'],v['disclaimer'],dict(v.get('roles',{})))
class Resolver:
 def __init__(self,hub,papers=None,aliases=None,litigation=None,max_depth=4,max_nodes=200,time_budget=20):
  root=Path(__file__).resolve().parents[1];self.hub=hub;self.papers=papers
  if aliases is None:
   try:aliases=json.loads((root/'data/aliases.json').read_text())
   except Exception:aliases={}
  self.aliases=aliases if isinstance(aliases,dict) else {};self.litigation=litigation if litigation is not None else _litigation(root/'data/litigation.json');self.max_depth=max(0,int(max_depth));self.max_nodes=max(1,int(max_nodes));self.time_budget=max(.05,float(time_budget))
 def resolve(self,roots,depth=None):
  from .extract import upstream
  from .licences import declared,detect,normalise,strictest
  deadline=time.monotonic()+self.time_budget;maximum=self.max_depth if depth is None else min(max(0,int(depth)),self.max_depth);roots=[canonical(x) for x in roots];q=deque((x,0,(x,)) for x in roots);nodes={};edges=[];expanded=set();limits=[];placeholders=set()
  while q:
   if time.monotonic()>=deadline:limits.append('TIME_LIMIT');break
   if len(expanded)>=self.max_nodes:limits.append('NODE_LIMIT');break
   ident,level,ancestry=q.popleft()
   if ident in expanded:continue
   expanded.add(ident);children=[]
   if ident.startswith('ext:'):
    entry=next((x for x in self.aliases.values() if isinstance(x,dict) and x.get('id')==ident),None)
    if entry:lic=normalise(entry.get('licence') or entry.get('license'),'alias_table') if entry.get('licence') or entry.get('license') else LicenceInfo(None,None,'unknown','alias_table');node=Node(ident,str(entry.get('title') or ident[4:])[:500],entry.get('url') if isinstance(entry.get('url'),str) else None,'text',lic,[],True,level)
    else:node=_placeholder(ident,level)
   elif ident.startswith(('unresolved:','local:')):node=_placeholder(ident,level)
   else:
    did=ident[3:] if ident.startswith('hf:') else ident;doc=self.hub.get_dataset(did) if self.hub and _DATASET_ID.fullmatch(did) else None
    if isinstance(doc,dict):
     for x in doc.get('_limits_hit',[]):
      if isinstance(x,str) and x not in limits:limits.append(x)
     card=doc.get('card_text','') if isinstance(doc.get('card_text',''),str) else '';data=dict(doc.get('card_data') or {}) if isinstance(doc.get('card_data') or {},dict) else {};tags=doc.get('tags',[]) if isinstance(doc.get('tags'),list) else [];other=any(str(x).lower().startswith(('modality:image','modality:audio','modality:video')) or _NON_TEXT_TAG.match(str(x).lower()) for x in tags);title=data.get('pretty_name') if isinstance(data.get('pretty_name'),str) else did;node=Node(ident,title[:500],'https://huggingface.co/datasets/'+did,'other' if other else 'text',declared(data),detect(card),not bool(doc.get('partial')),level);children=[] if other else upstream(card,data,self.aliases,self.papers,limits)
    else:node=_placeholder(ident,level)
   nodes[ident]=node
   if level>=maximum:
    if children and 'DEPTH_LIMIT' not in limits:limits.append('DEPTH_LIMIT')
    for child,e,c in children:
     child=canonical(child);edges.append(Edge(ident,child,e,c,False));nodes.setdefault(child,_placeholder(child,level+1));placeholders.add(child)
    continue
   for child,e,c in children:
    if not isinstance(child,str):continue
    child=canonical(child)
    if child in ancestry:edges.append(Edge(ident,child,e,c,child in nodes));limits.append('CYCLE');continue
    edges.append(Edge(ident,child,e,c,True))
    if child not in expanded:q.append((child,level+1,ancestry+(child,)))
  for ident,level,_ in q:
   if ident not in nodes:nodes[ident]=_placeholder(ident,level);placeholders.add(ident)
  for e in edges:
   if e.child not in nodes or e.child in placeholders:e.traversed=False
  known=sum(bool(n.resolved and (x:=strictest([n.declared]+n.detected)) and x.category not in {'unknown','unlicensed'}) for n in nodes.values());pct=round(100*known/len(nodes),1) if nodes else 0.0;disc='Automated provenance draft. Not legal advice and not a compliance certification. Based only on public dataset cards and abstracts available at generation time; upstream sources may be incomplete or wrong. Coverage: '+str(pct)+'%. Review with qualified counsel before relying on it.';report=Report(uuid.uuid4().hex,datetime.now(timezone.utc).isoformat(),roots,nodes,edges,[],{'nodes':len(nodes),'licence_known':known,'pct':pct,'unresolved':sum(not n.resolved for n in nodes.values())},list(dict.fromkeys(limits)),'0.1.0',disc)
  from .flags import build_flags
  report.flags=build_flags(report,self.litigation);return report
