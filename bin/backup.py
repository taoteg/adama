#!/usr/bin/env python
import os
import datetime
import tarfile
import subprocess
import shutil

import adama.command.tools as t


def directory():
    """Create directory for backup.

    :rtype: str
    """
    dirname = 'backup-{}'.format(datetime.datetime.now().isoformat())
    d = os.path.join(os.path.expanduser('~'), dirname)
    try:
        os.makedirs(d)
    except OSError:
        pass
    return d


def pack(path):
    """Create a tarball from a directory.

    :type path: str
    :rtype: str
    """
    d = os.path.basename(path)
    target = os.path.join(os.path.expanduser('~'), '{}.tar.bz2'.format(d))
    with tarfile.open(target, 'w:bz2') as tar:
        tar.add(path)
    return target


def archive(path, directory):
    """Send a file to some persistent storage.

    :type path: str
    :rtype: None
    """
    subprocess.check_call('scp {} araport@corral.tacc.utexas.edu:/corral-tacc/tacc/bio/araport/users/araport/adama'.format(path).split(),
                          stdout=open('/dev/null', 'w'), stderr=subprocess.STDOUT) 
    os.unlink(path)
    shutil.rmtree(directory)


def main():
    d = directory()
    t.backup_adapters(d)
    tarball = pack(d)
    archive(tarball, d)


if __name__ == '__main__':
    main()
