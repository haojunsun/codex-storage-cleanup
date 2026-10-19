"""Read-only Codex storage inventory. JSON stdout is also the comparison format."""
import argparse
import collections
import datetime
import importlib
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys

LIMIT = 1024 * 1024


def path_key(value):
    value = str(value)
    if value.startswith('\\\\?\\'):
        value = value[4:]
    if re.match(r'^[A-Za-z]:[\\/]', value) or value.startswith('\\\\'):
        return ntpath.normcase(ntpath.normpath(value)).replace('\\', '/')
    return posixpath.normpath(value)


def transcript_key(value):
    return path_key(str(value).removesuffix('.zst'))


def inventory(root):
    files, skipped, errors = [], [], []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                            skipped.append(entry.path)
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                        elif stat.S_ISREG(info.st_mode):
                            p = Path(entry.path)
                            files.append({'path': str(p), 'relative': p.relative_to(root).as_posix(),
                                          'bytes': info.st_size, 'mtime': info.st_mtime})
                    except OSError as exc:
                        errors.append({'path': entry.path, 'error': type(exc).__name__})
        except OSError as exc:
            errors.append({'path': str(directory), 'error': type(exc).__name__})
    return files, skipped, errors


def indexes(root, errors, files):
    threads, projects, roots, databases = {}, {}, [], []
    paths = [Path(item['path']) for item in files
             if re.fullmatch(r'(?:sqlite/)?state_\d+\.sqlite|thread_history_1\.sqlite', item['relative'])]
    paths.sort(key=lambda p: (p.parent != root, p.name))
    # Prefer the main CODEX_HOME index over older secondary indexes for duplicate IDs.
    for p in paths:
        if not p.is_file() or p.is_symlink():
            continue
        con = None
        try:
            con = sqlite3.connect(p.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA query_only=ON')
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            page_size = con.execute('PRAGMA page_size').fetchone()[0]
            free = con.execute('PRAGMA freelist_count').fetchone()[0]
            databases.append({'path': str(p), 'file_bytes': p.stat().st_size,
                              'page_count': con.execute('PRAGMA page_count').fetchone()[0],
                              'free_pages': free, 'free_page_bytes': page_size * free})
            if 'threads' in tables:
                columns = {r[1] for r in con.execute('PRAGMA table_info(threads)')}
                fields = [v for v in ('id', 'name', 'title', 'cwd', 'project_id', 'rollout_path', 'archived') if v in columns]
                if 'id' in fields:
                    for row in con.execute('SELECT ' + ','.join(fields) + ' FROM threads'):
                        threads.setdefault(row['id'], dict(row))
            if 'projects' in tables:
                for row in con.execute('SELECT id,name FROM projects'):
                    projects.setdefault(row['id'], row['name'])
            if 'project_roots' in tables:
                columns = {r[1] for r in con.execute('PRAGMA table_info(project_roots)')}
                if {'project_id', 'path'} <= columns:
                    roots.extend((path_key(r['path']), r['project_id']) for r in con.execute('SELECT project_id,path FROM project_roots'))
        except (sqlite3.Error, OSError) as exc:
            errors.append({'path': str(p), 'error': type(exc).__name__})
        finally:
            if con is not None:
                con.close()
    return threads, projects, sorted(roots, key=lambda v: len(v[0]), reverse=True), databases


def first_metadata(stream):
    line = stream.readline(LIMIT + 1)
    if len(line) > LIMIT:
        raise ValueError('metadata too large')
    row = json.loads(line)
    payload = row.get('payload', {})
    if row.get('type') != 'session_meta' or not isinstance(payload.get('id'), str):
        raise ValueError('missing session metadata')
    return {'id': payload['id'], 'cwd': payload.get('cwd')}


def metadata(files, warnings, node):
    found, compressed = {}, []
    decoder = None
    for module in ('compression.zstd', 'zstandard'):
        try:
            decoder = importlib.import_module(module)
            break
        except ImportError:
            pass
    for item in files:
        p = item['path']
        if p.endswith('.zst'):
            compressed.append(p)
            continue
        try:
            with open(p, 'rb') as stream:
                found[p] = first_metadata(stream)
        except (OSError, ValueError, TypeError):
            warnings.append({'path': p, 'error': 'Cannot read first session metadata record'})
    pending = []
    for p in compressed:
        if decoder is None:
            pending.append(p)
            continue
        try:
            with decoder.open(p, 'rb') as stream:
                found[p] = first_metadata(stream)
        except Exception:
            pending.append(p)
    if pending and node:
        try:
            proc = subprocess.run([node, str(Path(__file__).with_name('zstd_headers.cjs'))],
                                  input=json.dumps(pending), text=True, encoding='utf-8',
                                  capture_output=True, timeout=120,
                                  **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
            for line in proc.stdout.splitlines():
                row = json.loads(line)
                if row.get('id') and row.get('file') in pending:
                    found[row['file']] = {'id': row['id'], 'cwd': row.get('cwd')}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            warnings.append({'error': 'Node metadata reader unavailable or incomplete'})
    missing = [p for p in pending if p not in found]
    if missing:
        warnings.append({'error': 'Compressed headers unavailable; exact index path fallback only', 'count': len(missing)})
    return found


def project_for(thread, cwd, projects, roots):
    if thread.get('project_id') in projects:
        return projects[thread['project_id']], 'project_id'
    key = path_key(cwd) if cwd else ''
    for root, pid in roots:
        if key and (key == root or key.startswith(root.rstrip('/') + '/')) and pid in projects:
            return projects[pid], 'project_root'
    return (key.rstrip('/').rsplit('/', 1)[-1] if key else '未知项目'), 'cwd_inferred'


def candidates(root, files):
    grouped = {}
    for item in files:
        rel = item['relative']
        parts = rel.split('/')
        label = purpose = None
        if len(parts) > 1 and parts[0] == '.tmp':
            for i, part in enumerate(parts[1:], 1):
                if part.startswith(('openai-bundled.staging-', 'marketplace-upgrade-', 'plugins-backup-')):
                    label = '/'.join(parts[:i + 1]); purpose = '插件升级暂存或旧备份'; break
        elif len(parts) == 1 and rel.startswith('..codex-global-state.json.tmp-'):
            label = rel; purpose = '全局状态写入临时副本'
        elif parts[0] in ('backups', 'db-backups', 'backups_state') and len(parts) > 1:
            label = '/'.join(parts[:2]); purpose = '恢复备份，删除会失去恢复点'
        elif len(parts) == 1 and ('.bak' in rel or 'pre-migration-backup' in rel):
            label = rel; purpose = '配置或数据库备份，需核对是否仍在更新'
        if label:
            row = grouped.setdefault(label, {'path': str(root / label), 'purpose': purpose,
                                             'bytes': 0, 'files': 0, 'latest_mtime': 0,
                                             'status': 'needs_review_not_approved'})
            row['bytes'] += item['bytes']; row['files'] += 1
            row['latest_mtime'] = max(row['latest_mtime'], item['mtime'])
    return sorted(grouped.values(), key=lambda v: v['bytes'], reverse=True)


def scan(root, top, node):
    files, skipped, errors = inventory(root)
    threads, projects, roots, databases = indexes(root, errors, files)
    session_files = [v for v in files if v['relative'].split('/')[0] in ('sessions', 'archived_sessions')
                     and v['path'].endswith(('.jsonl', '.jsonl.zst'))]
    headers = metadata(session_files, errors, node)
    by_path = {transcript_key(t['rollout_path']): t for t in threads.values() if t.get('rollout_path')}
    groups = {}
    for item in session_files:
        p = item['path']; meta = headers.get(p)
        if meta:
            identity = meta['id']; thread = threads.get(identity, {}); confidence = 'session_meta'
        else:
            thread = by_path.get(transcript_key(p), {})
            identity = thread.get('id'); confidence = 'index_path' if identity else 'unverified'
        key = identity or p
        cwd = thread.get('cwd') or (meta or {}).get('cwd')
        project, source = project_for(thread, cwd, projects, roots)
        title = thread.get('name') or thread.get('title') or '未匹配到聊天标题'
        title = str(title).splitlines()[0][:160]
        group = groups.setdefault(key, {'id': identity, 'title': title, 'project': project,
                                        'project_source': source, 'cwd': cwd, 'bytes': 0,
                                        'files': [], 'compressed_files': 0,
                                        'identity_sources': [], 'indexed': bool(thread)})
        group['bytes'] += item['bytes']; group['files'].append(p)
        group['compressed_files'] += int(p.endswith('.zst'))
        if confidence not in group['identity_sources']:
            group['identity_sources'].append(confidence)
    directories = collections.defaultdict(lambda: {'bytes': 0, 'files': 0})
    for item in files:
        parts = item['relative'].split('/')
        row = directories[parts[0] if len(parts) > 1 else '[root files]']
        row['bytes'] += item['bytes']; row['files'] += 1
    ranked = sorted(groups.values(), key=lambda v: v['bytes'], reverse=True)[:top]
    for i, row in enumerate(ranked, 1):
        row['rank'] = i
    return {'schema_version': 1, 'scanned_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'codex_home': str(root), 'size_basis': 'logical_file_lengths_not_physical_allocation',
            'total_bytes': sum(v['bytes'] for v in files), 'file_count': len(files),
            'directories': dict(sorted(directories.items(), key=lambda v: v[1]['bytes'], reverse=True)),
            'session_file_count': len(session_files), 'conversation_group_count': len(groups),
            'top_conversations': ranked, 'database_space': databases,
            'cleanup_candidates': candidates(root, files), 'skipped_links': skipped, 'warnings': errors}


def markdown(report):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = [f"Codex 目录：{report['codex_home']}",
             f"总量：{report['total_bytes'] / 2**30:.2f} GiB；会话文件：{report['session_file_count']} 个。",
             '大小按文件长度合计；同一会话已合并，压缩文件按压缩后大小计算。', '',
             '| 编号 | 聊天标题 | 项目 | MiB | 文件数 | 身份依据 |', '|---:|---|---|---:|---:|---|']
    for row in report['top_conversations']:
        lines.append(f"| {row['rank']} | {cell(row['title'])} | {cell(row['project'])} | {row['bytes']/2**20:.2f} | {len(row['files'])} | {','.join(row['identity_sources'])} |")
    lines += ['', '项目来源请核对 JSON 中的 project_source；cwd_inferred 表示按工作目录推断。',
              f"待人工核对的清理候选：{len(report['cleanup_candidates'])}；读取警告：{len(report['warnings'])}。"]
    lines += ['', '| 目录 | MiB |', '|---|---:|']
    for name, row in report['directories'].items():
        lines.append(f"| {cell(name)} | {row['bytes']/2**20:.2f} |")
    lines += ['', '| 数据库 | 文件 MiB | 内部空闲页 MiB（非保证释放量） |', '|---|---:|---:|']
    for row in report['database_space']:
        lines.append(f"| {cell(row['path'])} | {row['file_bytes']/2**20:.2f} | {row['free_page_bytes']/2**20:.2f} |")
    lines += ['', '以下仅为候选，尚未检查引用及运行占用，不代表可以直接删除。', '',
              '| 候选路径 | MiB | 用途 |', '|---|---:|---|']
    for row in report['cleanup_candidates']:
        lines.append(f"| {cell(row['path'])} | {row['bytes']/2**20:.2f} | {cell(row['purpose'])} |")
    if 'comparison' in report:
        lines.append(f"相比旧报告净变化：{report['comparison']['change_bytes']/2**20:+.2f} MiB。")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--format', choices=('json', 'markdown'), default='json')
    parser.add_argument('--compare', type=Path, help='Previously saved JSON report')
    parser.add_argument('--node', help='Optional Node executable for Zstandard metadata')
    args = parser.parse_args()
    root = (args.codex_home or Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')).expanduser().resolve()
    if not root.is_dir():
        parser.error('Codex directory does not exist')
    if args.top < 1:
        parser.error('--top must be positive')
    report = scan(root, args.top, args.node or shutil.which('node'))
    if args.compare:
        previous = json.loads(args.compare.read_text(encoding='utf-8-sig'))
        if path_key(previous['codex_home']) != path_key(root):
            parser.error('Cannot compare different Codex directories')
        report['comparison'] = {'change_bytes': report['total_bytes'] - previous['total_bytes'],
                                'session_file_change': report['session_file_count'] - previous['session_file_count'],
                                'directory_changes': {name: report['directories'].get(name, {}).get('bytes', 0) - previous.get('directories', {}).get(name, {}).get('bytes', 0)
                                                      for name in sorted(set(report['directories']) | set(previous.get('directories', {})))}}
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == 'json' else markdown(report))


if __name__ == '__main__':
    main()
