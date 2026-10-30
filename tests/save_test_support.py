"""A real external copy/readback before legacy acknowledgement test scenarios."""
from pathlib import Path
import shutil
import tempfile


def verify_download_for_test(case, guard, ticket):
    external = tempfile.TemporaryDirectory()
    case.addCleanup(external.cleanup)
    destination = Path(external.name) / 'download'
    with guard.open_result(ticket) as (source, _, _), destination.open('xb') as output:
        shutil.copyfileobj(source, output)
    return guard.verify_saved_download(ticket, destination)
