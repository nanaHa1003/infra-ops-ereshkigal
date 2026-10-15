"""Synthetic repository fixture; never reads inventory, Vault or a source SSH host."""
from pathlib import Path
import json
import os
import shutil
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'roles/recovery/files'),str(ROOT/'roles/backup/files')]
from backup_common import restic
from backup_manifest import build_manifest
from unittest.mock import patch

def sqlite_repository(root,version):
    stage=root/'old-host/staging/database'
    stage.mkdir(parents=True)
    (stage/'mealie-db').mkdir(); database=stage/'mealie-db/database.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE recipes(id INTEGER PRIMARY KEY,name TEXT)')
        db.execute("INSERT INTO recipes VALUES(1,'synthetic recipe')")
    (stage/'homepage-config').mkdir();(stage/'homepage-config/settings.yaml').write_text('title: Restored fixture\n')
    images={service:[{'service':service,'image':service+':fixture','registry_digests':[service+'@sha256:'+'d'*64],'architecture':'amd64','os':'linux'}] for service in ('mealie','homepage')}
    sources=[{'id':'mealie-db','kind':'sqlite','path':str(root/'old-host/mealie/live.db')},{'id':'homepage-config','kind':'files','path':str(root/'old-host/homepage/config')}]
    config={'host':'old-host.example','domain':'fixture.test','job_id':'database','jobs':{'database':{'sources':sources,'service_projects':{'mealie':'mealie','homepage':'homepage'}}}}
    if version==2:
        with patch('backup_manifest.capture_project_images',side_effect=lambda service:images[service]):
            manifest=build_manifest(stage,config,{'mealie-db':{},'homepage-config':{}})
    else:manifest={'sources':{'mealie-db':{'kind':'sqlite'},'homepage-config':{'kind':'files'}}}
    (stage/'manifest.json').write_text(json.dumps(manifest))
    repo={'url':str(root/'repository'),'restic_password':'synthetic-repository-password','access_key_id':'synthetic','secret_access_key':'synthetic'}
    restic(repo,['init']);restic(repo,['backup','--host','old-host.example','--tag','homelab-config-db:database','.'],cwd=stage)
    scope={'repository':'database','host':'old-host.example','job':'database','staging_path':str(stage),'snapshot':'latest'}
    legacy={'domain':'fixture.test','sources':{'mealie-db':{'kind':'sqlite'},'homepage-config':{'kind':'files'}},'images':images}
    registry={'mealie':{'sources':{'mealie-db':{'kind':'sqlite','layout':'file','target':'mealie/data/mealie.db','owner':int(os.environ.get('HOMELAB_FIXTURE_UID',os.getuid())),'group':int(os.environ.get('HOMELAB_FIXTURE_GID',os.getgid())),'mode':'0600'}}},'homepage':{'sources':{'homepage-config':{'kind':'files','layout':'directory','target':'homepage/config','owner':int(os.environ.get('HOMELAB_FIXTURE_UID',os.getuid())),'group':int(os.environ.get('HOMELAB_FIXTURE_GID',os.getgid())),'mode':'0600'}}}}
    config={'source':scope,'repository':repo,'domain':'fixture.test','state_root':str(root/'replacement-state'),'plan_file':str(root/'controller/plan.json'),'services':['mealie','homepage'],'legacy_profile':legacy,'secrets':{},'registry':registry,
        'target':{'host':'replacement.example','machine_id':'different-target-machine','distribution':'Fedora','release':'43','architecture':'amd64','storage_root':str(root/'replacement-storage'),'stack_root':str(root/'replacement-stack'),'storage_mount':'/','min_free_bytes':1,'probe':{'mount_ok':True,'free_bytes':100000000}}}
    shutil.rmtree(root/'old-host')
    return config
