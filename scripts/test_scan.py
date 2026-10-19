"""Isolated regression tests; all fixtures live in the OS temporary directory."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('scan_codex', Path(__file__).with_name('scan_codex.py'))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-storage-test-')
        self.root = Path(self.temp.name) / 'codex'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def history(self, name, identity, cwd='/Users/test/工程'):
        p = self.root / 'sessions' / name
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': identity, 'cwd': cwd}}, ensure_ascii=False) + '\n', encoding='utf-8')
        return p

    def test_groups_real_header_not_filename_suffix_and_keeps_data(self):
        a = self.history('rollout-parent.jsonl', 'parent')
        b = self.history('rollout-parent_other-uuid.jsonl', 'parent')
        con = sqlite3.connect(self.root / 'state_5.sqlite')
        con.executescript('CREATE TABLE threads(id TEXT,name TEXT,cwd TEXT,rollout_path TEXT,project_id TEXT); CREATE TABLE projects(id TEXT,name TEXT); CREATE TABLE project_roots(project_id TEXT,path TEXT);')
        con.execute('INSERT INTO threads VALUES(?,?,?,?,?)', ('parent', '中文测试标题', '/Users/test/工程', str(b), 'project'))
        con.execute('INSERT INTO projects VALUES(?,?)', ('project', '中文项目'))
        con.commit(); con.close()
        def hashes():
            return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
        before = hashes()
        result = scan.scan(self.root, 20, None)
        self.assertEqual(result['conversation_group_count'], 1)
        row = result['top_conversations'][0]
        self.assertEqual(row['bytes'], a.stat().st_size + b.stat().st_size)
        self.assertEqual(row['title'], '中文测试标题')
        self.assertEqual(row['project'], '中文项目')
        self.assertEqual(hashes(), before)

    def test_missing_database_not_created_and_unreadable_header_not_guessed(self):
        p = self.root / 'sessions' / 'rollout-unknown.jsonl'
        p.parent.mkdir(); p.write_bytes(b'not JSON\n')
        result = scan.scan(self.root, 20, None)
        self.assertIsNone(result['top_conversations'][0]['id'])
        self.assertEqual(result['top_conversations'][0]['identity_sources'], ['unverified'])
        self.assertFalse((self.root / 'state_5.sqlite').exists())

    def test_active_plugin_source_is_not_candidate(self):
        base = self.root / '.tmp' / 'bundled-marketplaces'
        for name in ('openai-bundled', 'openai-bundled.staging-old'):
            p = base / name; p.mkdir(parents=True); (p / 'data').write_bytes(b'x')
        result = scan.scan(self.root, 20, None)
        self.assertEqual(len(result['cleanup_candidates']), 1)
        self.assertTrue(result['cleanup_candidates'][0]['path'].endswith('openai-bundled.staging-old'))
        self.assertEqual(result['cleanup_candidates'][0]['status'], 'needs_review_not_approved')

    def test_windows_and_macos_paths(self):
        self.assertEqual(scan.path_key(r'\\?\C:\Users\A\.codex'), 'c:/users/a/.codex')
        self.assertEqual(scan.path_key('/Users/A/.codex'), '/Users/A/.codex')

    def test_skips_outside_symlink(self):
        outside = Path(self.temp.name) / 'outside'; outside.mkdir()
        (outside / 'data').write_bytes(b'secret')
        try:
            (self.root / 'sqlite').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('OS disallows unprivileged symlink creation')
        result = scan.scan(self.root, 20, None)
        self.assertEqual(result['total_bytes'], 0)
        self.assertEqual(len(result['skipped_links']), 1)

    def test_zstd_stream_header(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is not installed')
        raw = self.history('plain.jsonl', 'compressed-parent')
        target = raw.with_suffix('.jsonl.zst')
        code = "const f=require('node:fs'),z=require('node:zlib'); if(!z.zstdCompressSync)process.exit(77); f.writeFileSync(process.argv[2],z.zstdCompressSync(f.readFileSync(process.argv[1])));"
        proc = subprocess.run([node, '-e', code, str(raw), str(target)], capture_output=True)
        if proc.returncode == 77:
            self.skipTest('Node has no Zstandard support')
        self.assertEqual(proc.returncode, 0)
        result = scan.scan(self.root, 20, node)
        self.assertEqual(result['conversation_group_count'], 1)
        self.assertEqual(result['top_conversations'][0]['compressed_files'], 1)
        self.assertEqual(result['top_conversations'][0]['bytes'], raw.stat().st_size + target.stat().st_size)

    def test_comparison_and_unicode_cli(self):
        self.history('one.jsonl', 'one')
        before = scan.scan(self.root, 20, None)
        baseline = Path(self.temp.name) / 'before.json'
        baseline.write_text(json.dumps(before), encoding='utf-8')
        extra = self.root / 'cache'; extra.mkdir(); (extra / 'x').write_bytes(b'123')
        proc = subprocess.run([sys.executable, '-X', 'utf8', str(Path(scan.__file__)), '--codex-home', str(self.root), '--compare', str(baseline)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['comparison']['change_bytes'], 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
