"""Verify the exact delivery payload and record a portable package inventory."""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FINAL = ROOT / '交付' / 'TikTok工作台_本地最终版'
PAYLOAD = FINAL / 'resources' / 'payload'
ORIGINAL = Path(r'D:\workbuddy_project\tiktok')


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


manifest = json.loads((PAYLOAD / 'manifest.json').read_text(encoding='utf-8'))
actual = sorted(p.relative_to(PAYLOAD).as_posix() for p in PAYLOAD.rglob('*') if p.is_file() and p.name != 'manifest.json')
assert actual == sorted(manifest['files']), 'Payload manifest mismatch'
for name in actual:
    file = PAYLOAD / name
    assert file.read_bytes() == (ROOT / 'payload' / name).read_bytes(), name
    assert not any(x in name.lower() for x in ('__pycache__', '.bak', '/echotik/', '/tests/')), name
    assert file.suffix not in ('.xlsx', '.mp4', '.pt', '.log'), name
    if file.suffix == '.py':
        source = file.read_text(encoding='utf-8')
        compile(source, str(file), 'exec')
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert '.workbuddy' not in node.value.lower(), name
                assert 'D:/workbuddy_project' not in node.value and 'D:\\workbuddy_project' not in node.value, name

snapshot = json.loads((ROOT / 'source-snapshot.json').read_text(encoding='utf-8'))
assert all(digest(ORIGINAL / name) == expected for name, expected in snapshot.items()), 'Original source changed since snapshot'
assert not (FINAL / 'settings.json').exists()
assert not (PAYLOAD / 'pipeline' / 'accounts.json').exists()
assert not (PAYLOAD / 'webapp' / 'data').exists()
files = [p for p in FINAL.rglob('*') if p.is_file()]
runtime = FINAL / 'resources' / 'runtime'
assert (runtime / 'python' / 'python.exe').is_file()
assert (runtime / 'ffmpeg' / 'ffmpeg.exe').is_file()
assert (runtime / 'models' / 'medium.pt').is_file()
result = {
    'ok': True, 'delivery': str(FINAL), 'files': len(files), 'bytes': sum(p.stat().st_size for p in files),
    'payload_files': len(actual), 'original_python_files_unchanged': len(snapshot),
    'sha256': {name: digest(FINAL / name) for name in ('TikTok工作台.exe', 'resources/app.asar')},
    'checks': ['exact payload manifest', 'packaged and prepared payload identical', 'Python compilation', 'no original machine source paths or WorkBuddy configuration reads in runtime Python', 'no packaged accounts or settings', 'runtime and offline speech model present', 'original source hashes unchanged'],
}
(ROOT / '交付核验.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
