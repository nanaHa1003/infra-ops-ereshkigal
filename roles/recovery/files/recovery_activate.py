"""Explicit fenced production handoff; no DNS changes or automatic rollback."""
import json
from pathlib import Path
import time
from backup_common import BackupError, checked_path, safe_id
from recovery_databases import _docker, stop_recovery_databases
from recovery_state import advance_service, save_state

ORDER=['auth','wordpress','pinchflat','immich','mealie','homepage','n8n','aria2','proxy']
DEPENDENCIES={'proxy':{'auth','homepage'},'homepage':{'auth','proxy'}}


def build_activation_plan(state,services,old_host_fenced):
    if not old_host_fenced or state.get('phase') not in ('verified','activated'):
        raise BackupError('activation requires verified run and fenced original host')
    if not services or len(set(services))!=len(services) or set(services)-set(state['services']):
        raise BackupError('activation requires an explicit restored service subset')
    available=set(services)|set(state['activation'].get('readiness',{}))
    for service in services:
        safe_id(service)
        if state['services'][service]['phase'] not in ('verified','activated'):
            raise BackupError('service is not verified')
        if DEPENDENCIES.get(service,set())-available:
            raise BackupError('activation dependencies are missing')
        if state['results'].get(service,{}).get('activation_blockers'):
            raise BackupError('service has unresolved activation blockers')
    return {'services':[service for service in ORDER if service in services]}


def begin_activation(state,services,fenced,reference):
    if not isinstance(reference,str) or not reference.strip() or len(reference)>500:
        raise BackupError('fencing reference required')
    plan=build_activation_plan(state,services,fenced)
    stop_recovery_databases(state)
    # Record INTENT before Ansible starts anything, including partial failures.
    state['activation']['services_started']=sorted(set(state['activation'].get('services_started',[]))|set(services))
    state['activation']['fencing_reference']=reference
    save_state(state)
    return plan


WORDPRESS_FPM_PROBE = r"""
$ready=false;
register_shutdown_function(static function() use (&$ready) {if(!$ready) exit(1);});
$socket=@fsockopen('127.0.0.1',9000,$errno,$error,5);
if(!$socket) exit(1);
fclose($socket);
ob_start();
define('WP_USE_THEMES',false);
require '/var/www/html/wp-load.php';
ob_end_clean();
global $wpdb;
if(!isset($wpdb)||!is_blog_installed()||(string)$wpdb->get_var('SELECT 1')!=='1') exit(1);
$ready=true;
"""

APP_PROBES = {
 'n8n': ['node','-e',"fetch('http://127.0.0.1:5678/healthz/readiness').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"],
 'homepage': ['node','-e',"fetch('http://127.0.0.1:3000/api/healthcheck').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"],
 'authentik-server': ['python','-c',"import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/-/health/ready/',timeout=10).close()"],
 'authentik-worker': ['ak','healthcheck'],
 'wordpress': ['php','-r',WORDPRESS_FPM_PROBE],
 'mealie': ['python','-c',"import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/api/app/about',timeout=10).close()"],
 'immich-server': ['node','-e',"fetch('http://127.0.0.1:2283/api/server/ping').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"],
 'caddy': ['wget','-q','-O','/dev/null','http://127.0.0.1:2019/config/'],
 'docker-ro': ['wget','-q','-O','/dev/null','http://127.0.0.1:2375/_ping'],
 'docker-rw': ['wget','-q','-O','/dev/null','http://127.0.0.1:2375/_ping'],
 'mariadb': ['sh','-c','export MYSQL_PWD="$MYSQL_PASSWORD"; exec mariadb --user="$MYSQL_USER" --database="$MYSQL_DATABASE" --host=127.0.0.1 --execute="SELECT 1"'],
}


def ready(service,target):
    directory=checked_path(str(Path(target['stack_root'])/service))
    args=['compose','--project-directory',str(directory),'-f',str(directory/'docker-compose.yml')]
    if service=='proxy': args+=['-f',str(directory.parent/'docker-proxy/docker-compose.yml')]
    # Expected service names come from the exact rendered Compose, not arbitrary running containers.
    config=json.loads(_docker(args+['config','--format','json']))
    expected=set(config['services'])
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        identifiers=_docker(args+['ps','--all','--quiet']).decode().split()
        found=set(); healthy=True
        for container in identifiers:
            selected=json.loads(_docker(['inspect','--format','{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"state":{{json .State}}}',container]))
            found.add(selected['service']); status=selected['state']
            healthy &= status.get('Running') is True and status.get('Health',{}).get('Status','healthy')=='healthy'
        if found>=expected and healthy:
            try:
                for container in identifiers:
                    row=json.loads(_docker(['inspect','--format','{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"health":{{json .State.Health}}}',container]))
                    probe=APP_PROBES.get(row['service'])
                    if probe: _docker(['exec',container,*probe],timeout=20)
                    elif not row.get('health') or row['health'].get('Status')!='healthy':
                        raise BackupError('application ready probe unsupported')
                return {'containers_ready':True,'application_ready':True,'application_login':'manual_confirmation_required'}
            except BackupError: pass
        time.sleep(2)
    raise BackupError('activated service readiness failed')


def check_activation_ready(services,target):
    return {service:ready(service,target) for service in services}


def handoff_report(state,cutover):
    return {'run_id':state['run_id'],'status':'activated' if cutover else 'awaiting_cutover','dns_changed':False,
        'manual_dns_and_login_checks_required':True,'new_backup_configuration_required':True,'independent_healthchecks_required':True,
        'automatic_rollback_available':False}


def finish_activation(state,services,target,cutover,enable_ddns):
    if enable_ddns and not cutover: raise BackupError('DDNS requires completed manual cutover')
    checks=check_activation_ready(services,target)
    if cutover:
        if set(state['activation']['services_started'])-set(services):
            raise BackupError('cutover completion must recheck every started service')
        state['activation']['cutover_confirmed']=True
        for service in services: advance_service(state,service,'activated',state['results'][service])
        # Global phase remains verified when other restored services were not activated.
        state['activation']['status']='activated'
    else: state['activation']['status']='awaiting_cutover'
    state['activation']['readiness']=checks
    save_state(state)
    return handoff_report(state,cutover)


def validate_activation_environment(config,plan,state,services):
    model=plan['model']
    if not model.get('domain') or model['domain']!=config.get('domain'):
        raise BackupError('activation requires the recorded source domain; rename is not supported')
    accepted=config['activation'].get('accepted_limitations',{})
    for service in services:
        images=model.get('images',{}).get(service,[])
        if not images or any(row.get('os')!='linux' for row in images):
            raise BackupError('activation requires recorded Linux images')
        result=state['results'].get(service,{})
        limitations=result.get('limitations',[])
        if limitations and (not isinstance(accepted.get(service),str) or not accepted[service].strip()):
            raise BackupError('limited verification requires a service-specific review reference')
        if service in ('immich','aria2','wordpress','pinchflat','mealie'):
            relative={'immich':'immich/library','aria2':'aria2/downloads','wordpress':'wordpress/html/wp-content/uploads','pinchflat':'pinchflat/downloads','mealie':'mealie/data/recipes'}[service]
            evidence=config['activation'].get('media_evidence',{}).get(service,{})
            path=checked_path(str(Path(config['target']['storage_root'])/relative))
            if not evidence.get('reference') or not path.is_dir() or not any(path.iterdir()):
                raise BackupError('external media path and explicit completeness reference required')
            result['activation_blockers']=[item for item in result['activation_blockers'] if item!='external_media_not_verified']
