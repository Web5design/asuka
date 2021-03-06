""":mod:`asuka.app` --- Application configuration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

"""
import collections
import hashlib
import io
import os.path
import re

from boto.ec2.connection import EC2Connection
from boto.exception import EC2ResponseError
from boto.route53.connection import Route53Connection
from github3.api import login
from github3.github import GitHub
from github3.repos import Repository
from paramiko.pkey import PKey
from paramiko.rsakey import RSAKey
from werkzeug.utils import cached_property

from .instance import REGION_AMI_MAP, AMI_LOGIN_MAP, Instance

__all__ = 'App', 'DeployedBranchDict', 'InstanceSet'


class App(object):
    """Application configuration.  It takes keyword-only parameters
    that are the same name of settable properties and attributes.

    """

    #: (:class:`basestring`) The format string of AWS key pair name.
    KEY_PAIR_NAME_FORMAT = 'Asuka-{app.name}'

    #: (:class:`basestring`) The name of the app.
    name = None

    #: (:class:`basestring`) The URL without any path of the web frontend
    #: e.g. ``'http://example.com'``.  Note that there's no trailing
    #: slash.
    url_base = None

    #: (:class:`boto.ec2.connection.EC2Connection`) The EC2 connection
    #: to invoke APIs.
    ec2_connection = None

    #: (:class:`basestring`) The name of the Asuka configuration directory
    #: in the repository.  The default uses :file:`asuka/`.
    config_dir = 'asuka/'

    #: (:class:`basestring`) The path of directory to store data made by
    #: Asuka builds.
    data_dir = None

    #: (:class:`str`) The Route 53 hosted zone ID.
    route53_hosted_zone_id = None

    #: (:class:`collections.Mapping`) The map of service names and their
    #: mapped domain name format strings
    #: e.g. ``{'web': '{branch:label}.test.example.com.'}``.
    route53_records = {}

    #: (:class:`str`) Same to :attr:`route53_hosted_zone_id` except it's
    #: for promoted live deployment.
    route53_live_hosted_zone_id = None

    #: (:class:`collections.Mapping`) Same to :attr:`route53_records`
    #: except it's for promoted live deployment.
    route53_live_records = {}

    #: (:class:`collections.Set`) The set of security groups EC2 instances
    #: to use.
    ec2_security_groups = frozenset()

    #: (:class:`str`) The client ID of the GitHub app.
    github_client_id = None

    #: (:class:`str`) The client secret key of the GitHub app.
    github_client_secret = None

    #: (:class:`collections.Sequence`) The list of hook urls requested
    #: when the build has started.  These urls are requested in the order.
    start_hook_urls = []

    #: (:class:`collections.Sequence`) The list of hook urls requested
    #: when the build has finished.  These urls are requested in the order.
    finish_hook_urls = []

    #: (:class:`collections.Mapping`) The config dict for
    #: :class:`~asuka.web.WebApp`.
    web_config = {}

    def __init__(self, **values):
        # Pop and set "name" and "ec2_connection" first because other
        # properties require it.
        try:
            self.name = values.pop('name')
        except KeyError:
            raise TypeError('missing name parameter')
        else:
            if not isinstance(self.name, basestring):
                raise TypeError('name must be a string, not ' +
                                repr(self.name))
        try:
            self.data_dir = values.pop('data_dir')
        except KeyError:
            raise TypeError('missing data_dir parameter')
        else:
            if not isinstance(self.data_dir, basestring):
                raise TypeError('data_dir must be a string, not ' +
                                repr(self.data_dir))
            elif not os.path.isdir(self.data_dir):
                raise IOError(self.data_dir + ' is not directory that exists')
        try:
            self.ec2_connection = values.pop('ec2_connection')
        except KeyError:
            raise TypeError('missing ec2_connection parameter')
        else:
            if not isinstance(self.ec2_connection, EC2Connection):
                raise TypeError('ec2_connection must be an instance of '
                                'boto.ec2.connection.EC2Connection, not ' +
                                repr(self.ec2_connection))
        # For all keys: self.$key = values['$key']
        for attr, value in values.iteritems():
            setattr(self, attr, value)
        if self.private_key is None:
            self.key_pair
        self.ec2_security_groups = frozenset(self.ec2_security_groups)
        self.github_client_id = str(self.github_client_id)
        self.github_client_secret = str(self.github_client_secret)
        self.start_hook_urls = list(self.start_hook_urls)
        self.finish_hook_urls = list(self.finish_hook_urls)
        self.web_config = dict(self.web_config)

    @property
    def private_key(self):
        """(:class:`paramiko.pkey.PKey`) The pair of public and private key."""
        return getattr(self, '_private_key', None)

    @private_key.setter
    def private_key(self, pkey):
        if not isinstance(pkey, PKey):
            raise TypeError('private_key must be an instance of paramiko.'
                            'pkey.PKey, not ' + repr(pkey))
        self._private_key = pkey
        self._create_github_deploy_key()
        keys = self.ec2_connection.get_all_key_pairs([self.key_name])
        if keys:
            key_pair = keys[0]
        else:
            key_pair = self.ec2_connection.import_key_pair(
                self.key_name,
                self.public_key_string
            )
        self._key_pair = key_pair

    @property
    def public_key_string(self):
        """(:class:`basestring`) The public key string."""
        elements = (self.private_key.get_name(),
                    self.private_key.get_base64(), self.key_name)
        return ' '.join(elements)

    @property
    def key_pair(self):
        """(:class:`boto.ec2.keypair.KeyPair`) The EC2 key pair matched to
        :attr:`private_key`.

        """
        try:
            return self._key_pair
        except AttributeError:
            self._key_pair = self.ec2_connection.create_key_pair(
                self.key_name
            )
            private_key = str(self._key_pair.material)
            self._private_key = RSAKey.from_private_key(io.BytesIO(private_key))
            self._create_github_deploy_key()

    @property
    def key_name(self):
        """(:class:`basestring`) The human-readable title of the key pair."""
        return self.KEY_PAIR_NAME_FORMAT.format(app=self)

    @cached_property
    def github(self):
        """(:class:`github3.GitHub <github3.github.GitHub>`) The GitHub
        connection.

        """
        gh = GitHub()
        gh._session = self.repository._session
        return gh

    @property
    def repository(self):
        """(:class:`github3.repos.Repository`) The repository of the app."""
        repo = getattr(self, '_repository', None)
        hook_name = 'web'
        hook_events = frozenset(['push', 'pull_request'])
        hook_config = {
            'url': self.url_base + '/hook/',
            'content_type': 'json',
            'secret': self.github_client_secret
        }
        for hook in repo.iter_hooks():
            if (hook.name == hook_name and
                frozenset(hook.events) == hook_events and
                hook.config == hook_config):
                if not hook.active:
                    hook.edit(
                        name=hook_name,
                        events=list(hook_events),
                        config=hook_config,
                        active=True
                    )
                return repo
        repo.create_hook(
            name=hook_name,
            events=list(hook_events),
            config=hook_config,
            active=True
        )
        return repo

    @repository.setter
    def repository(self, repos):
        if not isinstance(repos, Repository):
            raise TypeError('repository must be an instance of github3.repos.'
                            'Repository, not ' + repr(repos))
        self._repository = repos
        if hasattr(self, '_private_key'):
            self._create_github_deploy_key()

    def _create_github_deploy_key(self):
        try:
            repos = self._repository
        except AttributeError:
            pass
        else:
            actual_key = self.private_key.get_base64()
            for key in repos.iter_keys():
                if key.title != self.key_name:
                    continue
                elif key.key.split()[1] != actual_key:
                    continue
                break
            else:
                repos.create_key(self.key_name, self.public_key_string)

    def get_clone_url(self, repository=None):
        """Makes an authenticated clone url of the GitHub repository.

        :param repository: the repository to clone.
                           default is :attr:`repository`
        :type repository: :class:`github3.repos.Repository`
        :returns: the clone url
        :rtype: :class:`basestring`

        """
        if repository is None:
            repository = self.repository
        url = repository.clone_url
        session = self.github._session
        if session.auth:
            token = ':'.join(session.auth)
        else:
            auth = session.headers['Authorization']
            _, token = auth.split(' ', 1)
        return re.sub(
            r'^https?://',
            lambda m: m.group(0) + token + '@',
            url
        )

    @cached_property
    def route53_connection(self):
        """(:class:`boto.route53.connection.Route53Connection`)
        The Route 53 connection.

        """
        ec2 = self.ec2_connection
        return Route53Connection(
            aws_access_key_id=ec2.provider.access_key,
            aws_secret_access_key=ec2.provider.secret_key,
            port=ec2.port,
            proxy=ec2.proxy, proxy_port=ec2.proxy_port,
            debug=ec2.debug,
            security_token=ec2.provider.security_token,
            validate_certs=ec2.https_validate_certificates
        )

    def create_instance(self, instance_type='t1.micro'):
        """Creates a new instance to deploy the application.

        :param instance_type: the ec2 instance type.
                              default is ``'t1.micro'``
        :type instance_type: :class:`basestring`
        :returns: the created new instance
        :rtype: :class:`asuka.instance.Instance`

        """
        region = self.ec2_connection.region.name
        ami = REGION_AMI_MAP[region]
        login = AMI_LOGIN_MAP[ami]
        reserve = self.ec2_connection.run_instances(
            image_id=ami,
            instance_type=instance_type,
            key_name=self.key_name,
            security_groups=list(self.ec2_security_groups)
        )
        instance = reserve.instances[0]
        return Instance(self, instance, login)

    @property
    def instances(self):
        """(:class:`InstanceSet`) :class:`~asuka.instance.Instance`\ s
        belong to the app.

        """
        return InstanceSet(self)

    @cached_property
    def consistent_secret(self):
        """(:class:`str`) The secret key consistent for every deployment
        of the app.

        """
        key = ','.join((
            self.name,
            self.ec2_connection.provider.secret_key,
            self.github_client_secret
        ))
        return hashlib.sha256(key).hexdigest()

    @property
    def deployed_branches(self):
        """(:class:`collections.Mapping`) The mapping of currently
        deployed banches (and commits).

        """
        return DeployedBranchDict(self)

    @property
    def deployments(self):
        """(:class:`collections.Set`) The set of current deployments."""
        from .deploy import Deployment
        return Deployment.from_app(self)

    def __eq__(self, operand):
        return isinstance(operand, type(self)) and operand.name == self.name

    def __ne__(self, operand):
        return not (self == operand)

    def __hash__(self):
        return hash(self.name)

    def __getstate__(self):
        config = dict((k, v)
                      for k, v in self.__dict__.items()
                      if not k.startswith('_'))
        repo = self.repository
        _, token = repo._session.headers['Authorization'].split()
        config['repository'] = token, repo.owner.login, repo.name
        config['private_key'] = self.private_key
        return config

    def __setstate__(self, state):
        token, owner, repo = state.pop('repository')
        github = login(token=token)
        state['repository'] = github.repository(owner, repo)
        self.__init__(**state)

    def __repr__(self):
        c = type(self)
        return '<{0}.{1} {2!r}>'.format(c.__module__, c.__name__, self.name)


