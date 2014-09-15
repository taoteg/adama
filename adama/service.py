import glob
import itertools
import json
import multiprocessing
import os
import socket
import tarfile
import tempfile
import threading
import urlparse
import zipfile

from enum import Enum
from flask import request, Response
from flask.ext import restful
import jinja2

from .api import APIException, RegisterException
from .config import Config
from .docker import docker_output, start_container, tail_logs
from .firewall import Firewall
from .tools import (location_of, identifier, service_iden,
                    interleave)
from .tasks import Producer
from .service_store import service_store


LANGUAGES = {
    'python': ('py', 'pip install {package}'),
    'ruby': ('rb', 'gem install {package}'),
    'javascript': ('js', 'npm install -g {package}'),
    'lua': ('lua', None),
    'java': ('jar', None)
}

EXTENSIONS = {
    '.py': 'python',
    '.js': 'javascript',
    '.rb': 'ruby',
    '.jar': 'java',
    '.lua': 'lua'
}

TARBALLS = ['.tar', '.gz', '.tgz']
ZIPS = ['.zip']

# Timeout to wait for output of containers at start up, before
# declaring them dead
TIMEOUT = 3  # second

# Timout to wait while stopping workers
STOP_TIMEOUT = 5

HERE = location_of(__file__)


class WorkerState(Enum):
    started = 1
    error = 2


class ParameterizedError(Exception):
    pass


class Parameterized(object):

    def __init__(self, params, dic):
        self._params = set(params)
        keys = set(dic.keys())
        if not self._params.issubset(keys):
            raise ParameterizedError(
                'missing parameters: {}'.
                format(', '.join(self._params - keys)))
        self.__dict__.update(dic)

    def to_json(self):
        return {key: getattr(self, key) for key in self._params}


class Service(Parameterized):

    PARAMS = [
        'name',
        'version',
        'namespace',
        'url',
        'whitelist',
        'description',
        'requirements',
        'notify',
        'adapter',
        'type'
    ]

    def __init__(self, code, **kwargs):
        super(Service, self).__init__(self.PARAMS, kwargs)
        self.code = code

        self.iden = identifier(**kwargs)
        self.whitelist.append(urlparse.urlparse(self.url).hostname)
        self.validate_whitelist()

        self.temp_dir = create_temp_dir()
        self.firewall = Firewall(self.whitelist)
        self.state = None
        self.language = None
        self.workers = []

    def extract_code(self):
        extract(self.adapter, self.code, into=self.temp_dir)

    def make_image(self):
        """Make a docker image for this service."""

        self.extract_code()
        self.language = self.detect_language()
        render_template(self.language, self.requirements, into=self.temp_dir)
        self.save_metadata()
        self.build()

    def detect_language(self):
        ext = extension(self.adapter)
        try:
            return EXTENSIONS[ext]
        except KeyError:
            if ext in TARBALLS + ZIPS:
                main = os.path.join(self.temp_dir, 'user_code/main.*')
                try:
                    ext = extension(glob.glob(main)[0])
                    return EXTENSIONS[ext]
                except IndexError:
                    raise APIException('did not find a "main" module', 400)
            raise APIException('unknown extension {0}'.format(ext), 400)

    def validate_whitelist(self):
        """Make sure ip's and domain name can be resolved."""

        for addr in self.whitelist:
            try:
                socket.gethostbyname_ex(addr)
            except:
                raise APIException(
                    "'{}' does not look like an ip or domain name"
                    .format(addr), 400)

    def save_metadata(self):
        with open(os.path.join(self.temp_dir, 'metadata.json'), 'w') as f:
            f.write(json.dumps(self.to_json()))

    def build(self):
        prev_cwd = os.getcwd()
        os.chdir(self.temp_dir)
        try:
            output = docker_output('build', '-t', self.iden, '.')
            print(output)
        finally:
            os.chdir(prev_cwd)

    def start_workers(self, n=None):
        if self.language is None:
            raise APIException('language of adapter not detected yet', 500)
        if n is None:
            n = Config.getint(
                'workers', '{}_instances'.format(self.language))
        self.workers = [self.start_worker() for _ in range(n)]

    def start_worker(self):
        worker, iface, ip = start_container(
            self.iden,          # image name
            '--queue-host',
            Config.get('queue', 'host'),
            '--queue-port',
            Config.get('queue', 'port'),
            '--queue-name',
            self.iden,
            '--adapter-type',
            self.type)
        self.firewall.register(worker, iface)
        return worker

    def stop_workers(self):
        threads = []
        for worker in self.workers:
            threads.append(self.async_stop_worker(worker))
        for thread in threads:
            thread.join(STOP_TIMEOUT)

    def async_stop_worker(self, worker):
        self.firewall.unregister(worker)
        thread = threading.Thread(target=docker_output,
                                  args=('kill', worker))
        thread.start()
        return thread

    def check_health(self):
        """Check that all workers started ok."""

        q = multiprocessing.Queue()

        def log(worker, q):
            producer = tail_logs(worker, timeout=TIMEOUT)
            v = (check(producer), worker)
            q.put(v)

        # Ask for logs from workers. We do it in processes so we can
        # poll for a little while the workers in parallel.
        ts = []
        for worker in self.workers:
            t = multiprocessing.Process(target=log, args=(worker, q),
                                        name='Worker log {}'.format(worker))
            t.start()
            ts.append(t)
        # wait for all processes (the timeout guarantees they'll finish)
        for t in ts:
            t.join()

        logs = []
        while not q.empty():
            state, worker = q.get()
            if state == WorkerState.error:
                logs.append(docker_output('logs', worker))

        if logs:
            raise RegisterException(len(self.workers), logs)


