Windows PCT follow-up after run 42534118291, HEAD fd86ffe13d705a0953c2ca818e2c6e49f194c93c

Observed: completion_media_native reported 11 PASS, 2 PASS_SAFE_REFUSAL,
0 BLOCKED, 2 FAIL. Only QuickDraw PCT -> PNG/TIFF failed, both with the
unchanged MEDIA_CANVAS_CHANGED mandatory postcondition. The report does not
record failed-output dimensions, so 96-vs-72-DPI scaling remains a causal
hypothesis rather than an observed numeric result. Native rich/limited Draw
GIF controls passed their strict refusal/exact-pixel controls on Windows.

Corrective implementation: serialize the confirmed single PCT shape with
GraphicExportFilter.setSourceDocument(shape), rather than GraphicProvider.
Keep its exact native importer, QuickDraw frame, single page/shape, no
rotation/shear/crop and bounded canvas gates. Choose a currently supported MIME
type; pass typed PixelWidth/PixelHeight; reject false filter/missing output.
Normal Draw page exports are unchanged. Canvas failures now include declared
source and observed result pixel dimensions in their diagnostic.

LibreOffice implementation reference inspected during diagnosis:
https://github.com/LibreOffice/core/blob/master/svx/source/unodraw/UnoGraphicExporter.cxx
The setSourceDocument implementation explicitly accepts an XShape; its shape
rasterization path passes requested pixel dimensions through CalcSize, separate
from full-page rendering. A local copy is retained under media/lo-source/.

Validation: media/windows-pct-fix-unit.log records 112 focused tests, all PASS
(including 9 real DOM variants). Updated PCT regression asserts the actual
GraphicExportFilter service, bound source shape, MIME and typed dimensions.
Compile and diff checks passed. Actual fixed PCT output still requires the
next Windows native run; no Linux/mock test is presented as that native proof.