class InstanceSet(collections.Set):
    """The set of instances.

    :param app: the app object
    :type app: :class:`App`
    :param tags: dictionary of tags to filter.  default is empty
    :type tags: :class:`collections.Mapping`

    """

    def __init__(self, app, tags={}):
        self.app = app
        self.tags = {'App': app.name}
        self.tags.update(tags)

    def tagged(self, tag, value):
        """Filters instances by ``tag`` and its ``value``.  For example::

            instances.tagged('Branch', 'master').tagged('Status', 'done')

        :param tag: tag name to filter
        :type tag: :class:`basestring`
        :param value: tag value that has to be matched
        :type value: :class:`basestring`
        :returns: a filtered new set object
        :rtype: :class:`InstanceSet`

        """
        tags = {tag: value}
        tags.update(self.tags)
        return type(self)(self.app, tags)

    def __iter__(self):
        app = self.app
        ec2_conn = app.ec2_connection
        filters = dict(('tag:' + tag, value)
                       for tag, value in self.tags.iteritems())
        filters['instance-state-name'] = 'running'
        try:
            reservations = ec2_conn.get_all_instances(filters=filters)
            for reserve in reservations:
                for instance in reserve.instances:
                    yield Instance(self.app, instance)
        except EC2ResponseError:
            pass

    def __len__(self):
        return sum(1 for _ in self)

    def __contains__(self, instance):
        if isinstance(instance, Instance):
            for i in self:
                if i.instance.id == instance.instance.id:
                    return True
        return False


