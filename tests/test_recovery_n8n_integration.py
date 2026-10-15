"""Matching n8n CLI decrypts synthetic credentials without starting its server."""
import json
import os
from pathlib import Path
import secrets
import sys
import time
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'roles/recovery/files'),str(ROOT/'roles/backup/files')]
from recovery_databases import _docker, BackupError
from recovery_verify import check_n8n_cli

@unittest.skipUnless(os.environ.get('HOMELAB_RECOVERY_INTEGRATION')=='1','real n8n integration explicitly disabled')
class N8nCredentialRecovery(unittest.TestCase):
    def test_correct_and_wrong_keys_without_workloads(self):
        name='recovery-n8n-'+secrets.token_hex(5)
        network=name+'-net'; dbname=name+'-db'; sink=name+'-sink'
        image=os.environ.get('HOMELAB_N8N_FIXTURE_IMAGE','docker.io/n8nio/n8n:2.28.0')
        _docker(['pull',image],timeout=900)
        image=json.loads(_docker(['image','inspect','--format','{{json .RepoDigests}}',image]))[0]
        base=Path(os.environ['HOMELAB_RECOVERY_FIXTURE_ROOT'])/name
        base.mkdir(mode=0o755)
        config=base/'config'; config.write_text(json.dumps({'encryptionKey':'synthetic-key-do-not-log'})); os.chown(config,1000,1000); config.chmod(0o600)
        credential=base/'credentials.json'
        credential.write_text(json.dumps([{'id':'fixture-credential','name':'Synthetic credential','type':'httpBasicAuth','data':{'user':'synthetic','password':'synthetic-plaintext-never-log'}}])); credential.chmod(0o644)
        workflow=base/'workflows.json'
        workflow.write_text(json.dumps([{'id':'fixture-workflow','name':'Never run fixture','active':True,'nodes':[{'id':'timer','name':'Schedule','type':'n8n-nodes-base.scheduleTrigger','typeVersion':1.2,'position':[0,0],'parameters':{'rule':{'interval':[{'field':'seconds','secondsInterval':1}]}}},{'id':'http','name':'HTTP','type':'n8n-nodes-base.httpRequest','typeVersion':4.2,'position':[300,0],'parameters':{'url':'http://'+sink+':8000/'}}],'connections':{'Schedule':{'main':[[{'node':'HTTP','type':'main','index':0}]]}},'settings':{}}])); workflow.chmod(0o644)
        env={'DB_TYPE':'postgresdb','DB_POSTGRESDB_HOST':dbname,'DB_POSTGRESDB_DATABASE':'appdb','DB_POSTGRESDB_USER':'app','DB_POSTGRESDB_PASSWORD':'fake-db','N8N_ENCRYPTION_KEY':'synthetic-key-do-not-log','N8N_DIAGNOSTICS_ENABLED':'false','N8N_VERSION_NOTIFICATIONS_ENABLED':'false','N8N_RUNNERS_ENABLED':'false'}
        def cli(command):
            args=['run','--rm','--network',network,'--volume',str(base)+':/fixtures:ro,Z']
            for key in env: args+=['-e',key]
            return _docker(args+[image,*command],env=env,timeout=300)
        try:
            _docker(['network','create','--internal',network])
            _docker(['run','-d','--name',dbname,'--network',network,'--network-alias',dbname,'-e','POSTGRES_PASSWORD=fake-db','-e','POSTGRES_USER=app','-e','POSTGRES_DB=appdb','postgres:18.0-alpine'])
            sink_script = "import http.server,socket,threading\nclass Handler(http.server.BaseHTTPRequestHandler):\n def do_GET(self):\n  print('HTTP_REQUEST',flush=True); self.send_response(200); self.end_headers()\n def log_message(self,*args): pass\ndef smtp():\n s=socket.socket(); s.bind(('0.0.0.0',25)); s.listen()\n while True:\n  c,a=s.accept(); print('SMTP_CONNECTION',flush=True); c.close()\nthreading.Thread(target=smtp,daemon=True).start()\nhttp.server.HTTPServer(('0.0.0.0',8000),Handler).serve_forever()"
            _docker(['run','-d','--name',sink,'--network',network,'--network-alias',sink,'python:3.12-alpine','python','-u','-c',sink_script],timeout=300)
            for _ in range(120):
                try: _docker(['exec',dbname,'pg_isready','-h','127.0.0.1','-U','app','-d','appdb']); break
                except BackupError: time.sleep(1)
            cli(['import:credentials','--input=/fixtures/credentials.json'])
            cli(['import:workflow','--input=/fixtures/workflows.json'])
            # Make it active in DB directly: verification must not run scheduled jobs.
            _docker(['exec',dbname,'psql','-U','app','-d','appdb','-c','UPDATE workflow_entity SET active=true;'])
            options={'image':image,'name':name+'-cli','network':network,'database':'appdb','app_user':'app','app_password':'fake-db','database_host':dbname,'config_file':str(config),'expected_count':1,'run_id':name}
            result=check_n8n_cli(options)
            self.assertEqual(result,{'status':'passed','count':1})
            config.write_text(json.dumps({'encryptionKey':'wrong-key'}))
            with self.assertRaisesRegex(BackupError,'n8n credential decryption failed'): check_n8n_cli(options)
            self.assertNotIn('synthetic-plaintext',json.dumps(result))
            self.assertEqual(_docker(['logs',sink]),b'')
            names=_docker(['ps','-a','--filter','name=^'+name+'-cli$','--format','{{.Names}}']).decode().strip()
            self.assertFalse(names,'CLI tmpfs container must be removed')
        finally:
            for container in (name+'-cli',sink,dbname):
                try: _docker(['rm','-f','-v',container])
                except BackupError: pass
            _docker(['network','rm',network])
