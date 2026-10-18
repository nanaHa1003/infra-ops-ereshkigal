"""Select one job and derive non-secret, destination-scoped runtime locations."""
import hashlib
from pathlib import Path
import urllib.parse
from backup_common import BackupError, safe_id


def scoped_config(config, job_id):
    safe_id(job_id)
    try:
        job = dict(config['jobs'][job_id])
        name = safe_id(job['repository'])
        repo = dict(config['repositories'][name])
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError('unknown job or repository') from exc
    timeout = job.get('timeout', 6600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 60 <= timeout <= 43200:
        raise BackupError('invalid job timeout')
    repo['timeout'] = min(timeout, job.get('restic_timeout', timeout if timeout > 6600 else 3600))
    if isinstance(repo['timeout'], bool) or not isinstance(repo['timeout'], int) or repo['timeout'] < 1:
        raise BackupError('invalid restic timeout')
    return {**config, 'jobs': {job_id: job}, 'repositories': {name: repo}}


def repository_identity(url):
    if not isinstance(url, str) or not url.strip():
        raise BackupError('repository URL missing')
    value = url.rstrip('/')
    if value.startswith('s3:'):
        parsed = urllib.parse.urlsplit(value[3:])
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BackupError('repository URL must not contain credentials or query')
        port = parsed.port
        if (parsed.scheme, port) in (('http', 80), ('https', 443)):
            port = None
        host = (parsed.hostname or '').lower()
        if ':' in host:
            host = '[' + host + ']'
        value = 's3:' + urllib.parse.urlunsplit((parsed.scheme.lower(), host + (':' + str(port) if port else ''), parsed.path.rstrip('/'), '', ''))
    elif Path(value).is_absolute():
        value = str(Path(value).resolve())
    return hashlib.sha256(value.encode()).hexdigest()
