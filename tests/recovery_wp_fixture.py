"""Actual pinned PHP-FPM transport; synthetic bootstrap isolates probe behavior.

This is not a WordPress login/schema acceptance test (that requires the VM).
"""
import json
import secrets
import time
from recovery_databases import _docker,BackupError
from recovery_activate import APP_PROBES


def exercise_fpm_probe():
    tag='docker.io/library/wordpress:6.8.3-php8.4-fpm-alpine'
    _docker(['pull',tag],timeout=900)
    image=json.loads(_docker(['image','inspect','--format','{{json .RepoDigests}}',tag]))[0]
    version=_docker(['run','--rm','--network','none',image,'php','-r',"include '/usr/src/wordpress/wp-includes/version.php';echo $wp_version;"]).decode().strip()
    if version!='6.8.3':raise AssertionError('fixture WordPress release mismatch')
    name='recovery-fpm-fixture-'+secrets.token_hex(5)
    try:
        _docker(['run','-d','--name',name,'--network','none',image],timeout=180)
        for _ in range(60):
            try:
                _docker(['exec',name,'php','-r',"$s=@fsockopen('127.0.0.1',9000,$n,$s,1);if(!$s)exit(1);fclose($s);"])
                break
            except BackupError:time.sleep(.5)
        else:raise AssertionError('FPM fixture not ready')
        bootstrap=b"<?php $wpdb=new class{function get_var($query){return '1';}};function is_blog_installed(){return true;}"
        _docker(['exec','-i',name,'sh','-c','cat > /var/www/html/wp-load.php'],input_bytes=bootstrap)
        _docker(['exec',name,*APP_PROBES['wordpress']],timeout=30)
        _docker(['exec','-i',name,'sh','-c','cat > /var/www/html/wp-load.php'],input_bytes=b'<?php exit(0);')
        try:_docker(['exec',name,*APP_PROBES['wordpress']],timeout=30)
        except BackupError:return
        raise AssertionError('early WordPress bootstrap exit must fail readiness')
    finally:_docker(['rm','-f','-v',name])
