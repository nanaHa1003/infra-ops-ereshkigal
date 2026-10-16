"""Evidence-based checks; no application server, timers or external notifications."""
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing

from backup_common import BackupError, checked_path
from recovery_databases import _docker, database_spec, pg_sql, mysql_sql, identifier, literal

# Schema families are deliberately bounded. Unknown versions remain unverified.
SCHEMAS = {
    'auth': (r':2026\.5\.', {'users':'authentik_core_user', 'applications':'authentik_core_application', 'providers':'authentik_core_provider', 'flows':'authentik_flows_flow'}),
    'n8n': (r':2\.28\.', {'workflows':'workflow_entity', 'credentials':'credentials_entity'}),
    'immich': (r':v2\.', {'assets':'asset', 'users':'user', 'albums':'album'}),
    'mealie': (r':v3\.', {'recipes':'recipes','users':'users'}),
    'pinchflat': (r':v2026\.', {'sources':'sources','profiles':'media_profiles'}),
}


def compare_counts(actual, baseline):
    if baseline is None: return {'status':'baseline_unavailable', 'actual':actual, 'baseline':None}
    expected = baseline.get('tables', baseline)
    if any(actual.get(key) != count for key,count in expected.items()):
        raise BackupError('restored counts differ from snapshot baseline')
    return {'status':'matched', 'actual':actual, 'baseline':expected}


def media_blockers(service, target):
    if service in ('immich','aria2','wordpress','pinchflat','mealie') and not target.get('media_evidence', {}).get(service):
        return ['external_media_not_verified']
    return []


def validate_caddy(image, path):
    _docker(['run','--rm','--network','none','--read-only','--volume',str(checked_path(path))+':/etc/caddy/Caddyfile:ro,Z',image,'caddy','validate','--config','/etc/caddy/Caddyfile','--adapter','caddyfile'], timeout=120)


# CLI output is deliberately discarded. Decrypted content only exists in tmpfs.
N8N_SCRIPT = r'''
const fs=require('fs'), cp=require('child_process');
try {
 const cfg=JSON.parse(fs.readFileSync('/run/recovery/config','utf8'));
 if(!cfg.encryptionKey) process.exit(2);
 process.env.N8N_ENCRYPTION_KEY=cfg.encryptionKey;
 const p=cp.spawnSync('n8n',['export:credentials','--all','--decrypted','--output=/tmp/recovery/credentials.json'],{stdio:'ignore',env:process.env,timeout:120000});
 if(p.status!==0) process.exit(3);
 const rows=JSON.parse(fs.readFileSync('/tmp/recovery/credentials.json','utf8'));
 if(!Array.isArray(rows)||rows.some(x=>!x.id||!x.type||!x.data||typeof x.data!=='object')) process.exit(4);
 fs.unlinkSync('/tmp/recovery/credentials.json');
 process.stdout.write(JSON.stringify({count:rows.length}));
} catch(e) {process.exit(5);}
'''


def check_n8n_cli(options):
    expected = options['expected_count']
    if expected == 0: return {'status':'not_applicable','count':0}
    args = ['run','--rm','--name',options['name'],'--label','homelab.recovery.run='+options.get('run_id','fixture'), '--network',options['network'], '--read-only',
        '--security-opt','no-new-privileges', '--tmpfs','/tmp:rw,noexec,nosuid,mode=1777', '--tmpfs','/home/node/.n8n:rw,noexec,nosuid,mode=1777',
        '--volume',str(checked_path(options['config_file']))+':/run/recovery/config:ro,Z']
    env = {'DB_TYPE':'postgresdb','DB_POSTGRESDB_HOST':options['database_host'], 'DB_POSTGRESDB_DATABASE':options['database'], 'DB_POSTGRESDB_USER':options['app_user'], 'DB_POSTGRESDB_PASSWORD':options['app_password'],
        'N8N_DIAGNOSTICS_ENABLED':'false','N8N_VERSION_NOTIFICATIONS_ENABLED':'false','N8N_TEMPLATES_ENABLED':'false','N8N_RUNNERS_ENABLED':'false','N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS':'false'}
    for key in env: args += ['--env',key]
    # Directory is on tmpfs and contains no persisted exports.
    args += ['--entrypoint','sh',options['image'],'-c','mkdir -p /tmp/recovery; exec node -e "$1"','sh',N8N_SCRIPT]
    try:
        result=json.loads(_docker(args,env=env,timeout=180))
        if result != {'count':expected}: raise BackupError('credential count mismatch')
    except Exception as error: raise BackupError('n8n credential decryption failed') from error
    return {'status':'passed','count':expected}


