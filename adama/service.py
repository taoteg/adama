import base64
import datetime
import glob
import itertools
import json
import multiprocessing
import os
import re
import ssl
import socket
import subprocess
import tarfile
import textwrap
import tempfile
import threading
import traceback
import uuid
import urlparse
import zipfile
import cStringIO

from enum import Enum
from flask import request, Response, g
from flask.ext import restful
import jinja2
import requests
import ijson
import yaml
from werkzeug.datastructures import FileStorage
import pyswagger
import pyswagger.getter
from PIL import Image

from . import app
from .requestparser import RequestParser
from .api import APIException, RegisterException, ok, api_url_for, error
from .config import Config
from .docker import docker_output, start_container, tail_logs, safe_docker
from .firewall import allow, get_nameservers
from .tools import (location_of, identifier, service_iden,
                    adapter_iden, interleave)
from .tasks import Producer
from .stores import service_store, prov_store, stats_store
from .swagger import swagger
from .namespace import DeleteResponseModel
from .tools import chdir, get_token
from .entity import get_permissions
from .parameters import fix_metadata, metadata_to_swagger
from .stats import tick, get_total_access, get_unique_access, get_users


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
TIMEOUT = 10  # second

# Timout to wait while stopping workers
STOP_TIMEOUT = 5

HERE = location_of(__file__)


class WorkerState(Enum):
    started = 1
    error = 2


class AbstractService(object):

    METADATA_DEFAULT = ''
    PARAMS = [
        # parameter, mandatory?, default
        ('name', True),
        ('namespace', True),
        ('type', True),
        ('code_dir', False, None),
        ('version', False, '0.1'),
        ('url', False, 'http://localhost'),
        ('whitelist', False, []),
        ('description', False, ''),
        ('requirements', False, []),
        ('notify', False, ''),
        ('json_path', False, ''),
        ('main_module', False, 'main'),
        ('users', False, {}),
        ('validate_request', False, False),
        ('validate_response', False, False),
        ('endpoints', False, {}),
        ('sources', False, []),
        ('git_repository', False, ''),
        ('git_branch', False, ''),
        ('registration_timestamp', False, None),
        ('authors', False, []),
        ('icon', False, ''),
        ('tags', False, []),
        ('metadata', False, METADATA_DEFAULT),
        ('timeout', False, 30),
        # private fields (not to be displayed)
        ('_icon', False, None),
        ('_no_firewall', False, None)
    ]

    def __init__(self, **kwargs):
        code_dir = kwargs.get('code_dir')
        if code_dir is not None:
            self.metadata = kwargs.get('metadata', self.METADATA_DEFAULT)
            self.__dict__.update(get_metadata_from(
                os.path.join(code_dir, self.metadata)))
        self.__dict__.update(kwargs)
        self._validate_args()

        self.iden = identifier(self.namespace,
                               self.name,
                               self.version)
        self.adapter_name = adapter_iden(self.name, self.version)

    def _validate_args(self):
        for param in self.PARAMS:
            try:
                getattr(self, param[0])
            except AttributeError:
                if param[1]:
                    raise
                setattr(self, param[0], param[2])
        if not valid_image_name(self.name):
            raise APIException("'{}' is not a valid service name.\n"
                               "Allowed characters: [a-z0-9_.-]"
                               .format(self.name))

    def to_json(self):
        return {key[0]: getattr(self, key[0], key[-1])
                for key in self.PARAMS if not key[0].startswith('_')}

    def make_image(self):
        raise NotImplementedError

    def start_workers(self):
        raise NotImplementedError

    def stop_workers(self):
        raise NotImplementedError

    def check_health(self):
        raise NotImplementedError

    def exec_worker(self, endpoint, args, req):
        """Exercise worker with data from the request.

        ``endpoint`` denotes which endpoint is using the worker (search,
        list).  ``args`` is the validated dictionary of GET arguments,
        if any (note that this method can be called for POST requests too,
        in which case ``args`` is empty).

        ``request`` is the request object.

        """
        raise NotImplementedError


