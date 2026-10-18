"""Read-only inventories and scoped Restic arguments for large file trees."""
import fnmatch
import os
from pathlib import Path
import stat
from backup_common import BackupError, checked_path, safe_id


def exclusion_patterns(source):
    patterns=source.get('exclude', [])
    if not isinstance(patterns,list) or any(not isinstance(p,str) or not p or p.startswith('/') or '..' in Path(p).parts or any(c in p for c in '\n\r\0\\') for p in patterns):
        raise BackupError('invalid direct source exclusions')
    return patterns


def scan_direct(source):
    root=checked_path(source['path']);identifier=safe_id(source['id']);patterns=exclusion_patterns(source)
    if not root.exists():
        if source.get('optional'):return {'kind':'files','capture':'direct','optional_absent':True}
        raise BackupError('required direct source missing')
    if not root.is_dir():raise BackupError('direct source must be a directory')
    artifacts=[];fingerprint={}
    def excluded(relative):
        return any(fnmatch.fnmatchcase(relative,p) for p in patterns)
    def add(path):
        checked_path(str(path));info=path.stat(follow_symlinks=False);relative=path.relative_to(root).as_posix()
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise BackupError('unsupported direct source entry')
        directory=stat.S_ISDIR(info.st_mode)
        fingerprint[relative]=(info.st_dev,info.st_ino,info.st_mode) if directory else (info.st_dev,info.st_ino,info.st_mode,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        artifacts.append({'relative_path':identifier+('' if relative=='.' else '/'+relative), 'entry_type':'directory' if directory else 'file','size':0 if directory else info.st_size,'sha256':None})
    def failure(error):raise BackupError('direct source scan failed') from error
    add(root)
    for parent,dirs,files in os.walk(root,followlinks=False,onerror=failure):
        parent=Path(parent)
        dirs[:]=sorted(d for d in dirs if not excluded((parent/d).relative_to(root).as_posix()))
        for name in dirs+sorted(files):
            path=parent/name
            if not excluded(path.relative_to(root).as_posix()):add(path)
    return {'kind':'files','capture':'direct','snapshot_root':str(root),'artifacts':artifacts,'fingerprint':fingerprint}


def verify_direct(sources, exports):
    for source in sources:
        if source.get('capture')=='direct':
            before=exports[source['id']];after=scan_direct(source)
            if before.get('fingerprint')!=after.get('fingerprint') or before.get('optional_absent')!=after.get('optional_absent'):
                raise BackupError('direct source changed during backup')


def direct_arguments(sources, exports):
    roots=[];options=[]
    for source in sources:
        if source.get('capture')!='direct' or exports[source['id']].get('optional_absent'):continue
        root=str(checked_path(source['path']));roots.append(root)
        for pattern in exclusion_patterns(source):
            # Restic literal roots are escaped so glob characters in paths cannot widen exclusions.
            escaped=''.join('['+c+']' if c in '*?[' else c for c in root)
            options.append('--exclude='+escaped+'/'+pattern)
    return options,roots
