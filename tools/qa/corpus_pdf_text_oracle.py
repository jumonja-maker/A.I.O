"""QA-only positioned literal readback for native PDF corpus checks.

LibreOffice can emit Latin/punctuation and Cyrillic in separate, interleaved
font runs. The ordinary extractor then returns stream order rather than visual
line order. This reader independently positions each raw Tj/TJ glyph and orders
horizontal baselines. It shares pypdf's PDF/font parser; it is not a second PDF
parser, a renderer, or proof of clipping, appearance, fonts or accessibility.
Unsupported geometry remains explicit uncertainty. No production code uses it.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re
import unicodedata

IDENTITY = [1., 0., 0., 1., 0., 0.]


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', text))


def multiply(a, b):
    return [a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3],
            a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3],
            a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5]]


def translated(matrix, x, y=0.):
    a, b, c, d, e, f = matrix
    return [a, b, c, d, e+x*a+y*c, f+x*b+y*d]


def positioned_page(page):
    # _page imports the locked release's Font implementation. Its module
    # moved between local 6.10 and pinned CI 6.21 without changing this name.
    from pypdf._page import Font
    from pypdf.generic import ContentStream, NameObject, decode_pdfdocencoding

    state = dict(cm=IDENTITY[:], tm=IDENTITY[:], line=IDENTITY[:], font=None,
                 size=12., charspace=0., wordspace=0., scale=1., rise=0., leading=0., render=0)
    glyphs, issues = [], set()
    stats = Counter()
    box = tuple(float(v) for v in page.cropbox)
    budget = [0]
    actual_text_budget = [0]

    def consume(stream, resources, inherited, depth=0):
        if depth > 8:
            issues.add('form_depth_budget')
            return
        local = deepcopy(inherited)
        stack = []
        marked = []
        fonts = {}
        for name, resource in resources.get('/Font', {}).items():
            try:
                fonts[name] = Font.from_font_resource(resource.get_object())
            except Exception:
                issues.add('font_resource_unreadable')
        data = ContentStream(stream, page.pdf, 'bytes')

        def show(value):
            font = fonts.get(local['font'])
            if font is None or not font.interpretable:
                issues.add('font_uninterpretable')
                return
            if isinstance(value, bytes):
                try:
                    if isinstance(font.encoding, str):
                        codes = value.decode(font.encoding, 'surrogatepass')
                    else:
                        codes = ''.join(font.encoding.get(v, chr(v)) for v in value)
                except Exception:
                    issues.add('font_encoding_unreadable')
                    return
                characters = [font.character_map.get(code, code) for code in codes]
                # Pinned pypdf 6.21 keeps widths under raw character codes;
                # local 6.10 maps widths to the decoded Unicode characters.
                # Preserve both rather than guessing widths from display text.
                raw_codes = (list(codes) if isinstance(font.encoding, str) else
                             [chr(byte) for byte in value])
            else:
                # Forced bytes parsing should supply raw PDF character codes.
                issues.add('text_operand_not_raw_bytes')
                characters = list(str(value))
                raw_codes = characters[:]
            for raw_code, text in zip(raw_codes, characters):
                if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
                    issues.add('unmapped_surrogate_glyph')
                    text = ''.join('\ufffd' if 0xD800 <= ord(char) <= 0xDFFF else char for char in text)
                budget[0] += 1
                if budget[0] > 500_000:
                    raise ValueError('PDF positioned glyph budget exceeded')
                matrix = multiply(local['tm'], local['cm'])
                x = matrix[4]+local['rise']*matrix[2]
                y = matrix[5]+local['rise']*matrix[3]
                horizontal = (abs(matrix[1]) < .001 and abs(matrix[2]) < .001
                              and matrix[0] > 0 and matrix[3] > 0)
                if not horizontal:
                    issues.add('rotated_sheared_or_reversed_text')
                elif local['render'] in (3, 7):
                    stats['invisibleGlyphs'] += 1
                elif not (box[0]-1 <= x <= box[2]+1 and box[1]-1 <= y <= box[3]+1):
                    stats['offPageBaselineGlyphs'] += 1
                else:
                    glyph = (y, x, text, abs(local['size']*matrix[3]))
                    glyphs.append(glyph)
                    for frame in marked:
                        if frame['anchor'] is None:
                            frame['anchor'] = glyph
                width_key = raw_code if hasattr(font, 'get_text_width') else text
                width = font.character_widths.get(width_key, font.character_widths.get('default', 500))
                step = ((float(width)/1000*local['size'])+local['charspace']
                        +(local['wordspace'] if text == ' ' else 0))*local['scale']
                local['tm'] = translated(local['tm'], step)

        def begin_marked(properties=None):
            if len(marked) >= 32:
                raise ValueError('PDF marked-content depth budget exceeded')
            frame = {'start':len(glyphs), 'anchor':None, 'text':None,
                     'hasActualText':False, 'valid':True,
                     'invisible':stats['invisibleGlyphs'], 'offPage':stats['offPageBaselineGlyphs']}
            if properties is not None:
                if isinstance(properties, NameObject):
                    named = resources.get('/Properties', {})
                    if hasattr(named, 'get_object'):
                        named = named.get_object()
                    properties = named.get(properties)
                    if hasattr(properties, 'get_object'):
                        properties = properties.get_object()
                if not isinstance(properties, dict):
                    issues.add('marked_content_property_unconfirmed')
                    frame['valid'] = False
                elif '/ActualText' in properties:
                    frame['hasActualText'] = True
                    value = properties['/ActualText']
                    if hasattr(value, 'get_object'):
                        value = value.get_object()
                    try:
                        if isinstance(value, NameObject) or not isinstance(value, (str, bytes)):
                            raise ValueError('not_a_text_string')
                        if len(value) > 65536:
                            raise ValueError('actualtext_budget')
                        actual_text_budget[0] += len(value)
                        if actual_text_budget[0] > 1024*1024:
                            raise ValueError('actualtext_total_budget')
                        if isinstance(value, bytes):
                            if value.startswith(b'\xfe\xff'):
                                value = value[2:].decode('utf-16-be')
                            elif value.startswith(b'\xef\xbb\xbf'):
                                value = value[3:].decode('utf-8')
                            else:
                                value = decode_pdfdocencoding(value)
                        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                            raise ValueError('actualtext_surrogate')
                        frame['text'] = value
                    except (ValueError, UnicodeError):
                        issues.add('actualtext_string_unconfirmed')
                        frame['valid'] = False
            marked.append(frame)

        def end_marked():
            if not marked:
                issues.add('marked_content_unbalanced')
                return
            frame = marked.pop()
            if not frame['hasActualText'] or not frame['valid']:
                return
            if (stats['invisibleGlyphs'] != frame['invisible'] or
                    stats['offPageBaselineGlyphs'] != frame['offPage']):
                issues.add('actualtext_visibility_unconfirmed')
                return
            if frame['anchor'] is None and len(glyphs) > frame['start']:
                frame['anchor'] = glyphs[frame['start']]
            if frame['anchor'] is None and frame['text']:
                issues.add('actualtext_position_unconfirmed')
                return
            # PDF ActualText replaces the whole marked sequence, including
            # ligature/diacritic glyphs and nested spans. Empty replacement is
            # meaningful (e.g. a discretionary hyphen), never "missing data".
            del glyphs[frame['start']:]
            if frame['text']:
                y, x, _old, size = frame['anchor']
                glyphs.append((y, x, frame['text'], size))
            stats['actualTextSpans'] += 1
            stats['actualTextCharacters'] += len(frame['text'])

        for operands, operation in data.operations:
            stats['operations'] += 1
            if stats['operations'] > 2_000_000:
                raise ValueError('PDF positioned operation budget exceeded')
            if operation == b'q':
                stack.append(deepcopy(local))
            elif operation == b'Q':
                if stack:
                    local = stack.pop()
                else:
                    issues.add('graphics_stack_unbalanced')
            elif operation == b'cm':
                local['cm'] = multiply(list(map(float, operands)), local['cm'])
            elif operation == b'BT':
                local['tm'], local['line'] = IDENTITY[:], IDENTITY[:]
            elif operation == b'Tf':
                local['font'], local['size'] = operands[0], float(operands[1])
            elif operation == b'Tm':
                local['tm'] = local['line'] = list(map(float, operands))
            elif operation in (b'Td', b'TD'):
                x, y = map(float, operands)
                if operation == b'TD':
                    local['leading'] = -y
                local['line'] = translated(local['line'], x, y)
                local['tm'] = local['line'][:]
            elif operation in (b'T*', b"'", b'"'):
                if operation == b'"':
                    local['wordspace'], local['charspace'] = map(float, operands[:2])
                local['line'] = translated(local['line'], 0, -local['leading'])
                local['tm'] = local['line'][:]
                if operation != b'T*':
                    show(operands[-1])
            elif operation == b'Tj':
                show(operands[0])
            elif operation == b'TJ':
                for item in operands[0]:
                    if isinstance(item, (str, bytes)):
                        show(item)
                    else:
                        step = -float(item)/1000*local['size']*local['scale']
                        local['tm'] = translated(local['tm'], step)
            elif operation in (b'Tc', b'Tw', b'Tz', b'TL', b'Ts', b'Tr'):
                field = {b'Tc':'charspace', b'Tw':'wordspace', b'Tz':'scale',
                         b'TL':'leading', b'Ts':'rise', b'Tr':'render'}[operation]
                local[field] = float(operands[0])/(100 if operation == b'Tz' else 1)
            elif operation == b'Do':
                obj = resources.get('/XObject', {}).get(operands[0])
                if obj is None:
                    issues.add('xobject_missing')
                    continue
                obj = obj.get_object()
                if obj.get('/Subtype') == '/Form':
                    nested = deepcopy(local)
                    nested['cm'] = multiply(list(map(float, obj.get('/Matrix', IDENTITY))), nested['cm'])
                    consume(obj, obj.get('/Resources', resources), nested, depth+1)
            elif operation == b'BMC':
                if len(operands) != 1 or not isinstance(operands[0], NameObject):
                    issues.add('marked_content_tag_unconfirmed')
                begin_marked()
            elif operation == b'BDC':
                if len(operands) != 2 or not isinstance(operands[0], NameObject):
                    issues.add('marked_content_property_unconfirmed')
                    begin_marked()
                else:
                    begin_marked(operands[1])
            elif operation == b'EMC':
                if operands:
                    issues.add('marked_content_unbalanced')
                end_marked()
            elif operation in (b'W', b'W*'):
                stats['clippingPathsNotInterpreted'] += 1
        if marked:
            issues.add('marked_content_unbalanced')
    resources = page.get('/Resources', {})
    if hasattr(resources, 'get_object'):
        resources = resources.get_object()
    consume(page.get_contents(), resources, state)
    lines = []
    for glyph in sorted(glyphs, key=lambda g:(-g[0], g[1])):
        # A narrow baseline tolerance rejoins different font runs without
        # collapsing ordinary separate 10+ point body lines or table rows.
        if not lines or abs(lines[-1]['baseline']-glyph[0]) > .8:
            lines.append({'baseline':glyph[0], 'glyphs':[glyph]})
        else:
            lines[-1]['glyphs'].append(glyph)
    text = '\n'.join(''.join(g[2] for g in sorted(line['glyphs'], key=lambda g:g[1])) for line in lines)
    return {'text':text, 'logicalText':''.join(g[2] for g in glyphs),
            'glyphs':len(glyphs), 'lines':len(lines),
            'issues':sorted(issues), **dict(stats)}


def inspect_pdf_literals(path, paragraphs):
    from pypdf import PdfReader
    reader = PdfReader(path)
    pages, issues = [], set()
    for page in reader.pages:
        try:
            result = positioned_page(page)
        except Exception as error:
            result = {'text':'', 'issues':['positioned_parser_failed:'+type(error).__name__]}
        pages.append(result)
        issues.update(result['issues'])
    positioned = compact('\n'.join(p['text'] for p in pages))
    semantic_order = compact('\n'.join(p.get('logicalText','') for p in pages))
    # Separate Draw text objects can overlap on one baseline. Coordinate
    # sorting interleaves their glyphs even though each logical object survives.
    # Ordinary order can therefore corroborate literal storage, but it may not
    # bypass known invisible/off-page glyphs or failed geometry decoding.
    ordinary = compact('\n'.join(page.extract_text() or '' for page in reader.pages))
    semantic_allowed = not issues and not any(p.get('invisibleGlyphs') or p.get('offPageBaselineGlyphs') for p in pages)
    ordinary_allowed = semantic_allowed and not any(p.get('actualTextSpans') for p in pages)
    expected = set(compact(p) for p in paragraphs if compact(p))
    semantic_fallback = {p for p in expected if p not in positioned and semantic_allowed and p in semantic_order}
    fallback = {p for p in expected if p not in positioned and p not in semantic_fallback and ordinary_allowed and p in ordinary}
    missing = sorted(p for p in expected if p not in positioned and p not in fallback and p not in semantic_fallback)
    uncertain_semantics = any(s.startswith(('actualtext_', 'marked_content_')) for s in issues)
    status = ('NOT_CHECKED' if not expected else 'UNCERTAIN' if uncertain_semantics else 'PASS' if not missing else
              'UNCERTAIN' if issues else 'MISSING')
    return {'status':status, 'missingLiterals':missing, 'missingLiteralCount':len(missing),
            'distinctExpectedLiterals':len(expected), 'positionedPages':pages,
            'ordinaryOrderFallbackLiteralCount':len(fallback),
            'actualTextAwareLogicalOrderLiteralCount':len(semantic_fallback),
            'ordinaryFallbackSuppressedForActualText':any(p.get('actualTextSpans') for p in pages),
            'issues':sorted(issues), 'scope':'horizontal literal text storage and reading order',
            'secondIndependentPdfParser':False, 'visualEquivalenceChecked':False,
            'clippingCertified':False, 'duplicateCountsCertified':False}
