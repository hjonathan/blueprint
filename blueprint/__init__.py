from collections import defaultdict
import copy
import json
import logging
import os
import os.path
import re
import subprocess
import time
import urllib

# This must be called early - before the rest of the blueprint library loads.
logging.basicConfig(format='# [blueprint] %(message)s',
                    level=logging.INFO)

import context_managers
import git
from manager import Manager
import util


class Blueprint(dict):

    DISCLAIMER = """#
# Automatically generated by blueprint(7).  Edit at your own risk.
#
"""

    @classmethod
    def destroy(cls, name):
        """
        Destroy the named blueprint.
        """
        if not os.path.isdir(git.repo()):
            raise KeyError(name)
        try:
            git.git('branch', '-D', name)
        except:
            raise KeyError(name)

    @classmethod
    def iter(cls):
        """
        Yield the name of each blueprint.
        """
        if not os.path.isdir(git.repo()):
            return
        status, stdout = git.git('branch')
        for line in stdout.splitlines():
            yield line.strip()

    def __init__(self, name=None, commit=None, create=False):
        """
        Construct a blueprint in the new format in a backwards-compatible
        manner.
        """
        self.name = name
        self._commit = commit

        # Create a new blueprint object and populate it based on this server.
        if create:
            super(Blueprint, self).__init__()
            import backend
            for funcname in backend.__all__:
                getattr(backend, funcname)(self)
            import services
            services.services(self)

        # Create a blueprint from a Git repository.
        elif name is not None:
            git.init()
            if self._commit is None:
                self._commit = git.rev_parse('refs/heads/{0}'.format(name))
                if self._commit is None:
                    raise KeyError(name)
            tree = git.tree(self._commit)
            blob = git.blob(tree, 'blueprint.json')
            content = git.content(blob)
            super(Blueprint, self).__init__(**json.loads(content))

        # Create an empty blueprint object to be filled in later.
        else:
            super(Blueprint, self).__init__()

    def __sub__(self, other):
        """
        Subtracting one blueprint from another allows blueprints to remain
        free of superfluous packages from the base installation.  It takes
        three passes through the package tree.  The first two remove
        superfluous packages and the final one accounts for some special
        dependencies by adding them back to the tree.
        """
        b = copy.deepcopy(self)

        # Compare file contents and metadata.  Keep files that differ.
        for pathname, file in self.files.iteritems():
            if other.files.get(pathname, {}) == file:
                del b.files[pathname]

        # The first pass removes all duplicate packages that are not
        # themselves managers.  Allowing multiple versions of the same
        # packages complicates things slightly.  For each package, each
        # version that appears in the other blueprint is removed from
        # this blueprint.  After that is finished, this blueprint is
        # normalized.  If no versions remain, the package is removed.
        def package(manager, package, version):
            if package in b.packages:
                return
            if manager.name in b.packages.get(manager.name, {}):
                return
            b_packages = b.packages[manager.name]
            if package not in b_packages:
                return
            b_versions = b_packages[package]
            try:
                del b_versions[b_versions.index(version)]
            except ValueError:
                pass
            if 0 == len(b_versions):
                del b_packages[package]
            else:
                b_packages[package] = b_versions
        other.walk(package=package)

        # The second pass removes managers that manage no packages, a
        # potential side-effect of the first pass.  This step must be
        # applied repeatedly until the blueprint reaches a steady state.
        def package(manager, package, version):
            if package not in b.packages:
                return
            if 0 == len(b.packages[package]):
                del b.packages[package]
                del b.packages[self.managers[package].name][package]
        while 1:
            l = len(b.packages)
            other.walk(package=package)
            if len(b.packages) == l:
                break

        # The third pass adds back special dependencies like `ruby*-dev`.
        # It isn't apparent from the rules above that a manager like RubyGems
        # needs more than just itself to function.  In some sense, this might
        # be considered a missing dependency in the Debian archive but in
        # reality it's only _likely_ that you need `ruby*-dev` to use
        # `rubygems*`.
        def after_packages(manager):
            if manager.name not in b.packages:
                return

            deps = {r'^python(\d+(?:\.\d+)?)$': ['python{0}',
                                                 'python{0}-dev',
                                                 'python',
                                                 'python-devel'],
                    r'^ruby(\d+\.\d+(?:\.\d+)?)$': ['ruby{0}-dev'],
                    r'^rubygems(\d+\.\d+(?:\.\d+)?)$': ['ruby{0}',
                                                        'ruby{0}-dev',
                                                        'ruby',
                                                        'ruby-devel']}

            for pattern, packages in deps.iteritems():
                match = re.search(pattern, manager.name)
                if match is None:
                    continue
                for package in packages:
                    package = package.format(match.group(1))
                    for managername in ('apt', 'yum'):
                        mine = self.packages.get(managername, {}).get(package,
                                                                      None)
                        if mine is not None:
                            b.packages[managername][package] = mine
        other.walk(after_packages=after_packages)

        # Compare source tarball filenames, which indicate their content.
        # Keep source tarballs that differ.
        for dirname, filename in self.sources.iteritems():
            if other.sources.get(dirname, '') == filename:
                del b.sources[dirname]

        return b

    def get_name(self):
        return self._name
    def set_name(self, name):
        """
        Validate and set the blueprint name.
        """
        if name is not None and re.search(r'[/ \t\r\n]', name):
            raise ValueError('invalid blueprint name')
        self._name = name
    name = property(get_name, set_name)

    def get_arch(self):
        if 'arch' not in self:
            self['arch'] = None
        return self['arch']
    def set_arch(self, arch):
        self['arch'] = arch
    arch = property(get_arch, set_arch)

    @property
    def files(self):
        if 'files' not in self:
            self['files'] = defaultdict(dict)
        return self['files']

    @property
    def managers(self):
        """
        Build a hierarchy of managers for easy access when declaring
        dependencies.
        """
        if hasattr(self, '_managers'):
            return self._managers
        self._managers = {'apt': None, 'yum': None}

        def package(manager, package, version):
            if package in self.packages and manager != package:
                self._managers[package] = manager

        self.walk(package=package)
        return self._managers

    @property
    def packages(self):
        if 'packages' not in self:
            self['packages'] = defaultdict(lambda: defaultdict(set))
        return self['packages']

    @property
    def services(self):
        if 'services' not in self:
            self['services'] = defaultdict(lambda: defaultdict(dict))
        return self['services']

    @property
    def sources(self):
        if 'sources' not in self:
            self['sources'] = defaultdict(dict)
        return self['sources']

    def add_file(self, pathname, **kwargs):
        """
        Create a file resource.
        """
        self.files[pathname] = kwargs

    def add_package(self, manager, package, version):
        """
        Create a package resource.
        """
        self.packages[manager][package].add(version)

    def add_service(self, manager, service):
        """
        Create a service resource which depends on given files and packages.
        """
        self.services[manager][service]

    def add_service_file(self, manager, service, *args):
        """
        Add file dependencies to a service resource.
        """
        if 0 == len(args):
            return
        s = self.services[manager][service].setdefault('files', set())
        for dirname in args:
            s.add(dirname)

    def add_service_package(self, manager, service, package_manager, *args):
        """
        Add package dependencies to a service resource.
        """
        if 0 == len(args):
            return
        d = self.services[manager][service].setdefault('packages',
                                                       defaultdict(set))
        for package in args:
            d[package_manager].add(package)

    def add_service_source(self, manager, service, *args):
        """
        Add source tarball dependencies to a service resource.
        """
        if 0 == len(args):
            return
        s = self.services[manager][service].setdefault('sources', set())
        for dirname in args:
            s.add(dirname)

    def add_source(self, dirname, filename):
        """
        Create a source tarball resource.
        """
        self.sources[dirname] = filename

    def commit(self, message=''):
        """
        Create a new revision of this blueprint in the local Git repository.
        Include the blueprint JSON and any source archives referenced by
        the JSON.
        """
        git.init()
        refname = 'refs/heads/{0}'.format(self.name)
        parent = git.rev_parse(refname)

        # Start with an empty index every time.  Specifically, clear out
        # source tarballs from the parent commit.
        if parent is not None:
            for mode, type, sha, pathname in git.ls_tree(git.tree(parent)):
                git.git('update-index', '--remove', pathname)

        # Add `blueprint.json` to the index.
        f = open('blueprint.json', 'w')
        f.write(self.dumps())
        f.close()
        git.git('update-index', '--add', os.path.abspath('blueprint.json'))

        # Add source tarballs to the index.
        for filename in self.sources.itervalues():
            git.git('update-index', '--add', os.path.abspath(filename))

        # Add the `.blueprintignore` file to the index.  Since adding extra
        # syntax to this file, it no longer makes sense to store it as
        # `.gitignore`.
        try:
            os.link(os.path.expanduser('~/.blueprintignore'),
                    '.blueprintignore')
            git.git('update-index',
                    '--add',
                    os.path.abspath('.blueprintignore'))
        except OSError:
            pass

        # Write the index to Git's object store.
        tree = git.write_tree()

        # Write the commit and update the tip of the branch.
        self._commit = git.commit_tree(tree, message, parent)
        git.git('update-ref', refname, self._commit)

    def dumps(self):
        """
        Return a JSON serialization of this blueprint.  Make a best effort
        to prevent variance from run-to-run.  Remove superfluous empty keys.
        """
        if 'arch' in self and self['arch'] is None:
            del self['arch']
        for key in ['files', 'packages', 'sources']:
            if key in self and 0 == len(self[key]):
                del self[key]
        return util.JSONEncoder(indent=2, sort_keys=True).encode(self)

    def puppet(self):
        """
        Generate Puppet code.
        """
        import frontend.puppet
        return frontend.puppet.puppet(self)

    def chef(self):
        """
        Generate Chef code.
        """
        import frontend.chef
        return frontend.chef.chef(self)

    def sh(self, server='https://devstructure.com', secret=None):
        """
        Generate shell code.
        """
        import frontend.sh
        return frontend.sh.sh(self, server, secret)

    def blueprintignore(self):
        """
        Return the blueprint's ~/.blueprintignore file.  Prior to v3.0.4
        this file was stored as .gitignore in the repository.
        """
        tree = git.tree(self._commit)
        blob = git.blob(tree, '.blueprintignore')
        if blob is None:
            blob = git.blob(tree, '.gitignore')
        import ignore
        if blob is None:
            return ignore.Rules('')
        content = git.content(blob)
        if content is None:
            return ignore.Rules('')
        return ignore.Rules(content)

    def walk(self, **kwargs):
        """
        Walk an entire blueprint in the appropriate order, executing
        callbacks along the way.  See blueprint(5) for details on the
        algorithm.  The callbacks are passed directly from this method
        to the resource type-specific methods and are documented there.
        """
        self.walk_sources(**kwargs)
        self.walk_files(**kwargs)
        self.walk_packages(**kwargs)
        self.walk_services(**kwargs)

    def walk_sources(self, **kwargs):
        """
        Walk a blueprint's source tarballs and execute callbacks.

        * `before_sources():`
          Executed before source tarballs are enumerated.
        * `source(dirname, filename, gen_content):`
          Executed when a source tarball is enumerated.  `gen_content`
          is a callable that will return the file's contents.
        * `after_sources():`
          Executed after source tarballs are enumerated.
        """

        kwargs.get('before_sources', lambda *args: None)()

        callable = kwargs.get('source', lambda *args: None)
        tree = git.tree(self._commit)
        for dirname, filename in sorted(self.sources.iteritems()):
            blob = git.blob(tree, filename)
            callable(dirname, filename, lambda: git.content(blob))

        kwargs.get('before_sources', lambda *args: None)()

    def walk_files(self, **kwargs):
        """
        Walk a blueprint's files and execute callbacks.

        * `before_files():`
          Executed before files are enumerated.
        * `file(pathname, f):`
          Executed when a file is enumerated.
        * `after_files():`
          Executed after files are enumerated.
        """

        kwargs.get('before_files', lambda *args: None)()

        callable = kwargs.get('file', lambda *args: None)
        for pathname, f in sorted(self.files.iteritems()):
            callable(pathname, f)

        kwargs.get('after_files', lambda *args: None)()

    def walk_packages(self, managername=None, **kwargs):
        """
        Walk a package tree and execute callbacks along the way.  This is
        a bit like iteration but can't match the iterator protocol due to
        the varying argument lists given to each type of callback.  The
        available callbacks are:

        * `before_packages(manager):`
          Executed before a package manager's dependencies are enumerated.
        * `package(manager, package, version):`
          Executed when a package version is enumerated.
        * `after_packages(manager):`
          Executed after a package manager's dependencies are enumerated.
        """

        # Walking begins with the system package managers, `apt` and `yum`.
        if managername is None:
            self.walk_packages('apt', **kwargs)
            self.walk_packages('yum', **kwargs)
            return

        # Get the full manager from its name.  Watch out for KeyError (by
        # using dict.get instead of dict.__get__), which means the manager
        # isn't part of this blueprint.
        manager = Manager(managername, self.packages.get(managername, {}))

        # Give the manager a chance to setup for its dependencies.
        kwargs.get('before_packages', lambda *args: None)(manager)

        # Each package gets its chance to take action.  Note which packages
        # are themselves managers so they may be visited recursively later.
        managers = []
        callable = kwargs.get('package', lambda *args: None)
        for package, versions in sorted(manager.iteritems()):
            for version in versions:
                callable(manager, package, version)
            if managername != package and package in self.packages:
                managers.append(package)

        # Give the manager a change to cleanup after itself.
        kwargs.get('after_packages', lambda *args: None)(manager)

        # Now recurse into each manager that was just installed.  Recursing
        # here is safer because there may be secondary dependencies that are
        # not expressed in the hierarchy (for example the `mysql2` gem
        # depends on `libmysqlclient-dev` in addition to its manager).
        for managername in managers:
            self.walk_packages(managername, **kwargs)

    def walk_services(self, managername=None, **kwargs):
        """
        Walk a blueprint's services and execute callbacks.

        * `before_services(managername):`
          Executed before a service manager's dependencies are enumerated.
        * `service(managername, service):`
          Executed when a service is enumerated.
        * `after_services(managername):`
          Executed after a service manager's dependencies are enumerated.
        """

        # Unless otherwise specified, walk all service managers.
        if managername is None:
            for managername in sorted(self.services.iterkeys()):
                self.walk_services(managername, **kwargs)
            return

        kwargs.get('before_services', lambda *args: None)(managername)

        callable = kwargs.get('service', lambda *args: None)
        for service, deps in sorted(self.services[managername].iteritems()):
            callable(managername, service)
            self.walk_service_files(managername, service, **kwargs)
            self.walk_service_packages(managername, service, **kwargs)
            self.walk_service_sources(managername, service, **kwargs)

        kwargs.get('after_services', lambda *args: None)(managername)

    def walk_service_files(self, managername, servicename, **kwargs):
        """
        Walk a service's file dependencies and execute callbacks.

        * `service_file(managername, servicename, pathname):`
          Executed when a file service dependency is enumerated.
        """
        deps = self.services[managername][servicename]
        if 'files' not in deps:
            return
        callable = kwargs.get('service_file', lambda *args: None)
        for pathname in deps['files']:
            callable(managername, servicename, pathname)

    def walk_service_packages(self, managername, servicename, **kwargs):
        """
        Walk a service's package dependencies and execute callbacks.

        * `service_package(managername,
                           servicename,
                           package_managername,
                           package):`
          Executed when a file service dependency is enumerated.
        """
        deps = self.services[managername][servicename]
        if 'packages' not in deps:
            return
        callable = kwargs.get('service_package', lambda *args: None)
        for package_managername, packages in deps['packages'].iteritems():
            for package in packages:
                callable(managername,
                         servicename,
                         package_managername,
                         package)

    def walk_service_sources(self, managername, servicename, **kwargs):
        """
        Walk a service's source tarball dependencies and execute callbacks.

        * `service_source(managername, servicename, dirname):`
          Executed when a source tarball service dependency is enumerated.
        """
        deps = self.services[managername][servicename]
        if 'sources' not in deps:
            return
        callable = kwargs.get('service_source', lambda *args: None)
        for dirname in deps['sources']:
            callable(managername, servicename, dirname)