class DeployedBranchDict(collections.Mapping):
    """The mapping of deployed branches and commits."""

    def __init__(self, app):
        self.app = app
        self.refresh()

    def refresh(self):
        self.branches = None

    def itertags(self):
        app = self.app
        self.branches = {}
        for instance in app.instances.tagged('Status', 'done'):
            tags = dict(instance.tags)
            if tags.get('Live', '') == 'live':
                continue
            try:
                branch = tags['Branch']
                commit = tags['Commit']
            except KeyError:
                continue
            if branch not in self.branches:
                self.branches[branch] = commit
                yield branch, commit

    def __len__(self):
        if self.branches is None:
            l = 0
            for _ in self.itertags():
                l += 1
            return l
        return len(self.branches)

    def __iter__(self):
        from .branch import find_by_label
        if self.branches is None:
            labels = (label for label, commit in self.itertags())
        else:
            labels = self.branches
        for label in labels:
            yield find_by_label(self.app, label)

    def __getitem__(self, branch):
        from .branch import Branch
        if not isinstance(branch, Branch):
            raise TypeError('expected asuka.branch.Branch, not ' +
                            repr(branch))
        label = branch.label
        if self.branches is None:
            for branch_label, commit in self.itertags():
                if branch_label == label:
                    break
            else:
                raise KeyError(branch)
        else:
            try:
                commit = self.branches[label]
            except KeyError:
                raise KeyError(branch)
        from .commit import Commit
        return Commit(self.app, commit)