class Service(AbstractService):

    def __init__(self, **kwargs):
        """Initialize service.

        First get metadata from ``code_dir`` and then use ``kwargs``.

        """
        super(Service, self).__init__(**kwargs)

        self.whitelist.insert(0, urlparse.urlparse(self.url).hostname)
        self.whitelist.extend(get_nameservers())
        self.whitelist.append('172.17.42.1')
        self.validate_whitelist()

        self.main_module_path = self.find_main_module()
        self.language = self.detect_language(self.main_module_path)
        self.state = None
        self.workers = []

    def to_json(self):
        obj = super(Service, self).to_json()
        obj['language'] = self.language
        obj['workers'] = self.workers
        try:
            obj['self'] = api_url_for('service',
                                      namespace=self.namespace,
                                      service=self.adapter_name)
        except RuntimeError:
            # no app context, ignore 'self' field
            pass
        return obj

    def endpoint_names(self):
        if self.type == 'passthrough':
            return ['access']
        else:
            return ['search', 'list']

    def process_icon(self):
        """Process and save icon to database."""

        if self.code_dir is None:
            return
        try:
            icon = Image.open(
                os.path.join(self.code_dir, self.metadata, self.icon))
        except IOError:
            return
        resized = icon
        buffer = cStringIO.StringIO()
        resized.save(buffer, 'PNG')
        self._icon = buffer.getvalue()

    def make_image(self):
        """Make a docker image for this service."""

        if self.type == 'passthrough':
            return
        render_template(
            os.path.dirname(self.main_module),
            os.path.basename(self.main_module_path),
            self.language,
            self.requirements,
            into=self.code_dir)
        self.build()

    def find_main_module(self):
        """Find the path to the ``main_module``."""

        if self.type == 'passthrough':
            return None
        directory, basename = os.path.split(self.main_module)
        module, ext = os.path.splitext(basename)
        if ext:
            # if the module include the extension, just return its absolute
            #  path
            return os.path.join(self.code_dir, self.main_module)

        # Otherwise, try to find the proper module, by assuming that there
        # is only one file with such name.  Note that this may fail if
        # there are other files such as byte-compiled binaries, etc.
        found = glob.glob(os.path.join(self.code_dir, directory, module+'.*'))
        if not found:
            raise APIException('module not found: {}'
                               .format(self.main_module), 400)

        return found[0]

    def detect_language(self, module):
        if not module:
            return None
        _, ext = os.path.splitext(module)
        try:
            return EXTENSIONS[ext]
        except KeyError:
            raise APIException('unknown extension {0}'.format(ext), 400)

    def validate_whitelist(self):
        """Make sure ip's and domain name can be resolved."""

        if isinstance(self.whitelist, dict):
            # make sure this method is idempotent
            return

        policies = {}
        for addr in self.whitelist:
            if isinstance(addr, dict):
                addr = normalize_case(addr)
                policies.update(addr)
                addr = addr.keys()[0]
            else:
                policies[addr] = {}
            try:
                socket.gethostbyname_ex(addr)
            except:
                raise APIException(
                    "'{}' does not look like an ip or domain name"
                    .format(addr), 400)
        self.whitelist = policies

    def build(self):
        if self.type == 'passthrough':
            return
        with chdir(self.code_dir):
            safe_docker('build', '-t', self.iden, '.')

    def start_workers(self, n=None):
        if self.type == 'passthrough':
            return
        if self.language is None:
            raise APIException('language of adapter not detected yet', 500)
        if n is None:
            n = Config.getint(
                'workers', '{}_instances'.format(self.language))
        self.workers = [self.start_worker() for _ in range(n)]

    def start_worker(self):
        worker = start_container(
            self.iden,          # image name
            '--queue-host',
            Config.get('queue', 'host'),
            '--queue-port',
            Config.get('queue', 'port'),
            '--queue-name',
            self.iden,
            '--adapter-type',
            self.type)
        if not getattr(self, '_no_firewall', False):
            allow(worker, self.whitelist)
        docker_output('exec', worker, 'touch', '/ready')
        return worker

    def stop_workers(self):
        if self.type == 'passthrough':
            return
        threads = []
        for worker in self.workers:
            threads.append(self.async_stop_worker(worker))
        for thread in threads:
            thread.join(STOP_TIMEOUT)
        self.workers = []

    def async_stop_worker(self, worker):
        thread = threading.Thread(target=docker_output,
                                  args=('rm', '-f', worker))
        thread.start()
        return thread

    def check_health(self):
        """Check that all workers started ok."""

        if self.type == 'passthrough':
            return True

        q = multiprocessing.Queue()

        def log(wkr, qq):
            producer = tail_logs(wkr, timeout=TIMEOUT)
            v = (check(producer), wkr)
            qq.put(v)

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

    def exec_worker(self, endpoint, args, req):
        """Process a request through the worker."""

        if self.validate_request:
            params = validate_swagger_request(self, endpoint, req)
            if args is not None:
                args.update(params)

        tick(self, req, endpoint=endpoint, args=args)

        meth = getattr(self, 'exec_worker_{}'.format(self.type))
        return meth(endpoint, args, req)

    def exec_worker_query(self, endpoint, args, req):
        """Send ``args`` to ``queue`` in QueryWorker model."""

        queue = self.iden
        args['_namespace'] = self.namespace
        args['_adapter'] = self.adapter_name
        args['_endpoint'] = endpoint
        args['_headers'] = dict(req.headers)
        args['_token'] = get_token(req.headers)
        args['_url'] = (Config.get('server', 'api_url') +
                        Config.get('server', 'api_prefix'))
        args['_queue_host'] = qh = Config.get('queue', 'host')
        args['_queue_port'] = qp = Config.getint('queue', 'port')
        args['_store_host'] = Config.get('store', 'host')
        args['_store_port'] = Config.getint('store', 'port')
        args['_queue_name'] = queue
        client = Producer(queue_host=qh, queue_port=qp, queue_name=queue)
        client.send(args)
        gen = itertools.imap(json.dumps,
                             client.receive(max_wait=self.timeout))
        header = next(gen)
        key = uuid.uuid4().hex
        header_json = json.loads(header)
        header_json['sources'] = self.sources
        prov_store[key] = header_json

        probe, real_gen = itertools.tee(gen)
        try:
            first_record = json.loads(next(probe))
            if 'error' in first_record and 'traceback' in first_record:
                raise APIException(first_record['error'])
        except StopIteration:
            pass

        result = result_generator(real_gen, lambda: client.metadata)
        response = Response(result, mimetype='application/json')
        # store and add header for: client.metadata['prov']
        response.headers['Link'] = ('{}; rel="http://www.w3.org/ns/prov'
                                    '#has_provenance"').format(
            api_url_for('prov',
                        namespace=self.namespace,
                        service=self.adapter_name,
                        uuid=key))
        return response

    def exec_worker_map_filter(self, endpoint, args, req):
        """Forward request and process response.

        Forward the request to the third party service, and map the
        response through the ``process`` user function.

        """
        del args
        if endpoint != 'search':
            raise APIException("service of type 'map_filter' does "
                               "not support /list")

        if is_https(self.url) and req.method == 'GET':
            method = tls1_get
        else:
            method = getattr(requests, req.method.lower())
        try:
            headers = {'Authorization': req.headers['Authorization']}
        except KeyError:
            headers = {}
        response = method(self.url,
                          params=req.args,
                          headers=headers,
                          stream=True)
        if response.ok:
            path = '.'.join(filter(None, [self.json_path, 'item']))
            results = ijson.items(FileLikeWrapper(response), path)

            headers = req.headers
            response = Response(
                result_generator(process_by_client(self, results, headers),
                                 lambda: {}),
                mimetype='application/json')

            key = uuid.uuid4().hex
            prov_store[key] = {'sources': self.sources}

            response.headers['Link'] = ('{}; rel="http://www.w3.org/ns/prov'
                                        '#has_provenance"').format(
                api_url_for('prov',
                            namespace=self.namespace,
                            service=self.adapter_name,
                            uuid=key))
            return response
        else:
            raise APIException('response from external service: {}'
                               .format(response))

    def exec_worker_generic(self, endpoint, args, req):
        queue = self.iden
        args['_namespace'] = self.namespace
        args['_adapter'] = self.adapter_name
        args['_endpoint'] = endpoint

        args['_headers'] = dict(req.headers)
        args['_token'] = get_token(req.headers)
        args['_url'] = (Config.get('server', 'api_url') +
                        Config.get('server', 'api_prefix'))
        args['_queue_host'] = Config.get('queue', 'host')
        args['_queue_port'] = Config.getint('queue', 'port')
        args['_store_host'] = Config.get('store', 'host')
        args['_store_port'] = Config.getint('store', 'port')

        client = Producer(queue_host=Config.get('queue', 'host'),
                          queue_port=Config.getint('queue', 'port'),
                          queue_name=queue)
        client.send(args)
        result = client.receive(max_wait=self.timeout)
        header = next(result)
        print('header', header)
        response = list(result)
        if len(response) != 1:
            raise APIException(
                'Wrong return type of generic adapter: got {} results'
                .format(len(response)))
        response = response[0]
        if 'error' not in response:
            resp = Response(base64.b64decode(response['body']),
                            content_type=response['content_type'])
        else:
            resp = Response(json.dumps(response),
                            content_type='application/json')

        key = uuid.uuid4().hex
        prov_store[key] = {'sources': self.sources}

        resp.headers['Link'] = ('{}; rel="http://www.w3.org/ns/prov'
                                '#has_provenance"').format(
            api_url_for('prov',
                        namespace=self.namespace,
                        service=self.adapter_name,
                        uuid=key))

        return resp

    def exec_worker_passthrough(self, endpoint, args, req):
        """Pass a request straight to a pre-defined url.

        ``endpoint`` is what comes after the /access endpoint, and it
        should be added to the final url.

        """
        del args
        method = getattr(requests, req.method.lower())
        data = req.data if req.data else req.form
        url = _join(self.url, endpoint)
        headers = dict(self.filter_headers(url, req))
        response = method(url, params=req.args, data=data, headers=headers,
                          stream=True)
        resp = Response(
            response=response.raw.read(),
            status=response.status_code,
            headers=response.headers.items())

        key = uuid.uuid4().hex
        prov_store[key] = {'sources': self.sources}

        resp.headers['Link'] = ('{}; rel="http://www.w3.org/ns/prov'
                                '#has_provenance"').format(
            api_url_for('prov',
                        namespace=self.namespace,
                        service=self.adapter_name,
                        uuid=key))

        return resp

    def filter_headers(self, url, req):
        """Filter headers according to policy for this url. """

        domain = urlparse.urlsplit(url).netloc
        full_headers = req.headers
        policy = self.whitelist[domain]
        for header, value in full_headers.items():
            if header.lower() in policy.get('forward', []):
                yield header, value


