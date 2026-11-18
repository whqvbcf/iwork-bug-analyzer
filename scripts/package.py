"""Build a portable source/extension ZIP without runtime data or credentials."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import re

root = Path(__file__).resolve().parent.parent
version = re.search(r"__version__ = '([^']+)'", (root / 'iwork_collector/__init__.py').read_text()).group(1)
output = root / ('dist/iwork-collector-' + version + '.zip')
output.parent.mkdir(exist_ok=True)
with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
    for base in ('iwork_collector', 'extension', 'examples', 'scripts', 'docs', 'tests', 'vendor'):
        for path in sorted((root / base).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                archive.write(path, 'iwork-collector/' + path.relative_to(root).as_posix())
    for name in ('README.md', 'pyproject.toml'):
        archive.write(root / name, 'iwork-collector/' + name)
print(output)
