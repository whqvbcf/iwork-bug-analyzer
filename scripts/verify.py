"""Run offline regression checks without installing any dependencies."""

import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
commands = [[sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
            ['node', '--test', *[str(p.relative_to(root)) for p in sorted((root/'tests/js').glob('*.test.mjs'))]],
            [sys.executable, '-m', 'compileall', '-q', 'iwork_collector']]
commands.extend(['node', '--check', str(p.relative_to(root))] for p in sorted((root/'extension').rglob('*.mjs')))
for command in commands:
    subprocess.run(command, cwd=root, check=True)
print('Offline regression and syntax checks passed.')