def verify_n8n_credentials(model, state):
    target = state['_verification_target']
    runtime = state['databases']['n8n']
    spec = database_spec('n8n',model,target,state)
    count = int(pg_sql(runtime,'SELECT count(*) FROM credentials_entity;',runtime['database']))
    options = {**spec, 'image':target['image_variables']['n8n_image'], 'database_host':runtime['name'], 'name':'recovery-cli-'+state['run_id'], 'config_file':str(Path(target['storage_root'])/'n8n/data/config'), 'expected_count':count}
    name=options['name']
    if count and name not in state['owned_containers']:
        state['owned_containers'].append(name)
        from recovery_state import save_state
        save_state(state)
    result=check_n8n_cli(options)
    if name in state['owned_containers']: state['owned_containers'].remove(name)
    return result


def verify_service(service, model, target, state):
    spec=database_spec(service,model,target,state)
    checks={}; limitations=[]; blockers=media_blockers(service,target)
    if spec:
        source=spec['source']; baseline=source.get('verification',{}).get('baseline')
        if spec['kind']=='sqlite':
            with closing(sqlite3.connect(Path(spec['target_file']).as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]: raise BackupError('SQLite verification failed')
                names=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                counts={name:db.execute('SELECT count(*) FROM '+identifier(name)).fetchone()[0] for name in names}
            checks['counts']=compare_counts(counts,baseline)
        elif service in SCHEMAS:
            definition=target['registry'][service].get('verification',{})
            if not definition: raise BackupError('versioned verification registry missing')
            pattern,tables=definition['image_pattern'],definition['tables']
            if not any(re.search(pattern,row['image']) for row in model.get('images',{}).get(service,[])):
                limitations.append('application_schema_version_unsupported'); blockers.append('application_schema_version_unsupported')
            else:
                counts={key:int(pg_sql(spec,'SELECT count(*) FROM '+identifier(table)+';',spec['database'])) for key,table in tables.items()}
                checks['counts']=compare_counts(counts,baseline)
            if service=='n8n':
                state['_verification_target']=target
                checks['credential_decryption']=verify_n8n_credentials(model,state)
        elif service=='wordpress':
            prefix=target.get('database_options',{}).get('wordpress',{}).get('table_prefix')
            if not isinstance(prefix,str) or not re.fullmatch(r'[a-zA-Z0-9_]+',prefix):
                blockers.append('wordpress_table_prefix_confirmation_required')
            else:
                counts={table:int(mysql_sql(spec,'SELECT count(*) FROM `'+prefix+table+'`;',spec['database'])) for table in ('posts','options','users')}
                checks['counts']=compare_counts(counts,baseline)
        if checks.get('counts',{}).get('status')=='baseline_unavailable': limitations.append('snapshot_count_baseline_unavailable')
    else:
        limitations.append('configuration_only_verification')
        if service=='proxy':
            validate_caddy(target['image_variables']['proxy_caddy_image'],str(Path(target['stack_root'])/'proxy/Caddyfile'))
            checks['caddy_config_valid']=True
        if service=='homepage':
            import yaml
            for path in (Path(target['storage_root'])/'homepage/config').glob('*.yaml'):
                yaml.safe_load(path.read_text())
            checks['yaml_parsed']=True
    return {'service':service,'status':'limited' if limitations else 'passed','data_restored':True,'checks':checks,'counts':checks.get('counts',{}),'limitations':limitations,'activation_blockers':blockers}


def write_report(state,results,destination):
    path=checked_path(str(destination)); path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as output:
        json.dump({'run_id':state['run_id'],'snapshot_id':state['snapshot_id'],'phase':state['phase'],'services':results},output,indent=2)