class ServiceQueryResource(restful.Resource):

    @swagger.operation(
        notes=textwrap.dedent(
            """Perform a search query using the adapter registered for this
            service.

            <p>The parameters and response type of this operation are
            dependent on the particular service.</p>

            """),
        nickname='search'
    )
    def get(self, namespace, service):
        """Perform a query using a service"""

        args = self.validate_get()
        try:
            iden = service_iden(namespace, service)
            srv = service_store[iden]['service']
        except KeyError:
            raise APIException('service not found: {}'
                               .format(service_iden(namespace, service)),
                               404)

        return srv.exec_worker('search', args, request)

    def validate_get(self):
        # no defined query language yet
        # accept everything
        return {key: val[0] for key, val in dict(request.args).items()}


class ServiceListResource(restful.Resource):

    @swagger.operation(
        notes=textwrap.dedent(
            """Perform a list query using the adapter registered for this
            service.

            <p>This query takes no parameters other than pagination
            specific parameters. It returns a list of objects.</p>

            """),
        nickname='list'
    )
    def get(self, namespace, service):
        """List all objects using a service"""

        args = self.validate_get()
        try:
            iden = service_iden(namespace, service)
            srv = service_store[iden]['service']
        except KeyError:
            raise APIException('service not found: {}'
                               .format(service_iden(namespace, service)),
                               404)

        return srv.exec_worker('list', args, request)

    def validate_get(self):
        return {key: val[0] for key, val in dict(request.args).items()}


