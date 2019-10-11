import errno
import json
import os
from buildbot.interfaces import IRenderable
from textwrap import dedent, wrap
from twisted.python import log
from zope.interface import implements


def notify(tmpl, *args, **kw):
    """Print a big obvious banner. Reformats msg to allow indented triple-strings."""
    msg = tmpl.format(*args, **kw)
    print ' ***'
    print ' *** ' + ('\n *** '.join(wrap(dedent(msg))).strip())
    print ' ***'

def _read_path(path):
    with file(path, 'r') as f:
        return f.read()

def read_optional_path(path):
    try:
        return _read_path(path)
    except IOError as e:
        if e.errno == errno.ENOENT:
            return None
        else:
            raise

def read_required_path(path, description):
    result = read_optional_path(path)
    if result is not None:
        return result

    print ' ***'
    print ' *** Could not read: {!r}'.format(path)
    notify(description)
    e = IOError('{}: {!r}'.format(os.strerror(errno.ENOENT), path))
    e.errno = errno.ENOENT
    raise e

def read_or_generate_secret(path, githubsecret):
    result = read_optional_path(path)
    if result is not None:
        return result

    # File doesn't exist:
    SECRET_BYTES = 10
    value = os.urandom(SECRET_BYTES).encode('base64').strip().rstrip('=')
    with file(path, 'w') as f:
        f.write(value)

    if githubsecret:
        notify(
            '''
            NOTE: The secret {!r} was just auto-generated. You must
            configure the relevant github repositories and add it as
            the webhook secret.
            ''',
            path)

    return value

def load_webcreds(path):
    filedesc = 'A json file with an array, each element is [username, password]'
    jsoncreds = json.loads(read_required_path(path, filedesc))
    creds = []
    try:
        for entry in jsoncreds:
            [username, password] = entry
            username = username.encode('utf8')
            password = password.encode('utf8')
            entry = (username, password)
            creds.append(entry)
    except:
        notify(
            'There was a JSON format error loading {!r}, which should be: {}',
            path,
            filedesc,
        )
        raise
    else:
        return creds


class GoodRepo:
    implements(IRenderable)

    def __init__(self, repos, default_repourl):
        self.repos = repos
        self.default_repourl = default_repourl

    def getRenderingFor(self, props):
        # the 'repository' property might be missing or an empty string
        repourl = props.getProperty("repository", None) or self.default_repourl
        if repourl not in self.repos:
            log.msg("refusing to build from unsafe repo '%s'"
                    " (will only accept: %s)" % (repourl, ",".join(self.repos)))
            raise ValueError("refusing to build from unsafe repo, see logs")
        # rewrite SSH URLs because workers won't have the correct keys
        if repourl.startswith('git@'):
            repourl = 'https://' + '/'.join(repourl[4:].split(':', 1))
        return repourl
