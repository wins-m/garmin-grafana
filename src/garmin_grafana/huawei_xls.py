"""Minimal read-only CFB/BIFF8 extraction for Huawei export .xls tables."""
import struct,json,pathlib,collections,argparse
U=lambda b,o=0:struct.unpack_from('<I',b,o)[0]
H=lambda b,o=0:struct.unpack_from('<H',b,o)[0]

def workbook(path):
 b=path.read_bytes();size=1<<H(b,30); mini=1<<H(b,32)
 sec=lambda i:b[(i+1)*size:(i+2)*size]
 difat=list(struct.unpack_from('<109I',b,76)); n=U(b,72); cur=U(b,68)
 for _ in range(n):
  d=sec(cur);difat.extend(struct.unpack('<%dI'%(size//4-1),d[:-4]));cur=U(d,size-4)
 fat=[]
 for i in difat:
  if i<0xfffffffa:fat.extend(struct.unpack('<%dI'%(size//4),sec(i)))
 def chain(start,table,get):
  chunks=[];seen=set()
  while start<0xfffffffa:
   assert start not in seen;seen.add(start);chunks.append(get(start));start=table[start]
  return b''.join(chunks)
 directory=chain(U(b,48),fat,sec); entries=[]
 for p in range(0,len(directory),128):
  d=directory[p:p+128];ln=H(d,64)
  if ln:entries.append((d[:ln-2].decode('utf-16le'),d[66],U(d,116),struct.unpack_from('<Q',d,120)[0]))
 root=next(e for e in entries if e[1]==5);ministream=chain(root[2],fat,sec)[:root[3]]
 mf=chain(U(b,60),fat,sec) if U(b,64) else b'';minifat=list(struct.unpack('<%dI'%(len(mf)//4),mf))
 e=next(e for e in entries if e[0] in ('Workbook','Book'))
 if e[3]<U(b,56):return chain(e[2],minifat,lambda i:ministream[i*mini:(i+1)*mini])[:e[3]]
 return chain(e[2],fat,sec)[:e[3]]

def records(b):
 p=0
 while p+4<=len(b):
  code,n=struct.unpack_from('<HH',b,p);yield p,code,b[p+4:p+4+n];p+=4+n

class Chunks:
 def __init__(self,chunks):self.chunks=chunks;self.i=0;self.p=0
 def read(self,n):
  out=b''
  while n:
   if self.p==len(self.chunks[self.i]):self.i+=1;self.p=0
   k=min(n,len(self.chunks[self.i])-self.p);out+=self.chunks[self.i][self.p:self.p+k];self.p+=k;n-=k
  return out
 def string(self):
  n=H(self.read(2));flags=self.read(1)[0];rich=H(self.read(2)) if flags&8 else 0;extra=U(self.read(4)) if flags&4 else 0;wide=bool(flags&1);parts=[]
  while n:
   if self.p==len(self.chunks[self.i]):self.i+=1;self.p=0;wide=bool(self.read(1)[0]&1)
   k=min(n,(len(self.chunks[self.i])-self.p)//(2 if wide else 1));raw=self.read(k*(2 if wide else 1));parts.append(raw.decode('utf-16le' if wide else 'latin1'));n-=k
  self.read(rich*4+extra);return ''.join(parts)

def rk(v):
 if v&2:r=v>>2;r=r-(1<<30) if r&(1<<29) else r
 else:r=struct.unpack('<d',struct.pack('<II',0,v&0xfffffffc))[0]
 return r/100 if v&1 else r

def extract(path):
 rec=list(records(workbook(path)));sst=[];sheets=[]
 for i,(p,c,d) in enumerate(rec):
  if c==0x85:
   n=d[6];sheets.append((U(d),d[8:8+n*(2 if d[7]&1 else 1)].decode('utf-16le' if d[7]&1 else 'latin1')))
  if c==0xfc:
   chunks=[d];j=i+1
   while j<len(rec) and rec[j][1]==0x3c:chunks.append(rec[j][2]);j+=1
   reader=Chunks(chunks);total=U(reader.read(4));unique=U(reader.read(4));sst=[reader.string() for _ in range(unique)]
 result=[]
 for offset,name in sheets:
  cells={};started=False;counts=collections.Counter()
  for p,c,d in rec:
   if p<offset:continue
   if c==0xA:break
   counts[c]+=1
   if c in (0xfd,0x203,0x27e,0x204,0x205):
    r,col,xf=struct.unpack_from('<HHH',d);v=None
    if c==0xfd:v=sst[U(d,6)]
    elif c==0x203:v=struct.unpack_from('<d',d,6)[0]
    elif c==0x27e:v=rk(U(d,6))
    elif c==0x204:v=d[8:8+H(d,6)].decode('latin1')
    elif c==0x205:v=bool(d[6]) if d[7]==0 else {'excel_error':d[6]}
    cells[(r,col)]=v
   elif c==0xbd:
    r,col=struct.unpack_from('<HH',d)
    for j in range((len(d)-6)//6):cells[(r,col+j)]=rk(U(d,6+j*6))
  maxrow=max((r for r,c in cells),default=-1);maxcol=max((c for r,c in cells),default=-1)
  rows=[[cells.get((r,c)) for c in range(maxcol+1)] for r in range(maxrow+1)]
  result.append({'sheet':name,'rows':rows,'record_types':{hex(k):v for k,v in counts.items()}})
 return result

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('source',type=pathlib.Path)
 parser.add_argument('output',type=pathlib.Path)
 args=parser.parse_args()
 data=extract(args.source)
 if args.source.name=='user device info.xls':
   for sheet in data:
    for row in sheet['rows'][1:]:row[0]=None
 args.output.write_text(json.dumps(data,ensure_ascii=False,indent=2))