@swagger.model
class ServiceModel(object):

    resource_fields = {
        'name': restful.fields.String(attribute='name of the service'),
        'namespace': restful.fields.String(
            attribute='namespace of the service'),
        'type': restful.fields.String(attribute='type of the adapter'),
        'code_dir': restful.fields.String(
            attribute='(internal) location of adapter code in the server'),
        'version': restful.fields.String(attribute='version of the adapter'),
        'url': restful.fields.String(
            attribute='url of third party data service'),
        'self': restful.fields.String(
            attribute='url to access this service'),
        'whitelist': restful.fields.List(
            restful.fields.String,
            attribute="ip's or domains the adapter can access"),
        'description': restful.fields.String(
            attribute='description of the service'),
        'requirements': restful.fields.List(
            restful.fields.String,
            attribute='third party packages needed by the adapter'),
        'notify': restful.fields.String(
            attribute='url to notify via POST when adapter is ready'),
        'json_path': restful.fields.String(
            attribute='location of array of results in response'),
        'main_module': restful.fields.String(
            attribute='path to main module'),
        'metadata': restful.fields.String(
            attribute='path to metadata file')
    }


@swagger.model
@swagger.nested(
    result=ServiceModel.__name__
)
class ServiceResponseModel(object):

    resource_fields = {
        'status': restful.fields.String(attribute='success or error'),
        'result': restful.fields.Nested(ServiceModel.resource_fields)
    }


@swagger.model
class ModifyServiceResponseModel(object):

    resource_fields = {
        'status': restful.fields.String(attribute='success or error')
    }


