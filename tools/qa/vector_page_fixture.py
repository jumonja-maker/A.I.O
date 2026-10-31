"""Independent, deterministic three-page PPTX for native page acceptance.

Only stdlib creates these QA inputs. LibreOffice never creates or repairs this
fixture, so a PPTX export defect cannot redefine the expected source colours.
"""
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

TOKENS = tuple('AIO_VECTOR_PAGE_%02d_ONLY' % n for n in (1,2,3))
COLOURS = ('FF0000','0000FF','00AA00')
A='http://schemas.openxmlformats.org/drawingml/2006/main'
P='http://schemas.openxmlformats.org/presentationml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS='xmlns:a="'+A+'" xmlns:p="'+P+'" xmlns:r="'+R+'"'
GROUP=('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
       '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
       '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')


def _rels(items):
    return ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+
            ''.join('<Relationship Id="rId%d" Type="%s/%s" Target="%s"/>' %
                    (i+1,R,kind,target) for i,(kind,target) in enumerate(items))+'</Relationships>')


def write_pptx_fixture(destination):
    parts={}
    overrides=[('ppt/presentation.xml','presentation'),
               ('ppt/slideMasters/slideMaster1.xml','slideMaster'),
               ('ppt/slideLayouts/slideLayout1.xml','slideLayout')]
    overrides.extend(('ppt/slides/slide%d.xml'%i,'slide') for i in (1,2,3))
    parts['[Content_Types].xml']=(
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'+''.join(
        '<Override PartName="/%s" ContentType="application/vnd.openxmlformats-officedocument.presentationml.%s+xml"/>'
        %(path,kind+'.main' if kind=='presentation' else kind) for path,kind in overrides)+
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '</Types>')
    parts['_rels/.rels']=_rels([('officeDocument','ppt/presentation.xml')])
    parts['ppt/presentation.xml']=(
        '<p:presentation '+NS+'><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/>'
        '</p:sldMasterIdLst><p:sldIdLst>'+''.join(
        '<p:sldId id="%d" r:id="rId%d"/>'%(255+i,i+1) for i in (1,2,3))+
        '</p:sldIdLst><p:sldSz cx="10080000" cy="5670000" type="screen16x9"/>'
        '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
    parts['ppt/_rels/presentation.xml.rels']=_rels(
        [('slideMaster','slideMasters/slideMaster1.xml')]+
        [('slide','slides/slide%d.xml'%i) for i in (1,2,3)])
    parts['ppt/slideMasters/slideMaster1.xml']=(
        '<p:sldMaster '+NS+'><p:cSld><p:spTree>'+GROUP+'</p:spTree></p:cSld>'
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
        'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
        'accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        '<p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>')
    parts['ppt/slideMasters/_rels/slideMaster1.xml.rels']=_rels(
        [('slideLayout','../slideLayouts/slideLayout1.xml'),('theme','../theme/theme1.xml')])
    parts['ppt/slideLayouts/slideLayout1.xml']=(
        '<p:sldLayout '+NS+' type="blank" preserve="1"><p:cSld name="Blank">'
        '<p:spTree>'+GROUP+'</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/>'
        '</p:clrMapOvr></p:sldLayout>')
    parts['ppt/slideLayouts/_rels/slideLayout1.xml.rels']=_rels(
        [('slideMaster','../slideMasters/slideMaster1.xml')])
    palette=(('dk1','000000'),('lt1','FFFFFF'),('dk2','222222'),('lt2','EEEEEE'),
             ('accent1','4472C4'),('accent2','ED7D31'),('accent3','A5A5A5'),
             ('accent4','FFC000'),('accent5','5B9BD5'),('accent6','70AD47'),
             ('hlink','0000FF'),('folHlink','800080'))
    fill='<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    line='<a:ln w="12700">'+fill+'<a:prstDash val="solid"/></a:ln>'
    font='<a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/>'
    parts['ppt/theme/theme1.xml']=(
        '<a:theme xmlns:a="'+A+'" name="Independent QA"><a:themeElements>'
        '<a:clrScheme name="QA">'+''.join('<a:%s><a:srgbClr val="%s"/></a:%s>'%(key,val,key)
        for key,val in palette)+'</a:clrScheme><a:fontScheme name="QA"><a:majorFont>'+font+
        '</a:majorFont><a:minorFont>'+font+'</a:minorFont></a:fontScheme>'
        '<a:fmtScheme name="QA"><a:fillStyleLst>'+fill*3+'</a:fillStyleLst>'
        '<a:lnStyleLst>'+line*3+'</a:lnStyleLst><a:effectStyleLst>'+
        '<a:effectStyle><a:effectLst/></a:effectStyle>'*3+'</a:effectStyleLst>'
        '<a:bgFillStyleLst>'+fill*3+'</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>')
    for i,(token,colour) in enumerate(zip(TOKENS,COLOURS),1):
        textbox=('<p:sp><p:nvSpPr><p:cNvPr id="2" name="Text%d"/><p:cNvSpPr txBox="1"/>'
                 '<p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="360000" y="360000"/>'
                 '<a:ext cx="5760000" cy="1080000"/></a:xfrm><a:prstGeom prst="rect">'
                 '<a:avLst/></a:prstGeom><a:noFill/></p:spPr><p:txBody><a:bodyPr/>'
                 '<a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="2000"><a:solidFill>'
                 '<a:srgbClr val="000000"/></a:solidFill></a:rPr><a:t>%s</a:t></a:r>'
                 '</a:p></p:txBody></p:sp>')%(i,token)
        rectangle=('<p:sp><p:nvSpPr><p:cNvPr id="3" name="Marker%d"/><p:cNvSpPr/>'
                   '<p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="360000" y="1800000"/>'
                   '<a:ext cx="1800000" cy="1080000"/></a:xfrm><a:prstGeom prst="rect">'
                   '<a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="%s"/></a:solidFill>'
                   '<a:ln><a:noFill/></a:ln></p:spPr></p:sp>')%(i,colour)
        parts['ppt/slides/slide%d.xml'%i]=('<p:sld '+NS+'><p:cSld name="Page%d"><p:spTree>'%i+
            GROUP+textbox+rectangle+'</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/>'
            '</p:clrMapOvr></p:sld>')
        parts['ppt/slides/_rels/slide%d.xml.rels'%i]=_rels(
            [('slideLayout','../slideLayouts/slideLayout1.xml')])
    with ZipFile(Path(destination),'x',compression=ZIP_DEFLATED) as archive:
        for name,xml in sorted(parts.items()):
            info=ZipInfo(name,date_time=(2020,1,1,0,0,0));info.compress_type=ZIP_DEFLATED
            archive.writestr(info,('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'+xml).encode())
