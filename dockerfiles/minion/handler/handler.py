#!/usr/bin/env python
from functools import wraps
import json
import os
import sys
import traceback
import cStringIO

from serf_master import SerfHandler, SerfHandlerProxy
import docker_utils
import firewall


MAX_OUTPUT = 1000


def truncated_stdout(f):
    """Decorator to truncate stdout to final `MAX_OUTPUT` characters. """

    @wraps(f)
    def wrapper(*args, **kwargs):
        old_stdout = sys.stdout
        old_stdout.flush()
        sys.stdout = cStringIO.StringIO()
        out = ''
        try:
            result = f(*args, **kwargs)
            out = sys.stdout.getvalue() + '\nSUCCESS'
            return result
        except Exception:
            out = traceback.format_exc() + '\nERROR'
        finally:
            sys.stdout = old_stdout
            print out[-MAX_OUTPUT:]
    return wrapper


class MinionHandler(SerfHandler):

    @truncated_stdout
    def start(self):
        image, num_workers = sys.stdin.read().split()
        registry = os.environ['LOCAL_REGISTRY']
        remote_image = '{}/{}'.format(registry, image)
        docker_utils.docker('pull', remote_image)
        for i in range(int(num_workers)):
            _start(remote_image)


def _start(image):
    """Start a container from `image`. """

    environment = docker_utils.env(image)
    whitelist = json.loads(environment.get('WHITELIST', '[]'))
    c = docker_utils.docker('run', '-d', image, 'sleep', 'infinity').strip()
    firewall.allow(c, whitelist)
    docker_utils.docker('exec', c, 'touch', '/ready')
    print c


if __name__ == '__main__':
    handler = SerfHandlerProxy()
    handler.register('minion', MinionHandler())
    handler.run()