class ServiceResource(restful.Resource):

    @swagger.operation(
        notes="Obtain information about a service.",
        nickname="getService",
        responseClass=ServiceResponseModel.__name__,
        parameters=[
            {
                'name': 'namespace',
                'description': 'namespace of the service',
                'required': True,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'path'
            },
            {
                'name': 'service',
                'description': 'name of the service, including the version',
                'required': True,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'path'
            }
        ]
    )
    def get(self, namespace, service):
        """Get information about a service"""

        full_name = service_iden(namespace, service)
        try:
            srv = service_store[full_name]
            if srv['slot'] == 'ready':
                return ok({
                    'result': {
                        'service': srv['service'].to_json()
                    }
                })
            else:
                return ok({
                    'result': srv
                })
        except KeyError:
            raise APIException(
                "service not found: {}/{}"
                .format(namespace, service),
                404)

    @swagger.operation(
        notes="Delete a service in a namespace. Note that this operation "
              "always succeed. Also, the service is deleted only in the "
              "given namespace, and since the name includes the version, "
              "no other version of the same service is deleted.",
        nickname='deleteService',
        responseClass=DeleteResponseModel.__name__,
        parameters=[
            {
                'name': 'namespace',
                'description': 'namespace of the service',
                'required': True,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'path'
            },
            {
                'name': 'service',
                'description': 'name of the service, including the version',
                'required': True,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'path'
            }
        ]
    )
    def delete(self, namespace, service):
        """Delete a service"""

        name = service_iden(namespace, service)
        try:
            srv = service_store[name]['service']
            if (srv is not None and
                    'DELETE' not in get_permissions(srv.users, g.user)):
                raise APIException(
                    'user {} does not have permissions to DELETE '
                    'the service {}'.format(g.user, name))
            try:
                srv.stop_workers()
                # TODO: need to clean up containers here too
            except Exception:
                # ignore any error while stopping and removing workers
                pass
            del service_store[name]
        except KeyError:
            pass
        return ok({})

    @swagger.operation(
        notes=textwrap.dedent(
            """Modify an existing service.

            <p>This method allows to modify an existing service. Code can
            be updated by specifiying the 'code' parameter, or by setting
            the field 'update_git_repository', in which case the adapter
            performs a "git pull". </p>

            """),
        nickname='modifyService',
        responseClass=ModifyServiceResponseModel.__name__,
        consumes='multipart/form-data',
        parameters=[
            {
                'name': 'namespace',
                'description': 'namespace of the service',
                'required': True,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'path'
            },
            {
                'name': 'service',
                'description': 'name of the service, including the version',
                'required': True,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'path'
            },
            {
                'name': 'metadata',
                'description': 'path of metadata file relative to '
                               'git_repository root',
                'required': False,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'form'
            },
            {
                'name': 'type',
                'description': 'type of the adapter',
                'required': False,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'form'
            },
            {
                'name': 'url',
                'description': 'url of the third party data service',
                'required': False,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'form'
            },
            {
                'name': 'whitelist',
                'description': 'ip or domain to be whitelisted',
                'required': False,
                'allowMultiple': True,
                'dataType': 'string',
                'paramType': 'form'
            },
            {
                'name': 'description',
                'description': 'description of the service',
                'required': False,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'form'
            },
            {
                'name': 'requirements',
                'description': 'third party package needed by the adapter',
                'required': False,
                'allowMultiple': True,
                'dataType': 'string',
                'paramType': 'form'
            },
            {
                'name': 'notify',
                'description': 'url to notify when adapter is ready',
                'required': False,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'form'
            },
            {
                'name': 'json_path',
                'description': 'location of the array of result in response',
                'required': False,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'form'
            },
            {
                'name': 'main_module',
                'description': 'path of main module relative to '
                               'git_repository root',
                'required': False,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'form'
            },
            {
                'name': 'code',
                'description': 'type of the adapter',
                'required': False,
                'allowMultiple': False,
                'dataType': 'File',
                'paramType': 'form'
            },
            {
                'name': 'update_git_repository',
                'description': ('whether to update the git repository via'
                                '"git pull"'),
                'required': False,
                'allowMultiple': False,
                'dataType': 'string',
                'paramType': 'form'
            }
        ]
    )
    def put(self, namespace, service):
        """Modify a service"""

        name = service_iden(namespace, service)
        try:
            slot = service_store[name]
        except KeyError:
            raise APIException('service not found: {}'.format(name), 404)

        old_srv = slot['service']
        if 'PUT' not in get_permissions(old_srv.users, g.user):
            raise APIException(
                'user {} does not have permissions to PUT to '
                'service {}'.format(g.user, name))

        args = self.validate_put()
        if old_srv is None:
            raise APIException('service not ready: {}'.format(name), 400)
        old_srv.stop_workers()
        slot['slot'] = 'free'
        slot['service'] = None
        service_store[name] = slot
        # if args.update, then update the git repo
        if args.get('update_git_repository', False):
            with chdir(old_srv.code_dir):
                subprocess.check_call('git pull'.split())
        register(
            Service, args, namespace, old_srv.code_dir, post_notifier)
        return ok({})

    @staticmethod
    def validate_put():
        parser = RequestParser()
        parser.add_argument('type', type=str)
        parser.add_argument('url', type=str)
        parser.add_argument('whitelist', type=str, action='append')
        parser.add_argument('description', type=str)
        parser.add_argument('requirements', type=str, action='append')
        parser.add_argument('notify', type=str)
        parser.add_argument('json_path', type=str)
        parser.add_argument('main_module', type=str)
        parser.add_argument('code', type=FileStorage, location='files')
        parser.add_argument('update_git_repository', type=bool)
        parser.add_argument('metadata', type=str)

        args = parser.parse_args()

        for key, value in args.items():
            if value is None:
                del args[key]

        return args


