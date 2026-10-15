"""Real engine round trips. Explicit integration flag makes missing Docker an error."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'roles/recovery/files'), str(ROOT / 'roles/backup/files')]
from recovery_databases import _docker, _restore_postgres, _restore_mariadb, pg_sql, mysql_sql, database_spec, restore_database, stop_recovery_databases, BackupError
from recovery_state import open_run, advance_service

@unittest.skipUnless(os.environ.get('HOMELAB_RECOVERY_INTEGRATION') == '1', 'real database integration explicitly disabled')
class RealDatabaseRecovery(unittest.TestCase):
    def roundtrip(self, service, tag, major, extension=False):
        _docker(['pull', tag], timeout=900)
        image = json.loads(_docker(['image', 'inspect', '--format', '{{json .RepoDigests}}', tag]))[0]
        run = 'db-' + secrets.token_hex(5)
        base = Path(os.environ['HOMELAB_RECOVERY_FIXTURE_ROOT']) / run
        base.mkdir(mode=0o700)
        source_name = 'recovery-source-' + run
        state = None
        try:
            postgres = major != 'mariadb'
            args = ['run', '-d', '--name', source_name, '--network', 'none', '-e', 'POSTGRES_PASSWORD=fake-source', '-e', 'POSTGRES_USER=app', '-e', 'POSTGRES_DB=appdb'] if postgres else ['run', '-d', '--name', source_name, '--network', 'none', '-e', 'MARIADB_ROOT_PASSWORD=fake-source']
            args += [image]
            if extension: args += ['postgres', '-c', 'config_file=/etc/postgresql/postgresql.conf', '-c', 'shared_preload_libraries=vchord.so,vectors.so']
            _docker(args, timeout=180)
            spec = {'name': source_name}
            for _ in range(120):
                try:
                    if postgres: _docker(['exec', source_name, 'pg_isready', '-h', '127.0.0.1', '-U', 'app', '-d', 'appdb'])
                    else: mysql_sql(spec, 'SELECT 1;', tcp=True)
                    break
                except BackupError: time.sleep(1)
            if postgres:
                sql = 'CREATE TABLE parent(id integer PRIMARY KEY, name text); CREATE TABLE child(id integer PRIMARY KEY, parent_id integer REFERENCES parent(id)); INSERT INTO parent VALUES(1,\'fixture\'); INSERT INTO child VALUES(2,1);'
                if extension:
                    sql += "CREATE EXTENSION vector; CREATE EXTENSION vectors; CREATE EXTENSION vchord CASCADE; CREATE TABLE embedding(id integer PRIMARY KEY, value vector(3)); INSERT INTO embedding VALUES(1,'[1,2,3]'); CREATE INDEX embedding_hnsw ON embedding USING hnsw(value vector_l2_ops);"
                pg_sql(spec, sql, 'appdb')
                version = pg_sql(spec, 'SELECT version();', 'appdb')
                extensions = json.loads(pg_sql(spec, "SELECT json_agg(json_build_object('name',extname,'version',extversion)) FROM pg_extension;", 'appdb'))
                dump = _docker(['exec', source_name, 'pg_dump', '-U', 'app', '-d', 'appdb', '-Fc', '--no-owner', '--no-acl'])
                kind = 'postgres'; suffix = 'dump'
                metadata = {'version': version, 'extensions': extensions, 'database': 'appdb', 'role': 'app', 'preload_libraries': 'vchord.so,vectors.so' if extension else ''}
            else:
                mysql_sql(spec, "CREATE DATABASE appdb; CREATE USER 'app'@'%' IDENTIFIED BY 'fake-app'; GRANT ALL ON appdb.* TO 'app'@'%'; CREATE TABLE appdb.parent(id int PRIMARY KEY, name text); INSERT INTO appdb.parent VALUES(1,'fixture'); CREATE TABLE appdb.child(id int PRIMARY KEY,parent_id int, FOREIGN KEY(parent_id) REFERENCES parent(id)); INSERT INTO appdb.child VALUES(2,1); CREATE DEFINER='app'@'%' PROCEDURE appdb.read_parent() SELECT * FROM appdb.parent; CREATE DEFINER='app'@'%' TRIGGER appdb.parent_update BEFORE UPDATE ON appdb.parent FOR EACH ROW SET NEW.name=UPPER(NEW.name); CREATE DEFINER='app'@'%' EVENT appdb.test_event ON SCHEDULE EVERY 1 DAY DO UPDATE appdb.parent SET name='event';")
                version = mysql_sql(spec, 'SELECT VERSION();')
                dump = _docker(['exec', source_name, 'sh', '-c', 'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; exec mariadb-dump -uroot --single-transaction --routines --events --triggers --databases appdb'])
                kind = 'mariadb'; suffix = 'sql'; metadata = {'version': version, 'database': 'appdb', 'role': 'app', 'definers': ['app@%']}
            _docker(['rm', '-f', '-v', source_name])
            source_name = None
            data = base / 'storage' / 'db'
            plan = {'run_id': run, 'selection': {'snapshot_id': 'a'*64}, 'profile_sha256': 'b'*64, 'target': {'machine_id': 'fixture'}, 'services': [service], 'approved_roots': [str(data)]}
            state = open_run(plan, base / 'state')
            for phase in ('downloaded', 'prepared'): advance_service(state, service, phase, {})
            scratch = Path(state['_run_root']) / 'scratch'; scratch.mkdir()
            (scratch / 'database').mkdir()
            artifact = scratch / 'database' / ('database.' + suffix); artifact.write_bytes(dump)
            model = {'selection': {'tree_root': '/'}, 'services': [service], 'sources': {'database': {'kind': kind, **metadata, 'artifacts': [{'relative_path': 'database/' + artifact.name, 'entry_type': 'file', 'size': len(dump), 'sha256': None}]}}}
            from recovery_databases import DB_IMAGES
            target = {'storage_root': str(base/'storage'), 'stack_root': str(base/'stack'), 'storage_mount': '/', 'scratch_root': str(scratch), 'services': [service], 'run_id': run, 'registry': {service: {'sources': {'database': {'kind': kind, 'target': 'db', 'layout': 'database', 'owner': 0, 'group': 0, 'mode': '0700'}}}}, 'image_variables': {DB_IMAGES[service]: image}, 'secrets': {'authentik': {'pgpass': 'fake-app'}, 'n8n': {'postgres_user': 'app', 'postgres_db': 'appdb', 'postgres_password': 'fake-app'}, 'immich': {'db_username': 'app', 'db_database_name': 'appdb', 'db_password': 'fake-app'}, 'wordpress': {'mysql_user': 'app', 'mysql_database': 'appdb', 'mysql_password': 'fake-app'}}}
            result = restore_database(service, model, target, state)
            self.assertEqual(result['status'], 'passed')
            recovered = database_spec(service, model, target, state)
            if postgres:
                self.assertEqual(pg_sql(recovered, 'SELECT name FROM parent WHERE id=1;', 'appdb'), 'fixture')
                self.assertEqual(pg_sql(recovered, "SELECT tableowner FROM pg_tables WHERE tablename='parent';", 'appdb'), 'app')
                self.assertEqual(pg_sql(recovered, "SELECT rolsuper FROM pg_roles WHERE rolname='app';"), 'f')
                with self.assertRaises(BackupError): pg_sql(recovered, 'INSERT INTO child VALUES(3,999);', 'appdb')
                if extension: self.assertEqual(pg_sql(recovered, 'SELECT count(*) FROM embedding;', 'appdb'), '1')
                if major == '18': self.assertTrue(list(data.glob('18/*/PG_VERSION')))
            else:
                self.assertEqual(mysql_sql(recovered, 'SELECT name FROM parent WHERE id=1;', 'appdb'), 'fixture')
                mysql_sql(recovered, "UPDATE parent SET name='updated' WHERE id=1;", 'appdb')
                self.assertEqual(mysql_sql(recovered, 'SELECT name FROM parent;', 'appdb'), 'UPDATED')
                self.assertIn('UPDATED', mysql_sql(recovered, 'CALL read_parent();', 'appdb'))
                with self.assertRaises(BackupError): mysql_sql(recovered, 'INSERT INTO child VALUES(3,999);', 'appdb')
            self.assertEqual(restore_database(service, model, target, state), result)
            stop_recovery_databases(state)
            self.assertTrue(any(data.iterdir()))
            # Handoff reuses persistent data after validation DB is stopped.
            production = 'recovery-handoff-' + run
            args=['run','-d','--name',production,'--network','none','--volume',str(data)+':'+recovered['mount']+':Z']
            args += ['-e','POSTGRES_PASSWORD=unused','-e','POSTGRES_USER=app','-e','POSTGRES_DB=appdb',image] if postgres else ['-e','MARIADB_ROOT_PASSWORD=unused',image,'--event-scheduler=OFF']
            if extension: args += ['postgres','-c','config_file=/etc/postgresql/postgresql.conf']
            try:
                _docker(args,timeout=180)
                for _ in range(120):
                    try:
                        if postgres:
                            value=_docker(['exec',production,'psql','-U','app','-d','appdb','-Atc','SELECT name FROM parent WHERE id=1;']).decode().strip()
                        else:
                            value=_docker(['exec','-e','MYSQL_PWD',production,'mariadb','-uapp','--host=127.0.0.1','--database=appdb','--batch','--skip-column-names','--execute=SELECT name FROM parent WHERE id=1;'],env={'MYSQL_PWD':'fake-app'}).decode().strip()
                        break
                    except BackupError:
                        if _docker(['inspect','--format','{{.State.Running}}',production]).decode().strip() != 'true':
                            self.fail('handoff database exited during startup')
                        time.sleep(1)
                else: self.fail('handoff database not ready')
                self.assertEqual(value,'fixture' if postgres else 'UPDATED')
                if extension:
                    self.assertIn('1',_docker(['exec',production,'psql','-U','app','-d','appdb','-Atc','SELECT count(*) FROM embedding;']).decode())
            finally: _docker(['rm','-f','-v',production])
        finally:
            if source_name:
                _docker(['rm', '-f', '-v', source_name])
            if state:
                for name in state.get('owned_containers', []): _docker(['rm', '-f', '-v', name])
                for name in state.get('owned_networks', []): _docker(['network', 'rm', name])

    def test_postgres16(self): self.roundtrip('n8n', 'postgres:16.10-alpine', '16')
    def test_postgres18(self): self.roundtrip('n8n', 'postgres:18.0-alpine', '18')
    def test_immich_extensions(self): self.roundtrip('immich', 'ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0', '14', True)
    def test_mariadb_routines_and_definers(self): self.roundtrip('wordpress', 'mariadb:11.4.5', 'mariadb')
