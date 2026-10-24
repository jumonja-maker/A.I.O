"""Preserve an already-native standalone chart without a lossy re-export."""
from pathlib import Path
import zipfile

from .errors import GuardError


def copy_chart_identity(source, output, target, proof, *, cancel):
    contract = proof.get('sourceContract', {})
    chart = 'com.sun.star.chart2.ChartDocument'
    if (target != 'odc' or proof.get('state') != 'detected'
            or contract.get('state') != 'recognized'
            or contract.get('mimetype') != 'application/vnd.oasis.opendocument.chart'
            or chart not in proof.get('documentServices', ())):
        return None
    # Read every entry to EOF: ZipExtFile verifies its CRC only at EOF. A
    # bounded declaration scan alone is insufficient proof for byte copying.
    try:
        with zipfile.ZipFile(source) as archive:
            for entry in archive.infolist():
                with archive.open(entry) as stream:
                    while stream.read(65536):
                        if cancel.is_set():
                            raise GuardError('CANCELLED', 'Проверка диаграммы отменена.', stage='result')
        if cancel.is_set():
            raise GuardError('CANCELLED', 'Сохранение диаграммы отменено.', stage='result')
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        target_path = folder / (Path(source).stem + '.odc')
        created = False
        try:
            with Path(source).open('rb') as src, target_path.open('xb') as dst:
                created = True
                while block := src.read(65536):
                    if cancel.is_set():
                        raise GuardError('CANCELLED', 'Сохранение диаграммы отменено.', stage='result')
                    dst.write(block)
        except BaseException:
            if created:
                target_path.unlink(missing_ok=True)
            raise
        return [target_path]
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, ValueError) as error:
        raise GuardError('SOURCE_CONTAINER_INVALID',
                         'Не удалось проверить целостность исходной диаграммы ODC.',
                         stage='result') from error