class ServiceHealthResource(restful.Resource):

    @staticmethod
    def _is_running(worker):
        running = docker_output(
            'inspect', '-f', '{{.State.Running}}', worker).strip()
        return running == 'true'

    def get(self, namespace, service):
        name = service_iden(namespace, service)
        try:
            slot = service_store[name]
        except KeyError:
            raise APIException('service not found: {}'.format(name), 404)
        srv = slot['service']
        workers_alive = len([worker for worker in srv.workers
                             if self._is_running(worker)])
        should_have = int(request.args.get('workers', 1))
        app.logger.debug(str(srv.workers))
        app.logger.debug('workers = {}'.format(workers_alive))
        return workers_alive >= should_have


class IconResource(restful.Resource):

    def get(self, namespace, service):
        name = service_iden(namespace, service)
        try:
            slot = service_store[name]
        except KeyError:
            raise APIException('service not found: {}'.format(name), 404)
        srv = slot['service']
        if getattr(srv, '_icon', None):
            return Response(srv._icon, content_type='image/png')
        else:
            raise APIException("no icon", code=404)


class StatsResource(restful.Resource):

    @swagger.operation(
        notes="Return statistics of usage of the service.",
        nickname='getStats',
        parameters=[]
    )
    def get(self, namespace, service):
        srv = get_service(namespace, service)
        stats = stats_store.get(srv.iden, [])
        total_access = get_total_access(stats)
        unique_access = get_unique_access(stats)
        users = get_users(stats)
        return ok({
            'total_access': total_access,
            'unique_access': unique_access,
            'users': users
        })


class FileLikeWrapper(object):

    def __init__(self, response):
        self.response = response
        self.it = self.response.iter_content(chunk_size=1)
        self.src = itertools.chain.from_iterable(self.it)

    def read(self, n=512):
        return ''.join(itertools.islice(self.src, 0, n))


def normalize_case(obj):
    """Convert keys and values to lowercase, recursively. """

    if isinstance(obj, basestring):
        return obj.lower()
    if isinstance(obj, list):
        return [normalize_case(elt) for elt in obj]
    if isinstance(obj, dict):
        return {k.lower(): normalize_case(v) for k, v in obj.items()}
    return obj


def process_by_client(service, results, headers):
    """Process results through a ProcessWorker.

    Return a generator which produces JSON objects (as strings).

    """

    client = Producer(
        queue_host=Config.get('queue', 'host'),
        queue_port=Config.getint('queue', 'port'),
        queue_name=service.iden)
    for result in results:
        result['_headers'] = dict(headers)
        result['_token'] = get_token(headers)
        result['_url'] = (Config.get('server', 'api_url') +
                          Config.get('server', 'api_prefix'))
        result['_queue_host'] = Config.get('queue', 'host')
        result['_queue_port'] = Config.getint('queue', 'port')
        result['_store_host'] = Config.get('store', 'host')
        result['_store_port'] = Config.getint('store', 'port')
        client.send(result)
        response = client.receive(max_wait=service.timeout)
        header = next(response)
        print('header', header)
        for obj in response:
            yield json.dumps(obj)
            if 'error' in obj:
                # abort as soon as there is an error
                return


def result_generator(results, metadata):
    """Construct JSON response from ``results``.

    ``results`` is a generator that produces JSON objects, and
    ``metadata`` is a function that returns extra information.

    The reason for metadata being a function is to be able to collect
    information after the ``results`` generator has been exhausted
    (for example: timing).

    """
    try:
        yield '{"result": [\n'
        for line in interleave(['\n, '], results):
            yield line
        yield '\n],\n'
        md = metadata()
        yield '"metadata": {0},\n'.format(json.dumps({
            'time_in_main': md.get('time_in_main', None)
        }))
        yield '"status": "success"}\n'
    except Exception:
        exc = traceback.format_exc()
        yield '---\n'
        yield exc


def is_https(url):
    return urlparse.urlparse(url).scheme == 'https'


class TLSv1Adapter(requests.adapters.HTTPAdapter):
    """Adapter to support TLSv1 in requests."""

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = requests.packages.urllib3.poolmanager.PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_version=ssl.PROTOCOL_TLSv1)


def tls1_get(*args, **kwargs):
    session = requests.Session()
    session.mount('https://', TLSv1Adapter())
    kwargs['verify'] = False
    return session.get(*args, **kwargs)


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


