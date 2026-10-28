"""Inert HTML text/table evidence. Never renders, executes, or fetches URLs."""
from html.parser import HTMLParser
import re


class HtmlEvidence(HTMLParser):
    def __init__(self,budget,max_text,max_cells):
        super().__init__(convert_charrefs=True)
        self.budget,self.max_text,self.max_cells=budget,max_text,max_cells
        self.body=False;self.skip=[];self.text=[];self.characters=0;self.nodes=0
        self.tables=[];self.table=None;self.row=None;self.cell=None;self.cells=0
        self.table_ambiguous=False;self.images=0;self.dynamic=False;self.hidden=False
    def handle_starttag(self,tag,attrs):
        self.budget.check();self.nodes+=1
        if self.nodes>200000:raise ValueError('html_node_budget')
        attrs=dict(attrs)
        if tag=='body':self.body=True
        if tag in ('script','style','template','head','noscript'):
            self.skip.append(tag)
            if tag in ('script','noscript','template'):self.dynamic=True
        if self.skip or not self.body:return
        if 'hidden' in attrs or attrs.get('aria-hidden')=='true' or any(v in attrs.get('style','').replace(' ','').lower() for v in ('display:none','visibility:hidden')):
            self.hidden=True
        if tag in ('p','div','br','li','tr','td','th','h1','h2','h3'):self._text(' ')
        if tag=='img':self.images+=1
        if tag=='table':
            if self.table is not None:self.table_ambiguous=True
            else:self.table=[]
        elif tag=='tr' and self.table is not None:
            if self.row is not None:self.table_ambiguous=True
            self.row=[]
        elif tag in ('td','th') and self.table is not None:
            if self.cell is not None or attrs.get('rowspan','1')!='1' or attrs.get('colspan','1')!='1':self.table_ambiguous=True
            self.cell=[]
    def handle_startendtag(self,tag,attrs):self.handle_starttag(tag,attrs);self.handle_endtag(tag)
    def handle_endtag(self,tag):
        if self.skip:
            if self.skip[-1]==tag:self.skip.pop()
            return
        if not self.body:return
        if tag in ('td','th') and self.table is not None and self.cell is not None:
            self.cells+=1
            if self.cells>self.max_cells:raise ValueError('html_cell_budget')
            if self.row is None:self.table_ambiguous=True
            else:self.row.append(' '.join(''.join(self.cell).split()))
            self.cell=None
        elif tag=='tr' and self.table is not None and self.row is not None:
            self.table.append(self.row);self.row=None
        elif tag=='table' and self.table is not None:
            if self.row is not None or self.cell is not None:self.table_ambiguous=True
            self.tables.append(self.table);self.table=None
        if tag in ('p','div','li','tr','td','th','h1','h2','h3'):self._text(' ')
        if tag=='body':self.body=False
    def _text(self,value):
        self.characters+=len(value)
        if self.characters>self.max_text:raise ValueError('html_text_budget')
        self.text.append(value)
        if self.cell is not None:self.cell.append(value)
    def handle_data(self,value):
        if self.skip and self.skip[-1]=='style' and re.search(r'display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|content\s*:|clip\s*:',value,re.I):
            self.hidden=True
        if self.body and not self.skip:
            self._text(value)


def summarize_html(path,budget,decode,value,max_text,max_cells):
    from .html_resources import inspect_dependencies
    from .core import _LocalHtmlResources
    reason=inspect_dependencies(path,_LocalHtmlResources,cancel=budget.cancel)
    if reason:raise ValueError(reason)
    with path.open('rb') as stream:raw=budget.read(stream,max_text*4)
    declaration=re.search(br'<meta\b[^>]{0,2048}\bcharset\s*=\s*["\x27]?\s*([A-Za-z0-9_-]+)',raw[:8192],re.I)
    if declaration and not raw.startswith((b'\xff\xfe',b'\xfe\xff',b'\xef\xbb\xbf')):
        encoding=declaration.group(1).decode('ascii').lower()
        if encoding not in ('utf-8','utf8','windows-1251','cp1251','windows-1252','iso-8859-1'):
            raise ValueError('html_declared_encoding_not_inspected')
        text=raw.decode(encoding,'strict')
    else:text=decode(raw,'html')
    parser=HtmlEvidence(budget,max_text,max_cells);parser.feed(text);parser.close()
    if parser.rawdata or parser.skip or parser.table is not None:raise ValueError('html_incomplete_markup')
    grids=[]
    if not(parser.table_ambiguous or parser.hidden or parser.dynamic):
        grids=[{'name':'HTML table '+str(i+1),'rows':[[value(c) for c in row] for row in rows]}
               for i,rows in enumerate(parser.tables)]
    unknown=['html_css_layout','html_text_visibility','html_embedded_resource_rendering']
    if parser.table_ambiguous:unknown.append('html_complex_table_mapping')
    if parser.dynamic:unknown.append('html_dynamic_content')
    if parser.hidden:unknown.append('html_hidden_content')
    return {'kind':'html','documentClass':'text','complete':True,'_text':' '.join(''.join(parser.text).split()),
        '_text_complete':not(parser.dynamic or parser.hidden),'_grids':grids,
        'counts':{'tables':len(parser.tables),'images':parser.images},'textCoverage':['html_body_markup'],
        'unknown':unknown}
