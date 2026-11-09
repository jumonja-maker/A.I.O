"""Image-preserving PDF defaults for AIO's private LibreOffice profiles.

These settings avoid adding JPEG loss and resampling during export. They do
not repair the import model, absent fonts or unsupported document features.
Existing prepared profiles and explicit filter options are never overwritten.
"""
from pathlib import Path

PDF_IMAGE_FILTER_DATA = (('UseLosslessCompression', True),
                         ('ReduceImageResolution', False))
PDF_PROFILE_ITEM = (
    '<item oor:path="/org.openoffice.Office.Common/Filter/PDF/Export">'
    '<prop oor:name="UseLosslessCompression" oor:op="fuse"><value>true</value></prop>'
    '<prop oor:name="ReduceImageResolution" oor:op="fuse"><value>false</value></prop>'
    '</item>')


def prepare_fresh_pdf_profile(profile):
    """Create defaults only in an unprepared, caller-owned CLI workspace.

    Guard prepares its own security/locale profile and typed filter data.
    The direct CLI path otherwise lets LO's JPEG defaults reduce source
    image quality. O_EXCL preserves an existing profile, including links.
    """
    user = Path(profile)/'user'
    config = user/'registrymodifications.xcu'
    if config.exists() or config.is_symlink():
        return False
    user.mkdir(parents=True, exist_ok=True)
    try:
        with config.open('x', encoding='utf-8') as stream:
            stream.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
                '<prop oor:name="BlockUntrustedReferrerLinks" oor:op="fuse"><value>true</value></prop>'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                '</item>' + PDF_PROFILE_ITEM + '</oor:items>\n')
    except FileExistsError:
        return False
    return True