class ServiceQueryResource(restful.Resource):

    def get(self, namespace, service):
        args = self.validate_get()
        try:
            iden = service_iden(namespace, service)
            srv = service_store[iden]
        except KeyError:
            raise APIException('service {} not found'
                               .format(service_iden(namespace, service)))

        queue = srv.iden

        if srv.type == 'QueryWorker':
            return exec_query_worker(args, queue)
        if srv.type == 'ProcessWorker':
            return exec_process_worker(args, queue)

    def validate_get(self):
        # no defined query language yet
        # accept everything
        return dict(request.args)


class ServiceResource(restful.Resource):

    def get(self, namespace, service):
        full_name = service_iden(namespace, service)
        try:
            srv = service_store[full_name]
            if isinstance(srv, basestring):
                # Service creation ongoing
                return {
                    'status': 'success',
                    'result': {
                        'state': srv
                    }
                }
            else:
                return {
                    'status': 'success',
                    'result': {
                        'service': srv.to_json(),
                        'code': srv.code
                    }
                }
        except KeyError:
            raise APIException(
                "service {}/{} not found"
                .format(namespace, service))


def exec_process_worker(args, queue):
    pass


def exec_query_worker(args, queue):
    """Send ``args`` to ``queue`` in QueryWorker model."""

    client = Producer(queue_host=Config.get('queue', 'host'),
                      queue_port=Config.getint('queue', 'port'),
                      queue_name=queue)
    client.send(args)

    def result_generator():
        yield '{"result": [\n'
        gen = itertools.imap(lambda x: json.dumps(x) + '\n',
                             client.receive())
        for line in interleave([', '], gen):
            yield line
        yield '],\n'
        yield '"metadata": {0},\n'.format(json.dumps(client.metadata))
        yield '"status": "success"}\n'

    return Response(result_generator(), mimetype='application/json')


def check(producer):
    """Check status of a container.

    Look at the output of the worker in the container.

    """
    state = WorkerState.error
    for line in producer:
        if line.startswith('*** WORKER ERROR'):
            return WorkerState.error
        if line.startswith('*** WORKER STARTED'):
            state = WorkerState.started
    return state


def create_temp_dir():
        return tempfile.mkdtemp()


def render_template(language, requirements, into):
    """Create Dockerfile for ``language``.

    Consider a list of ``requirements``, and write the Dockerfile in
    directory ``into``.

    """

    dockerfile_template = jinja2.Template(
        open(os.path.join(HERE, 'containers/Dockerfile.adapter')).read())
    requirement_cmds = (
        'RUN ' + requirements_installer(language, requirements)
        if requirements else '')

    dockerfile = dockerfile_template.render(
        language=language, requirement_cmds=requirement_cmds)
    with open(os.path.join(into, 'Dockerfile'), 'w') as f:
        f.write(dockerfile)


def requirements_installer(language, requirements):
    """Return the command to install requirements.

    ``requirements`` is a list of packages to be installed in the
    environment for ``language``.

    """
    _, installer = LANGUAGES[language]
    return installer.format(package=' '.join(requirements))


def extension(filename):
    _, ext = os.path.splitext(filename)
    return ext


def extract(filename, code, into):
    """Extract code from file object ``code``.

    ``filename`` is the name of the uploaded file.  Extract the code
    in directory ``into``.

    """

    ext = extension(filename)
    user_code_dir = os.path.join(into, 'user_code')
    os.mkdir(user_code_dir)
    contents = code

    if ext in ZIPS:
        # it's a zip file
        zip_file = os.path.join(into, 'contents.zip')
        with open(zip_file, 'w') as f:
            f.write(contents)
        zip = zipfile.ZipFile(zip_file)
        zip.extractall(user_code_dir)

    elif ext in TARBALLS:
        # it's a tarball
        tarball = os.path.join(into, 'contents.tgz')
        with open(tarball, 'w') as f:
            f.write(contents)
        tar = tarfile.open(tarball)
        tar.extractall(user_code_dir)

    elif ext in EXTENSIONS.keys():
        # it's a module
        module = os.path.join(user_code_dir, filename)
        with open(module, 'w') as f:
            f.write(contents)

    else:
        raise APIException(
            'unknown extension: {0}'.format(filename), 400)