def render_template(main_module_path, main_module_name,
                    language, requirements, into):
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
        main_module_path=main_module_path,
        main_module_name=main_module_name,
        language=language,
        requirement_cmds=requirement_cmds)
    with open(os.path.join(into, 'Dockerfile'), 'w') as f:
        f.write(dockerfile)


def requirements_installer(language, requirements):
    """Return the command to install requirements.

    ``requirements`` is a list of packages to be installed in the
    environment for ``language``.

    """
    _, installer = LANGUAGES[language]
    return installer.format(package=' '.join(requirements))


def get_metadata_from(directory):
    """Return a dictionary from the metadata file in ``directory``.

    Try to read the file ``metadata.yml`` or ``metadata.json`` at the root
    of the directory.

    Concatenate files ``foo.yml`` into a field ``foo:``.

    Return an empty dict if no file is found.

    :type directory: str
    :rtype: dict[str, object]

    """
    md = {}
    exts = ['yml', 'yaml', 'json']
    for filename in ['metadata.{}'.format(ext) for ext in exts]:
        try:
            f = open(os.path.join(directory, filename))
        except IOError:
            continue
        md.update(yaml.load(f))
    for extra in os.listdir(directory):
        if extra.startswith('metadata'):
            continue
        if any(extra.endswith(ext) for ext in exts):
            f = open(os.path.join(directory, extra))
            key, _ = os.path.splitext(extra)
            md[key] = yaml.load(f)
    return md


def valid_image_name(name):
    return re.search(r'[^a-z0-9-_.]', name) is None


def _join(url, endpoint):
    """Join endpoint at the end of url.

    Take care of considering the slashes at the end of ``url`` and at the
    beginning of ``endpoint``, each one with the proper semantics.

    """
    if not endpoint:
        return url
    parsed_url = urlparse.urlsplit(url)
    new_path = os.path.join(parsed_url.path, endpoint)
    parts = list(parsed_url)
    parts[2] = new_path
    return urlparse.urlunsplit(parts)


def register_code(args, namespace, notifier=None):
    """Register code that comes in the POST request."""

    user_code = None
    if getattr(args, 'code', None):
        filename = args.code.filename
        args.code = args.code.stream.read()
        tempdir = tempfile.mkdtemp()
        user_code = extract(filename, args.code, tempdir)
    return register(Service, args, namespace, user_code, notifier)


def register_git_repository(args, namespace, notifier=None):
    """Register code from git repo at ``args.git_repository``."""

    tempdir = tempfile.mkdtemp()
    with chdir(tempdir):
        cmd = ['git', 'clone']
        if getattr(args, 'git_branch', None):
            cmd.extend(['-b', args.git_branch])
        cmd.extend([args.git_repository, 'user_code'])
        subprocess.check_call(cmd)
    return register(Service, args, namespace,
                    os.path.join(tempdir, 'user_code'), notifier)


def register(service_class, args, namespace, user_code, notifier=None):
    """Register a service in a namespace.

    ``args`` is a dictionary with POST parameters. ``user_code`` is the
    directory where the user's code is checked out.

    Create the proper image, launch workers, and save the service in
    the store.

    """
    try:
        user = g.user
    except RuntimeError:
        user = 'anonymous'
    service = service_class(
        namespace=namespace, code_dir=user_code,
        users={user: ['POST', 'PUT', 'DELETE'], 'admin': ['POST', 'PUT', 'DELETE']},
        **dict(args))
    try:
        slot = service_store[service.iden]['slot']
    except KeyError:
        slot = 'free'

    # make sure to only use free or errored out slots
    if slot not in ('free', 'error'):
        raise APIException("service slot not available: {}\n"
                           "Current state: {}"
                           .format(service.iden, slot), 400)

    service_store[service.iden] = {
        'slot': 'busy',
        'msg': 'Empty service created',
        'stage': 1,
        'total_stages': 6,
        'service': None
    }

    service.registration_timestamp = datetime.datetime.now().isoformat(' ')
    _async_register(service, notifier)
    return service


def _async_register(service, notifier):
    """Launch async process for actual registration."""

    proc = multiprocessing.Process(
        name='Async Registration {}'.format(service.iden),
        target=_register, args=(service, notifier))
    proc.start()


