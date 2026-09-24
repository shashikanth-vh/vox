"""Rebuild Chitti SVG/PNG diagrams and self-contained HTML.

Run from any directory: python3 docs/chitti/render_architecture.py
Requires Pillow and Python-Markdown. SVG files remain directly editable;
update this script for changes that must survive regeneration.
"""
from pathlib import Path
import base64
import html
import re
import xml.etree.ElementTree as ET

import markdown
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
DOC = HERE / 'ARCHITECTURE.md'
IMAGES = HERE / 'images'
INK = '#172b4d'
BLUE = '#deebff'
TEAL = '#e3f5f1'
GRAY = '#f4f5f7'
AMBER = '#fff3d6'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'


class Diagram:
    def __init__(self, name, width, height, title, subtitle, scale=1):
        self.name = name
        self.scale = scale
        self.image = Image.new('RGB', (width * scale, height * scale), 'white')
        self.draw = ImageDraw.Draw(self.image)
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
                      f'<title>{html.escape(title)}</title>',
                      '<rect width="100%" height="100%" fill="white"/>']
        self.text(width / 2, 35, title, 28)
        self.text(width / 2, 74, subtitle, 17)

    def text(self, x, y, value, size=18, color=INK, anchor='middle'):
        font = ImageFont.truetype(FONT, size * self.scale)
        for i, line in enumerate(value.split('\n')):
            yy = y + i * (size + 7)
            self.parts.append(f'<text x="{x}" y="{yy}" text-anchor="{anchor}" dominant-baseline="middle" font-family="DejaVu Sans, sans-serif" font-size="{size}" fill="{color}">{html.escape(line)}</text>')
            self.draw.text((x * self.scale, yy * self.scale), line, font=font, fill=color, anchor='mm' if anchor == 'middle' else 'lm')

    def box(self, x, y, w, h, label='', fill=BLUE, size=18):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="#8993a4"/>')
        self.draw.rounded_rectangle(tuple(v * self.scale for v in (x, y, x+w, y+h)), radius=10 * self.scale, fill=fill, outline='#8993a4', width=self.scale)
        if label:
            n = len(label.split('\n'))
            self.text(x+w/2, y+h/2-(n-1)*(size+7)/2, label, size)

    def line(self, points, arrow=True, color='#526581', dashed=False):
        coords = ' '.join(f'{x},{y}' for x,y in points)
        dash = ' stroke-dasharray="6 6"' if dashed else ''
        self.parts.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"{dash}/>')
        if dashed:
            # Dashed lines are used only for vertical lifelines.
            x, y = points[0]
            end = points[-1][1]
            for yy in range(int(y), int(end), 12):
                self.draw.line(tuple(v * self.scale for v in (x, yy, x, min(yy+6, end))), fill=color, width=2 * self.scale)
        else:
            self.draw.line([(x * self.scale, y * self.scale) for x, y in points], fill=color, width=2 * self.scale)
        if arrow:
            import math
            x,y=points[-1]; px,py=points[-2]
            angle=math.atan2(y-py,x-px)
            tri=[(x,y),(x-12*math.cos(angle-.45),y-12*math.sin(angle-.45)),(x-12*math.cos(angle+.45),y-12*math.sin(angle+.45))]
            self.parts.append(f'<polygon points="{" ".join(f"{a},{b}" for a,b in tri)}" fill="{color}"/>')
            self.draw.polygon([(x * self.scale, y * self.scale) for x, y in tri], fill=color)

    def save(self):
        svg='\n'.join(self.parts+['</svg>'])
        ET.fromstring(svg)
        (IMAGES/f'{self.name}.svg').write_text(svg)
        raster = self.image
        if self.scale > 1:
            raster = raster.resize((raster.width // self.scale, raster.height // self.scale), Image.Resampling.LANCZOS)
        raster.save(IMAGES/f'{self.name}.png')


def context():
    d=Diagram('system-context',1400,850,'Chitti — system context','Arrows show requests and dependencies; responses return on the same connections')
    for x,y,w,h,label,fill in [
        (475,115,450,65,'PRISM browser · user bearer token',BLUE),
        (475,240,450,65,'Services Nginx / Gateway',BLUE),
        (1040,240,310,65,'Access · permissions',TEAL),
        (450,390,500,100,'Chitti pipeline\nLocal embedding and reranking models',BLUE),
        (40,400,315,85,'External model provider\nModel inference',AMBER),
        (1040,400,310,85,'Qdrant\nBusiness-definition index',TEAL),
        (475,595,450,70,'Register · authorized GET APIs',TEAL),
        (475,745,450,65,'Register PostgreSQL · ledger records',TEAL)]:
        d.box(x,y,w,h,label,fill)
    d.line([(700,180),(700,240)]);d.text(800,210,'HTTPS',16)
    d.line([(925,272),(1040,272)])
    d.line([(700,305),(700,390)]);d.box(505,331,390,34,'HTTPS via Chitti Nginx + signed context','white',17)
    d.line([(450,440),(355,440)])
    d.line([(950,440),(1040,440)])
    d.line([(700,490),(700,595)]);d.box(470,528,460,34,'HTTPS via services Nginx /machine + signed GET','white',17)
    d.line([(700,665),(700,745)]);d.text(800,705,'Data access',16)
    d.save()


def flow():
    d=Diagram('request-flow',1600,1720,'Chitti — request flow','Logical calls; Nginx proxies omitted; provider calls and paginated reads condensed')
    lanes=[130,380,650,920,1170,1450]
    labels=['Browser','Gateway / Access','Chitti','Register','Qdrant','Model provider']
    for x,label in zip(lanes,labels):
        d.box(x-112,110,224,60,label,TEAL if label in ['Register','Qdrant'] else AMBER if label=='Model provider' else BLUE,18)
        d.line([(x,175),(x,1570)],arrow=False,dashed=True,color='#c1c7d0')
    def msg(a,b,y,label):
        x1,x2=lanes[a],lanes[b]
        d.line([(x1,y),(x2,y)])
        # White backing keeps labels legible across lifelines.
        size=17
        width=d.draw.textlength(label,font=ImageFont.truetype(FONT,size))+18
        d.box((x1+x2-width)/2,y-35,width,27,'','white')
        d.text((x1+x2)/2,y-21,label,size)
    def note(y,text,fill=GRAY):
        d.box(45,y,1510,47,text,fill,17)
    msg(0,1,225,'Chat + bearer token')
    note(248,'Gateway authenticates the user and resolves permissions through Access')
    msg(1,2,337,'Signed call; Chitti verifies')
    msg(2,5,395,'1–2. Resolve conversation and interpret question')
    msg(2,4,453,'3. Search ontology using local retrieval models')
    msg(4,2,511,'Ontology candidates; Chitti reranks locally')
    msg(2,3,569,'4. Read visible candidates')
    msg(3,2,627,'Candidates + reference values')
    msg(2,5,685,'4–5. Ground values and assess answerability')
    note(711,'EARLY EXIT: clarification required or out of scope → return via Gateway; skip stages 6–10',AMBER)
    msg(2,5,802,'6. For answerable requests: generate structured plan')
    note(825,'Chitti validates all planned resources, filters, operations, and dependencies before execution reads')
    msg(2,3,915,'7. Authorized bounded GETs')
    msg(3,2,973,'Records; Chitti tracks coverage')
    note(997,'Chitti executes deterministic Python operations and tracks row lineage and completeness')
    note(1058,'OPTIONAL STAGE 8: only for qualitative requests; otherwise continue to evidence construction',TEAL)
    msg(2,5,1151,'Analyze bounded authorized source-text corpus')
    msg(5,2,1209,'Findings → Chitti validates citations and coverage')
    note(1233,'9. Chitti constructs evidence: facts, rows, contributing records, missing values, and caveats')
    msg(2,5,1328,'10. Generate prose from evidence')
    msg(5,2,1386,'Answer → Chitti applies public presentation controls')
    msg(2,1,1452,'Final answer + metadata')
    msg(1,0,1513,'Answer + evidence + tables')
    note(1545,'SSE: progress during execution; completed answer, outcome metadata, and [DONE] at completion',TEAL)
    note(1605,'FAILURES: invalid identity denies access; stage errors/timeouts return failures via Gateway when connected',AMBER)
    d.text(800,1681,'An internal answer-generation fallback is withheld by Gateway business presentation; the public outcome is FAILED.',17)
    d.save()


def deployment():
    d=Diagram('deployment',1600,1350,'Chitti — two-instance deployment',
              'HTTPS between instances · HTTP within each container network',scale=3)
    d.box(40,215,1520,295,'',GRAY)
    d.box(40,795,1520,400,'',GRAY)
    d.text(70,245,'PRISM services instance',23,anchor='start')
    d.text(70,830,'Chitti instance',23,anchor='start')

    d.box(440,115,260,65,'Browser / PRISM UI',BLUE,18)
    d.box(440,290,260,80,'Services Nginx\nHTTPS :443 / :8443',BLUE,20)
    d.box(100,290,220,80,'Register\nInternal HTTP :8000',TEAL,18)
    d.box(1080,290,320,80,'Gateway',BLUE,20)
    d.box(100,425,220,60,'Register PostgreSQL',TEAL,17)
    d.box(770,425,250,60,'Access · permissions',TEAL,18)

    d.box(440,880,260,80,'Chitti\nInternal HTTP :8000',BLUE,20)
    d.box(1080,880,320,80,'Chitti Nginx\nPrivate HTTPS :8443',BLUE,20)
    d.box(110,1060,250,80,'Qdrant\nInternal HTTP :6333',TEAL,18)
    d.box(110,1150,250,30,'Persistent data volume',TEAL,15)
    d.box(445,1060,250,80,'Model cache\nRead-only serving mount',TEAL,17)
    d.box(885,1050,570,95,'Preparation\nModel preload + Qdrant ready\n→ ontology indexing → Chitti startup',AMBER,18)

    # Internal paths stay within their instance. Inter-instance routes are
    # straight vertical arrows in separate columns, with labels beside them.
    d.line([(570,180),(570,290)])
    d.text(640,222,'HTTPS',17)
    d.line([(440,330),(320,330)])
    d.text(380,307,'HTTP /v1/…',16)
    d.line([(210,370),(210,425)])
    d.line([(700,330),(1080,330)])
    d.text(890,302,'HTTP /chitti/v1/…',18)
    d.line([(1120,370),(1120,455),(1020,455)])

    d.line([(570,880),(570,370)],color='#00875a')
    d.box(70,540,460,220,'', 'white')
    d.text(95,569,'CHITTI → REGISTER',17,color='#00875a',anchor='start')
    d.text(95,605,'https://services.example.com:8443',19,anchor='start')
    d.text(95,638,'/machine/v1/…',23,anchor='start')
    d.text(95,677,'Register key + signed GET context',17,anchor='start')
    d.text(95,708,'Nginx strips /machine before Register',17,anchor='start')
    d.text(95,739,'Readiness: /machine/readyz',16,anchor='start')

    d.line([(1240,370),(1240,880)])
    d.box(660,540,530,220,'','white')
    d.text(685,569,'GATEWAY → CHITTI',17,anchor='start')
    d.text(685,605,'https://chitti.example.com:8443',19,anchor='start')
    d.text(685,638,'/v1/…',23,anchor='start')
    d.text(685,677,'Chitti key + signed caller context',17,anchor='start')
    d.text(685,708,'Gateway strips /chitti before forwarding',17,anchor='start')
    d.text(685,739,'Readiness: /readyz at Chitti Nginx',16,anchor='start')

    d.line([(1080,920),(700,920)])
    d.text(890,892,'HTTP /v1/…',18)
    d.line([(570,960),(570,1060)])
    d.line([(440,920),(235,920),(235,1060)])
    d.text(325,1010,'Ontology search',16)
    d.line([(235,1140),(235,1150)])

    d.text(800,1240,'Both instances can publish port 8443 independently. Application and Qdrant ports stay internal.',18)
    d.text(800,1280,'TLS clients verify hostnames and retain system roots alongside mounted CA trust.',18)
    d.text(800,1320,'Checked-in configuration · live deployment not verified',16)
    d.save()


def document():
    source=DOC.read_text()
    body=markdown.markdown(source,extensions=['tables','fenced_code'])
    def embed(match):
        path=DOC.parent/match[1]
        data=base64.b64encode(path.read_bytes()).decode()
        return f'src="data:image/png;base64,{data}"'
    body=re.sub(r'src="([^\"]+\.png)"',embed,body)
    style='''body{font-family:Arial,Helvetica,sans-serif;line-height:1.55;color:#172b4d;max-width:1120px;margin:40px auto;padding:0 24px}h1,h2,h3{line-height:1.25}h2{margin-top:36px;border-bottom:1px solid #dfe1e6;padding-bottom:8px}table{border-collapse:collapse;width:100%;margin:20px 0;font-size:14px}th,td{border:1px solid #dfe1e6;padding:10px 12px;text-align:left;vertical-align:top}th{background:#f4f5f7}code{font-family:Consolas,monospace}img{display:block;max-width:100%;height:auto;margin:24px auto}li{margin-bottom:5px}@media print{body{max-width:none;margin:0;padding:0;font-size:10pt}h2,h3{break-after:avoid}tr,img{break-inside:avoid}}'''
    DOC.with_suffix('.html').write_text('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Chitti Chatbot — Technical Architecture</title><style>'+style+'</style></head><body>\n'+body+'\n</body></html>\n')


if __name__=='__main__':
    IMAGES.mkdir(exist_ok=True)
    context()
    flow()
    deployment()
    document()
    print('Generated three SVG/PNG diagram pairs and self-contained architecture HTML.')
