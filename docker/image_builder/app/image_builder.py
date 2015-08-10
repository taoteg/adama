#!/usr/bin/env python

import os
import uuid
import threading
from traceback import format_exc
import zipfile
import tarfile
import tempfile
import subprocess

from channelpy import Channel, RabbitConnection
import yaml

from store import Store
import stores
import tools


RABBIT_URI = 'amqp://{}:{}'.format(
    os.environ.get('RABBIT_PORT_5672_TCP_ADDR', '172.17.42.1'),
    os.environ.get('RABBIT_PORT_5672_TCP_PORT', 5672)
)
EXTENSIONS = {
    '.py': 'python',
    '.js': 'javascript',
    '.rb': 'ruby',
    '.jar': 'java',
    '.lua': 'lua'
}
TARBALLS = ['.tar', '.gz', '.tgz']
ZIPS = ['.zip']


def error(msg, code, ch):
    """
    :type msg: Any
    :type code: int
    :type ch: Channel
    """
    ch.put({
        'status': 'error',
        'message': msg,
        'code': code
    })


def extract(filename, code, into):
    """Extract code from string ``code``.

    ``filename`` is the name of the uploaded file.  Extract the code
    in directory ``into``.

    Return the directory where the user code is extracted (may differ from
    the original ``into``).

    """

    _, ext = os.path.splitext(filename)
    user_code_dir = os.path.join(into, 'user_code')
    os.makedirs(user_code_dir, exist_ok=True)
    contents = code

    if ext in ZIPS:
        # it's a zip file
        zip_file = os.path.join(into, 'contents.zip')
        with open(zip_file, 'wb') as f:
            f.write(contents)
        zipball = zipfile.ZipFile(zip_file)
        zipball.extractall(user_code_dir)

    elif ext in TARBALLS:
        # it's a tarball
        tarball = os.path.join(into, 'contents.tgz')
        with open(tarball, 'wb') as f:
            f.write(contents)
        tar = tarfile.open(tarball)
        tar.extractall(user_code_dir)

    elif ext in EXTENSIONS.keys():
        # it's a module
        module = os.path.join(user_code_dir, filename)
        with open(module, 'w') as f:
            f.write(contents)

    else:
        raise Exception(
            'unknown extension: {0}'.format(filename), 400)

    return user_code_dir


def get_metadata(directory):
    """Return a dictionary from the metadata file in ``directory``.

    Try to read the file ``metadata.yml`` or ``metadata.json`` at the root
    of the directory.

    Concatenate files ``foo.yml`` into a field ``foo:``.

    Return an empty dict if no file is found.

    :type directory: str
    :rtype: Dict[str, Any]

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


class StoreMutexException(Exception):
    pass


class ServiceException(Exception):
    pass
        

class Service(object):

    PARAMETERS = [
        # parameter, mandatory?, [default]
        ('name', True),
        ('namespace', True),
        ('version', False, '0.1'),
        ('notify', False, ''),
        ('code_content', False, ''),
        ('code_filename', False, ''),
        ('git_repository', False, None),
        ('git_branch', False, 'master'),
        ('metadata_location', False, ''),
        ('user', False, 'anonymous')
    ]
    
    def __init__(self, guid, **kwargs):
        self.guid = guid
        self.identity = dict(self._validate_args(kwargs))

    def _validate_args(self, args):
        for parameter in self.PARAMETERS:
            try:
                yield parameter[0], args[parameter[0]]
            except KeyError:
                if parameter[1]:
                    raise
                yield parameter[0], parameter[2]

    @property
    def identifier(self):
        return tools.identifier(self.identity['namespace'],
                                self.identity['name'],
                                self.identity['version'])

    def register(self):
        stores.registration_store[self.guid] = {
            'status': 'starting'
        }
        try:
            self._do_register()
            stores.registration_store[self.guid] = {
                'status': 'success',
            }
            # notify success
        except Exception as exc:
            stores.registration_store[self.guid] = {
                'status': 'error',
                'message': str(exc),
                'traceback': format_exc()
            }
            # notify failure
            raise

    def _do_register(self):
        if self.identity['git_repository']:
            code = self._get_git_repository()
        else:
            code = self._get_code()
        self._register(code)

    def _get_git_repository(self):
        tempdir = tempfile.mkdtemp()
        with tools.chdir(tempdir):
            cmd = ['git', 'clone', '-b', self.identity['git_branch'],
                   self.identity['git_repository'], 'user_code']
            subprocess.check_call(cmd)
        return os.path.join(tempdir, 'user_code')

    def _get_code(self):
        tempdir = tempfile.mkdtemp()
        return extract(self.identity['code_filename'],
                       self.identity['code_content'],
                       tempdir)

    def _make_image(self, directory):
        pass

    def _process_icon(self, directory):
        pass
    
    def _register(self, code_location):
        """``code_location`` is a path to the extracted code."""

        metadata = get_metadata(code_location)
        # for backwards compatibility, check that any intersection
        # between identity and metadata coincides
        for field, value in self.identity.items():
            if value != metadata.get(field, value):
                raise ServiceException('metadata differs from POST '
                                       'parameters: {}'.format(field))
            
        self._make_image(code_location)
        self._process_icon(code_location)

        identity = dict(self.identity)
        del identity['code_content']
        stores.service_store[self.identifier] = {
            'identity': identity, 'metadata': metadata
        }

    
def main():
    with Channel(name='image_builder', persist=False,
                 connection_type=RabbitConnection,
                 uri=RABBIT_URI) as listen:
        while True:
            job = listen.get()
            print('will process job:', flush=True)
            print(job, flush=True)
            t = threading.Thread(target=process, args=(job,))
            t.start()


def process(job):
    """
    :type job: Dict
    """

    with job['reply_to'] as reply_to:
        try:
            args = job['value']['args']
        except KeyError:
            error("missing 'args' and/or 'namespace': {}".format(job),
                  400, reply_to)
            return

        guid = uuid.uuid4().hex
        try:
            srv = Service(guid, **args)
            if srv.identifier in stores.service_store:
                raise ServiceException('service {} already registered'
                                       .format(srv.identifier))
            stores.registration_store.mutex_acquire(srv.identifier)
            reply_to.put({
                'message': guid,
                'status': 'success'
            })
            srv.register()
        except ServiceException as exc:
            reply_to.put({
                'message': str(exc),
                'status': 'error'
            })
        except StoreMutexException:
            reply_to.put({
                'message': ('service "{}" is in process of registration'
                            .format(srv.identifier)),
                'status': 'error'
            })
        finally:
            stores.registration_store.mutex_release(iden)


if __name__ == '__main__':
    main()