def _register(service, notifier=None):
    """Register and start a service."""

    full_name = service.iden
    slot = service_store[full_name]
    try:
        slot['msg'] = 'Async image creation started'
        slot['stage'] = 2
        service_store[full_name] = slot

        service.make_image()

        slot['msg'] = 'Image for service created'
        slot['stage'] = 3
        service_store[full_name] = slot

        service.start_workers()

        slot['msg'] = 'Workers started'
        slot['stage'] = 4
        service_store[full_name] = slot

        service.process_icon()

        slot['msg'] = 'Icon processed'
        slot['stage'] = 5
        service_store[full_name] = slot

        service.check_health()

        slot['msg'] = 'Service ready'
        slot['stage'] = 6
        slot['slot'] = 'ready'
        slot['service'] = service
        service_store[full_name] = slot

        result = ok
        data = service
    except Exception as exc:
        slot['msg'] = 'Error: {}'.format(exc)
        slot['slot'] = 'error'
        service_store[full_name] = slot

        result = error
        data = str(exc)

    if service.notify and notifier is not None:
        notifier(service.notify, result, data)


def post_notifier(url, result, data):
    """Do a post notification to ``url``.

    ``result`` is a function, and ``data`` an object.

    """
    if result is ok:
        content = result({
            'message': 'Registration successful',
            'result': {
                'search': api_url_for(
                    'service',
                    namespace=data.namespace,
                    service=data.adapter_name)
            }
        })
    else:
        content = result({
            'message': 'Registration failed',
            'result': {
                'error': data
            }
        })
    try:
        requests.post(url,
                      headers={"Content-Type": "application/json"},
                      data=json.dumps(content))
    except Exception:
        app.logger.warning(
            "Could not notify url '{}'"
            .format(url))


def debug_notifier(url, result, data):
    print(url, result, data)


def extract(filename, code, into):
    """Extract code from string ``code``.

    ``filename`` is the name of the uploaded file.  Extract the code
    in directory ``into``.

    Return the directory where the user code is extracted (may differ from
    the original ``into``).

    """

    _, ext = os.path.splitext(filename)
    user_code_dir = os.path.join(into, 'user_code')
    os.mkdir(user_code_dir)
    contents = code

    if ext in ZIPS:
        # it's a zip file
        zip_file = os.path.join(into, 'contents.zip')
        with open(zip_file, 'w') as f:
            f.write(contents)
        zipball = zipfile.ZipFile(zip_file)
        zipball.extractall(user_code_dir)

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

    return user_code_dir


def get_service(namespace, service):
    """

    :type namespace: str
    :type service: str
    :rtype: Service
    """
    name = service_iden(namespace, service)
    try:
        slot = service_store[name]
        srv = slot['service']
        if srv is None:
            raise APIException('service is not ready: {}'.format(name), 400)
        return srv
    except KeyError:
        raise APIException('service not found: {}'.format(name), 404)


def get_swagger(srv):
    md = srv.to_json()
    fixed_md = fix_metadata(md)
    return metadata_to_swagger(fixed_md)


class JsonGetter(pyswagger.getter.Getter):

    def __init__(self, obj):
        super(JsonGetter, self).__init__('')
        self.urls = [('', '')]
        self.result_obj = obj

    def load(self, path):
        return json.dumps(self.result_obj)


def multi_to_dict(md, parameters):
    """

    :type md: list[(str, object)]
    :type parameters: Iterable
    :rtype: dict[str, object]
    """
    d = {}
    param_dict = {p.name: p for p in parameters}
    for k, v in md:
        if param_dict[k].type == 'array':
            d.setdefault(k, []).append(v)
        elif param_dict[k].type == 'boolean':
            d[k] = v == 'True'
        else:
            d[k] = v
    return d

def to_bool(arg):
    if arg.lower() in ('true', 'yes'):
        return True
    else:
        return ''

def validate_swagger_request(srv, endpoint, req):
    sw = get_swagger(srv)
    getter = JsonGetter(sw)
    sw_app = pyswagger.SwaggerApp.load('', getter=getter)
    sw_app.prepare(strict=True)
    operation = '{}_{}'.format(endpoint, req.method.lower())
    args = {k: v for (k, v) in req.args.to_dict(flat=False).items()
            if not k.startswith('_')}
    op = sw_app.op[operation]
    for param in op.parameters:
        if param.type != 'array':
            try:
                args[param.name] = args[param.name][0]
                if param.type == 'boolean':
                    args[param.name] = to_bool(args[param.name])
            except (KeyError, IndexError):
                pass
    try:
        (sw_req, _) = op(**args)
    except ValueError as exc:
        raise APIException(exc.message)
    d = multi_to_dict(sw_req.query, op.parameters)

    def _optional_defaultless_not_given(spec_arg):
        # True if an argument is optional with no default and it was not
        # given in the request.
        return (not spec_arg.required and
                spec_arg.default is None and
                spec_arg.name not in args)

    params = {o.name: o._prim_(d[o.name])
              for o in op.parameters
              if not _optional_defaultless_not_given(o)}
    return params
