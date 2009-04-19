# package.py - apt package abstraction
#
#  Copyright (c) 2005-2009 Canonical
#
#  Author: Michael Vogt <michael.vogt@ubuntu.com>
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License as
#  published by the Free Software Foundation; either version 2 of the
#  License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307
#  USA
"""Functionality related to packages."""
import gettext
import httplib
import os
import sys
import re
import socket
import subprocess
import urllib2
import warnings
try:
    from collections import Mapping
except ImportError:
    # (for Python < 2.6) pylint: disable-msg=C0103
    Mapping = object

import apt_pkg
import apt.progress
from apt.deprecation import (function_deprecated_by, AttributeDeprecatedBy,
                             deprecated_args)

__all__ = ('BaseDependency', 'Dependency', 'Origin', 'Package', 'Record',
           'Version')


def _(string):
    """Return the translation of the string."""
    return gettext.dgettext("python-apt", string)


def _file_is_same(path, size, md5):
    """Return ``True`` if the file is the same."""
    if (os.path.exists(path) and os.path.getsize(path) == size and
        apt_pkg.md5sum(open(path)) == md5):
        return True


class FetchError(Exception):
    """Raised when a file could not be fetched."""


class BaseDependency(object):
    """A single dependency.

    Attributes defined here:
        name       - The name of the dependency
        relation   - The relation (>>,>=,==,<<,<=,)
        version    - The version depended on
        pre_depend - Boolean value whether this is a pre-dependency.
    """

    def __init__(self, name, rel, ver, pre):
        self.name = name
        self.relation = rel
        self.version = ver
        self.pre_depend = pre

    def __repr__(self):
        return ('<BaseDependency: name:%r relation:%r version:%r preDepend:%r>'
                % (self.name, self.relation, self.version, self.pre_depend))

    if apt_pkg._COMPAT_0_7:
        preDepend = AttributeDeprecatedBy('pre_depend')


class Dependency(object):
    """Represent an Or-group of dependencies.

    Attributes defined here:
        or_dependencies - The possible choices
    """

    def __init__(self, alternatives):
        self.or_dependencies = alternatives

    def __repr__(self):
        return repr(self.or_dependencies)


class DeprecatedProperty(property):
    """A property which gives DeprecationWarning on access.

    This is only used for providing the properties in Package, which have been
    replaced by the ones in Version.
    """

    def __init__(self, fget=None, fset=None, fdel=None, doc=None):
        property.__init__(self, fget, fset, fdel, doc)
        self.__doc__ = (doc or fget.__doc__ or '')

    def __get__(self, obj, type=None):
        if obj is not None:
            warnings.warn("Accessed deprecated property %s.%s, please see the "
                          "Version class for alternatives." %
                           ((obj.__class__.__name__ or type.__name__),
                           self.fget.__name__), DeprecationWarning, 2)
        return property.__get__(self, obj, type)


class Origin(object):
    """The origin of a version.

    Attributes defined here:
        archive   - The archive (eg. unstable)
        component - The component (eg. main)
        label     - The Label, as set in the Release file
        origin    - The Origin, as set in the Release file
        site      - The hostname of the site.
        trusted   - Boolean value whether this is trustworthy.
    """

    def __init__(self, pkg, packagefile):
        self.archive = packagefile.Archive
        self.component = packagefile.Component
        self.label = packagefile.Label
        self.origin = packagefile.Origin
        self.site = packagefile.Site
        self.not_automatic = packagefile.NotAutomatic
        # check the trust
        indexfile = pkg._pcache._list.FindIndex(packagefile)
        if indexfile and indexfile.IsTrusted:
            self.trusted = True
        else:
            self.trusted = False

    def __repr__(self):
        return ("<Origin component:%r archive:%r origin:%r label:%r "
                "site:%r isTrusted:%r>") % (self.component, self.archive,
                                            self.origin, self.label,
                                            self.site, self.trusted)


class Record(Mapping):
    """Represent a pkgRecord.

    It can be accessed like a dictionary and can also give the original package
    record if accessed as a string.
    """

    def __init__(self, record_str):
        self._rec = apt_pkg.TagSection(record_str)

    def __hash__(self):
        return hash(self._rec)

    def __str__(self):
        return str(self._rec)

    def __getitem__(self, key):
        return self._rec[key]

    def __contains__(self, key):
        return key in self._rec

    def __iter__(self):
        return iter(self._rec.keys())

    def iteritems(self):
        """An iterator over the (key, value) items of the record."""
        for key in self._rec.keys():
            yield key, self._rec[key]

    def get(self, key, default=None):
        """Return record[key] if key in record, else *default*.

        The parameter *default* must be either a string or None.
        """
        return self._rec.get(key, default)

    def has_key(self, key):
        """deprecated form of ``key in x``."""
        return key in self._rec

    def __len__(self):
        return len(self._rec)


class Version(object):
    """Representation of a package version.

    .. versionadded:: 0.7.9
    """

    def __init__(self, package, cand):
        self.package = package
        self._cand = cand

    def __eq__(self, other):
        return self._cand.ID == other._cand.ID

    def __gt__(self, other):
        return apt_pkg.VersionCompare(self.version, other.version) > 0

    def __lt__(self, other):
        return apt_pkg.VersionCompare(self.version, other.version) < 0

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return self._cand.Hash

    def __repr__(self):
        return '<Version: package:%r version:%r>' % (self.package.name,
                                                     self.version)

    @property
    def _records(self):
        """Internal helper that moves the Records to the right position."""
        if self.package._pcache._records.Lookup(self._cand.FileList[0]):
            return self.package._pcache._records

    @property
    def _translated_records(self):
        """Internal helper to get the translated description."""
        desc_iter = self._cand.TranslatedDescription
        self.package._pcache._records.Lookup(desc_iter.FileList.pop(0))
        return self.package._pcache._records

    @property
    def installed_size(self):
        """Return the size of the package when installed."""
        return self._cand.InstalledSize

    @property
    def homepage(self):
        """Return the homepage for the package."""
        return self._records.Homepage

    @property
    def size(self):
        """Return the size of the package."""
        return self._cand.Size

    @property
    def architecture(self):
        """Return the architecture of the package version."""
        return self._cand.Arch

    @property
    def downloadable(self):
        """Return whether the version of the package is downloadable."""
        return bool(self._cand.Downloadable)

    @property
    def version(self):
        """Return the version as a string."""
        return self._cand.VerStr

    @property
    def summary(self):
        """Return the short description (one line summary)."""
        return self._translated_records.ShortDesc

    @property
    def raw_description(self):
        """return the long description (raw)."""
        return self._records.LongDesc

    @property
    def section(self):
        """Return the section of the package."""
        return self._cand.Section

    @property
    def description(self):
        """Return the formatted long description.

        Return the formated long description according to the Debian policy
        (Chapter 5.6.13).
        See http://www.debian.org/doc/debian-policy/ch-controlfields.html
        for more information.
        """
        desc = ''
        dsc = self._translated_records.LongDesc
        try:
            if not isinstance(dsc, unicode):
                # Only convert where needed (i.e. Python 2.X)
                dsc = unicode(dsc, "utf-8")
        except UnicodeDecodeError, err:
            return _("Invalid unicode in description for '%s' (%s). "
                  "Please report.") % (self.package.name, err)

        lines = iter(dsc.split("\n"))
        # Skip the first line, since its a duplication of the summary
        lines.next()
        for raw_line in lines:
            if raw_line.strip() == ".":
                # The line is just line break
                if not desc.endswith("\n"):
                    desc += "\n\n"
                continue
            if raw_line.startswith("  "):
                # The line should be displayed verbatim without word wrapping
                if not desc.endswith("\n"):
                    line = "\n%s\n" % raw_line[2:]
                else:
                    line = "%s\n" % raw_line[2:]
            elif raw_line.startswith(" "):
                # The line is part of a paragraph.
                if desc.endswith("\n") or desc == "":
                    # Skip the leading white space
                    line = raw_line[1:]
                else:
                    line = raw_line
            else:
                line = raw_line
            # Add current line to the description
            desc += line
        return desc

    @property
    def source_name(self):
        """Return the name of the source package."""
        try:
            return self._records.SourcePkg or self.package.name
        except IndexError:
            return self.package.name

    @property
    def priority(self):
        """Return the priority of the package, as string."""
        return self._cand.PriorityStr

    @property
    def record(self):
        """Return a Record() object for this version."""
        return Record(self._records.Record)

    @property
    def dependencies(self):
        """Return the dependencies of the package version."""
        depends_list = []
        depends = self._cand.DependsList
        for t in ["PreDepends", "Depends"]:
            try:
                for dep_ver_list in depends[t]:
                    base_deps = []
                    for dep_or in dep_ver_list:
                        base_deps.append(BaseDependency(dep_or.TargetPkg.Name,
                                        dep_or.CompType, dep_or.TargetVer,
                                        (t == "PreDepends")))
                    depends_list.append(Dependency(base_deps))
            except KeyError:
                pass
        return depends_list

    @property
    def origins(self):
        """Return a list of origins for the package version."""
        origins = []
        for (packagefile, index) in self._cand.FileList:
            origins.append(Origin(self.package, packagefile))
        return origins

    @property
    def filename(self):
        """Return the path to the file inside the archive.

        .. versionadded:: 0.7.10
        """
        return self._records.FileName

    @property
    def md5(self):
        """Return the md5sum of the binary.

        .. versionadded:: 0.7.10
        """
        return self._records.MD5Hash

    @property
    def sha1(self):
        """Return the sha1sum of the binary.

        .. versionadded:: 0.7.10
        """
        return self._records.SHA1Hash

    @property
    def sha256(self):
        """Return the sha256sum of the binary.

        .. versionadded:: 0.7.10
        """
        return self._records.SHA256Hash

    def _uris(self):
        """Return an iterator over all available urls.

        .. versionadded:: 0.7.10
        """
        for (packagefile, index) in self._cand.FileList:
            indexfile = self.package._pcache._list.FindIndex(packagefile)
            if indexfile:
                yield indexfile.ArchiveURI(self._records.FileName)

    @property
    def uris(self):
        """Return a list of all available uris for the binary.

        .. versionadded:: 0.7.10
        """
        return list(self._uris())

    @property
    def uri(self):
        """Return a single URI for the binary.

        .. versionadded:: 0.7.10
        """
        return self._uris().next()

    def fetch_binary(self, destdir='', progress=None):
        """Fetch the binary version of the package.

        The parameter *destdir* specifies the directory where the package will
        be fetched to.

        The parameter *progress* may refer to an apt.progress.FetchProgress()
        object. If not specified or None, apt.progress.TextFetchProgress() is
        used.

        .. versionadded:: 0.7.10
        """
        base = os.path.basename(self._records.FileName)
        destfile = os.path.join(destdir, base)
        if _file_is_same(destfile, self.size, self._records.MD5Hash):
            print 'Ignoring already existing file:', destfile
            return
        acq = apt_pkg.Acquire(progress or apt.progress.TextFetchProgress())
        apt_pkg.AcquireFile(acq, self.uri, self._records.MD5Hash, self.size,
                              base, destfile=destfile)
        acq.Run()
        for item in acq.Items:
            if item.Status != item.StatDone:
                raise FetchError("The item %r could not be fetched: %s" %
                                    (item.DestFile, item.ErrorText))
        return os.path.abspath(destfile)

    def fetch_source(self, destdir="", progress=None, unpack=True):
        """Get the source code of a package.

        The parameter *destdir* specifies the directory where the source will
        be fetched to.

        The parameter *progress* may refer to an apt.progress.FetchProgress()
        object. If not specified or None, apt.progress.TextFetchProgress() is
        used.

        The parameter *unpack* describes whether the source should be unpacked
        (``True``) or not (``False``). By default, it is unpacked.

        If *unpack* is ``True``, the path to the extracted directory is
        returned. Otherwise, the path to the .dsc file is returned.
        """
        src = apt_pkg.SourceRecords()
        acq = apt_pkg.Acquire(progress or apt.progress.TextFetchProgress())

        dsc = None
        src.Lookup(self.package.name)
        try:
            while self.version != src.Version:
                src.Lookup(self.package.name)
        except AttributeError:
            raise ValueError("No source for %r" % self)
        for md5, size, path, type in src.Files:
            base = os.path.basename(path)
            destfile = os.path.join(destdir, base)
            if type == 'dsc':
                dsc = destfile
            if os.path.exists(base) and os.path.getsize(base) == size:
                fobj = open(base)
                try:
                    if apt_pkg.md5sum(fobj) == md5:
                        print 'Ignoring already existing file:', destfile
                        continue
                finally:
                    fobj.close()
            apt_pkg.AcquireFile(acq, src.Index.ArchiveURI(path), md5, size,
                                  base, destfile=destfile)
        acq.Run()

        for item in acq.Items:
            if item.Status != item.StatDone:
                raise FetchError("The item %r could not be fetched: %s" %
                                    (item.DestFile, item.ErrorText))

        if unpack:
            outdir = src.Package + '-' + apt_pkg.UpstreamVersion(src.Version)
            outdir = os.path.join(destdir, outdir)
            subprocess.check_call(["dpkg-source", "-x", dsc, outdir])
            return os.path.abspath(outdir)
        else:
            return os.path.abspath(dsc)


class Package(object):
    """Representation of a package in a cache.

    This class provides methods and properties for working with a package. It
    lets you mark the package for installation, check if it is installed, and
    much more.
    """

    def __init__(self, pcache, pkgiter):
        """ Init the Package object """
        self._pkg = pkgiter
        self._pcache = pcache           # python cache in cache.py
        self._changelog = ""            # Cached changelog

    def __repr__(self):
        return '<Package: name:%r id:%r>' % (self._pkg.Name, self._pkg.ID)

    def candidate(self):
        """Return the candidate version of the package.

        This property is writeable to allow you to set the candidate version
        of the package. Just assign a Version() object, and it will be set as
        the candidate version.

        .. versionadded:: 0.7.9
        """
        cand = self._pcache._depcache.GetCandidateVer(self._pkg)
        if cand is not None:
            return Version(self, cand)

    def __set_candidate(self, version):
        """Set the candidate version of the package."""
        self._pcache.cache_pre_change()
        self._pcache._depcache.SetCandidateVer(self._pkg, version._cand)
        self._pcache.cache_post_change()

    candidate = property(candidate, __set_candidate)

    @property
    def installed(self):
        """Return the currently installed version of the package.

        .. versionadded:: 0.7.9
        """
        if self._pkg.CurrentVer is not None:
            return Version(self, self._pkg.CurrentVer)

    @property
    def name(self):
        """Return the name of the package."""
        return self._pkg.Name

    @property
    def id(self):
        """Return a uniq ID for the package.

        This can be used eg. to store additional information about the pkg."""
        return self._pkg.ID

    def __hash__(self):
        """Return the hash of the object.

        This returns the same value as ID, which is unique."""
        return self._pkg.ID

    @DeprecatedProperty
    def installedVersion(self): #pylint: disable-msg=C0103
        """Return the installed version as string.

        .. deprecated:: 0.7.9"""
        return getattr(self.installed, 'version', None)

    @DeprecatedProperty
    def candidateVersion(self): #pylint: disable-msg=C0103
        """Return the candidate version as string.

        .. deprecated:: 0.7.9"""
        return getattr(self.candidate, "version", None)

    @DeprecatedProperty
    def candidateDependencies(self): #pylint: disable-msg=C0103
        """Return a list of candidate dependencies.

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "dependencies", None)

    @DeprecatedProperty
    def installedDependencies(self):  #pylint: disable-msg=C0103
        """Return a list of installed dependencies.

        .. deprecated:: 0.7.9
        """
        return getattr(self.installed, 'dependencies', [])

    @DeprecatedProperty
    def architecture(self):
        """Return the Architecture of the package.

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "architecture", None)

    @DeprecatedProperty
    def candidateDownloadable(self):  #pylint: disable-msg=C0103
        """Return ``True`` if the candidate is downloadable.

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "downloadable", None)

    @DeprecatedProperty
    def installedDownloadable(self):  #pylint: disable-msg=C0103
        """Return ``True`` if the installed version is downloadable.

        .. deprecated:: 0.7.9
        """
        return getattr(self.installed, 'downloadable', False)

    @DeprecatedProperty
    def sourcePackageName(self):  #pylint: disable-msg=C0103
        """Return the source package name as string.

        .. deprecated:: 0.7.9
        """
        try:
            return self.candidate._records.SourcePkg or self._pkg.Name
        except AttributeError:
            try:
                return self.installed._records.SourcePkg or self._pkg.Name
            except AttributeError:
                return self._pkg.Name

    @DeprecatedProperty
    def homepage(self):
        """Return the homepage field as string.

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "homepage", None)

    @property
    def section(self):
        """Return the section of the package."""
        return self._pkg.Section

    @DeprecatedProperty
    def priority(self):
        """Return the priority (of the candidate version).

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "priority", None)

    @DeprecatedProperty
    def installedPriority(self):  #pylint: disable-msg=C0103
        """Return the priority (of the installed version).

        .. deprecated:: 0.7.9
        """
        return getattr(self.installed, 'priority', None)

    @DeprecatedProperty
    def summary(self):
        """Return the short description (one line summary).

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "summary", None)

    @DeprecatedProperty
    def description(self):
        """Return the formatted long description.

        Return the formated long description according to the Debian policy
        (Chapter 5.6.13).
        See http://www.debian.org/doc/debian-policy/ch-controlfields.html
        for more information.

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "description", None)

    @DeprecatedProperty
    def rawDescription(self):  #pylint: disable-msg=C0103
        """return the long description (raw).

        .. deprecated:: 0.7.9"""
        return getattr(self.candidate, "raw_description", None)

    @DeprecatedProperty
    def candidateRecord(self):  #pylint: disable-msg=C0103
        """Return the Record of the candidate version of the package.

        .. deprecated:: 0.7.9"""
        return getattr(self.candidate, "record", None)

    @DeprecatedProperty
    def installedRecord(self):  #pylint: disable-msg=C0103
        """Return the Record of the candidate version of the package.

        .. deprecated:: 0.7.9"""
        return getattr(self.installed, 'record', '')

    # depcache states

    @property
    def marked_install(self):
        """Return ``True`` if the package is marked for install."""
        return self._pcache._depcache.MarkedInstall(self._pkg)

    @property
    def marked_upgrade(self):
        """Return ``True`` if the package is marked for upgrade."""
        return self._pcache._depcache.MarkedUpgrade(self._pkg)

    @property
    def marked_delete(self):
        """Return ``True`` if the package is marked for delete."""
        return self._pcache._depcache.MarkedDelete(self._pkg)

    @property
    def marked_keep(self):
        """Return ``True`` if the package is marked for keep."""
        return self._pcache._depcache.MarkedKeep(self._pkg)

    @property
    def marked_downgrade(self):
        """ Package is marked for downgrade """
        return self._pcache._depcache.MarkedDowngrade(self._pkg)

    @property
    def marked_reinstall(self):
        """Return ``True`` if the package is marked for reinstall."""
        return self._pcache._depcache.MarkedReinstall(self._pkg)

    @property
    def is_installed(self):
        """Return ``True`` if the package is installed."""
        return (self._pkg.CurrentVer is not None)

    @property
    def is_upgradable(self):
        """Return ``True`` if the package is upgradable."""
        return (self.is_installed and
                self._pcache._depcache.IsUpgradable(self._pkg))

    @property
    def is_auto_removable(self):
        """Return ``True`` if the package is no longer required.

        If the package has been installed automatically as a dependency of
        another package, and if no packages depend on it anymore, the package
        is no longer required.
        """
        return self.is_installed and \
               self._pcache._depcache.IsGarbage(self._pkg)

    # sizes

    @DeprecatedProperty
    def packageSize(self):  #pylint: disable-msg=C0103
        """Return the size of the candidate deb package.

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "size", None)

    @DeprecatedProperty
    def installedPackageSize(self):  #pylint: disable-msg=C0103
        """Return the size of the installed deb package.

        .. deprecated:: 0.7.9
        """
        return getattr(self.installed, 'size', 0)

    @DeprecatedProperty
    def candidateInstalledSize(self):  #pylint: disable-msg=C0103
        """Return the size of the candidate installed package.

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "installed_size", None)

    @DeprecatedProperty
    def installedSize(self):  #pylint: disable-msg=C0103
        """Return the size of the currently installed package.


        .. deprecated:: 0.7.9
        """
        return getattr(self.installed, 'installed_size', 0)

    @property
    def installed_files(self):
        """Return a list of files installed by the package.

        Return a list of unicode names of the files which have
        been installed by this package
        """
        path = "/var/lib/dpkg/info/%s.list" % self.name
        try:
            file_list = open(path)
            try:
                return file_list.read().decode().split("\n")
            finally:
                file_list.close()
        except EnvironmentError:
            return []

    def get_changelog(self, uri=None, cancel_lock=None):
        """
        Download the changelog of the package and return it as unicode
        string.

        The parameter *uri* refers to the uri of the changelog file. It may
        contain multiple named variables which will be substitued. These
        variables are (src_section, prefix, src_pkg, src_ver). An example is
        the Ubuntu changelog::

            "http://changelogs.ubuntu.com/changelogs/pool" \\
                "/%(src_section)s/%(prefix)s/%(src_pkg)s" \\
                "/%(src_pkg)s_%(src_ver)s/changelog"

        The parameter *cancel_lock* refers to an instance of threading.Lock,
        which if set, prevents the download.
        """
        # Return a cached changelog if available
        if self._changelog != "":
            return self._changelog

        if uri is None:
            if not self.candidate:
                pass
            if self.candidate.origins[0].origin == "Debian":
                uri = "http://packages.debian.org/changelogs/pool" \
                      "/%(src_section)s/%(prefix)s/%(src_pkg)s" \
                      "/%(src_pkg)s_%(src_ver)s/changelog"
            elif self.candidate.origins[0].origin == "Ubuntu":
                uri = "http://changelogs.ubuntu.com/changelogs/pool" \
                      "/%(src_section)s/%(prefix)s/%(src_pkg)s" \
                      "/%(src_pkg)s_%(src_ver)s/changelog"
            else:
                return _("The list of changes is not available")

        # get the src package name
        src_pkg = self.candidate.source_name

        # assume "main" section
        src_section = "main"
        # use the section of the candidate as a starting point
        section = self.candidate.section

        # get the source version, start with the binaries version
        bin_ver = self.candidate.version
        src_ver = self.candidate.version
        #print "bin: %s" % binver
        try:
            # FIXME: This try-statement is too long ...
            # try to get the source version of the pkg, this differs
            # for some (e.g. libnspr4 on ubuntu)
            # this feature only works if the correct deb-src are in the
            # sources.list
            # otherwise we fall back to the binary version number
            src_records = apt_pkg.SourceRecords()
            src_rec = src_records.Lookup(src_pkg)
            if src_rec:
                src_ver = src_records.Version
                #if apt_pkg.VersionCompare(binver, srcver) > 0:
                #    srcver = binver
                if not src_ver:
                    src_ver = bin_ver
                #print "srcver: %s" % src_ver
                section = src_records.Section
                #print "srcsect: %s" % section
            else:
                # fail into the error handler
                raise SystemError
        except SystemError:
            src_ver = bin_ver

        l = section.split("/")
        if len(l) > 1:
            src_section = l[0]

        # lib is handled special
        prefix = src_pkg[0]
        if src_pkg.startswith("lib"):
            prefix = "lib" + src_pkg[3]

        # stip epoch
        l = src_ver.split(":")
        if len(l) > 1:
            src_ver = "".join(l[1:])

        uri = uri % {"src_section": src_section,
                     "prefix": prefix,
                     "src_pkg": src_pkg,
                     "src_ver": src_ver}

        timeout = socket.getdefaulttimeout()

        # FIXME: when python2.4 vanishes from the archive,
        #        merge this into a single try..finally block (pep 341)
        try:
            try:
                # Set a timeout for the changelog download
                socket.setdefaulttimeout(2)

                # Check if the download was canceled
                if cancel_lock and cancel_lock.isSet():
                    return ""
                changelog_file = urllib2.urlopen(uri)
                # do only get the lines that are new
                changelog = ""
                regexp = "^%s \((.*)\)(.*)$" % (re.escape(src_pkg))
                while True:
                    # Check if the download was canceled
                    if cancel_lock and cancel_lock.isSet():
                        return ""
                    # Read changelog line by line
                    line_raw = changelog_file.readline()
                    if line_raw == "":
                        break
                    # The changelog is encoded in utf-8, but since there isn't
                    # any http header, urllib2 seems to treat it as ascii
                    line = line_raw.decode("utf-8")

                    #print line.encode('utf-8')
                    match = re.match(regexp, line)
                    if match:
                        # strip epoch from installed version
                        # and from changelog too
                        installed = getattr(self.installed, 'version', None)
                        if installed and ":" in installed:
                            installed = installed.split(":", 1)[1]
                        changelog_ver = match.group(1)
                        if changelog_ver and ":" in changelog_ver:
                            changelog_ver = changelog_ver.split(":", 1)[1]
                        if (installed and apt_pkg.VersionCompare(changelog_ver,
                                                              installed) <= 0):
                            break
                    # EOF (shouldn't really happen)
                    changelog += line

                # Print an error if we failed to extract a changelog
                if len(changelog) == 0:
                    changelog = _("The list of changes is not available")
                self._changelog = changelog

            except urllib2.HTTPError:
                return _("The list of changes is not available yet.\n\n"
                         "Please use http://launchpad.net/ubuntu/+source/%s/"
                         "%s/+changelog\n"
                         "until the changes become available or try again "
                         "later.") % (src_pkg, src_ver)
            except (IOError, httplib.BadStatusLine):
                return _("Failed to download the list of changes. \nPlease "
                         "check your Internet connection.")
        finally:
            socket.setdefaulttimeout(timeout)
        return self._changelog

    @DeprecatedProperty
    def candidateOrigin(self):  #pylint: disable-msg=C0103
        """Return a list of `Origin()` objects for the candidate version.

        .. deprecated:: 0.7.9
        """
        return getattr(self.candidate, "origins", None)

    @property
    def versions(self):
        """Return a list of versions.

        .. versionadded:: 0.7.9
        """
        return [Version(self, ver) for ver in self._pkg.VersionList]

    # depcache actions

    def mark_keep(self):
        """Mark a package for keep."""
        self._pcache.cache_pre_change()
        self._pcache._depcache.MarkKeep(self._pkg)
        self._pcache.cache_post_change()

    @deprecated_args
    def mark_delete(self, auto_fix=True, purge=False):
        """Mark a package for install.

        If *auto_fix* is ``True``, the resolver will be run, trying to fix
        broken packages.  This is the default.

        If *purge* is ``True``, remove the configuration files of the package
        as well.  The default is to keep the configuration.
        """
        self._pcache.cache_pre_change()
        self._pcache._depcache.MarkDelete(self._pkg, purge)
        # try to fix broken stuffsta
        if auto_fix and self._pcache._depcache.BrokenCount > 0:
            fix = apt_pkg.ProblemResolver(self._pcache._depcache)
            fix.Clear(self._pkg)
            fix.Protect(self._pkg)
            fix.Remove(self._pkg)
            fix.InstallProtect()
            fix.Resolve()
        self._pcache.cache_post_change()

    @deprecated_args
    def mark_install(self, auto_fix=True, auto_inst=True, from_user=True):
        """Mark a package for install.

        If *autoFix* is ``True``, the resolver will be run, trying to fix
        broken packages.  This is the default.

        If *autoInst* is ``True``, the dependencies of the packages will be
        installed automatically.  This is the default.

        If *fromUser* is ``True``, this package will not be marked as
        automatically installed. This is the default. Set it to False if you
        want to be able to automatically remove the package at a later stage
        when no other package depends on it.
        """
        self._pcache.cache_pre_change()
        self._pcache._depcache.MarkInstall(self._pkg, auto_inst, from_user)
        # try to fix broken stuff
        if auto_fix and self._pcache._depcache.BrokenCount > 0:
            fixer = apt_pkg.ProblemResolver(self._pcache._depcache)
            fixer.Clear(self._pkg)
            fixer.Protect(self._pkg)
            fixer.Resolve(True)
        self._pcache.cache_post_change()

    def mark_upgrade(self):
        """Mark a package for upgrade."""
        if self.is_upgradable:
            self.mark_install()
        else:
            # FIXME: we may want to throw a exception here
            sys.stderr.write(("MarkUpgrade() called on a non-upgrable pkg: "
                              "'%s'\n") % self._pkg.Name)

    def commit(self, fprogress, iprogress):
        """Commit the changes.

        The parameter *fprogress* refers to a FetchProgress() object, as
        found in apt.progress.

        The parameter *iprogress* refers to an InstallProgress() object, as
        found in apt.progress.
        """
        self._pcache._depcache.Commit(fprogress, iprogress)


    if not apt_pkg._COMPAT_0_7:
        del installedVersion
        del candidateVersion
        del candidateDependencies
        del installedDependencies
        del architecture
        del candidateDownloadable
        del installedDownloadable
        del sourcePackageName
        del homepage
        del priority
        del installedPriority
        del summary
        del description
        del rawDescription
        del candidateRecord
        del installedRecord
        del packageSize
        del installedPackageSize
        del candidateInstalledSize
        del installedSize
        del candidateOrigin
    else:
        markedInstalled = AttributeDeprecatedBy('marked_installed')
        markedUpgrade = AttributeDeprecatedBy('marked_upgrade')
        markedDelete = AttributeDeprecatedBy('marked_delete')
        markedKeep = AttributeDeprecatedBy('marked_keep')
        markedDowngrade = AttributeDeprecatedBy('marked_downgrade')
        markedReinstall = AttributeDeprecatedBy('marked_reinstall')
        isInstalled = AttributeDeprecatedBy('is_installed')
        isUpgradable = AttributeDeprecatedBy('is_upgradable')
        isAutoRemovable = AttributeDeprecatedBy('is_auto_removable')
        installedFiles = AttributeDeprecatedBy('installed_files')
        getChangelog = function_deprecated_by(get_changelog)
        markDelete = function_deprecated_by(mark_delete)
        markInstall = function_deprecated_by(mark_install)
        markKeep = function_deprecated_by(mark_keep)
        markUpgrade = function_deprecated_by(mark_upgrade)


def _test():
    """Self-test."""
    print "Self-test for the Package modul"
    import random
    apt_pkg.init()
    progress = apt.progress.OpTextProgress()
    cache = apt.Cache(progress)
    pkg = cache["apt-utils"]
    print "Name: %s " % pkg.name
    print "ID: %s " % pkg.id
    print "Priority (Candidate): %s " % pkg.candidate.priority
    print "Priority (Installed): %s " % pkg.installed.priority
    print "Installed: %s " % pkg.installed.version
    print "Candidate: %s " % pkg.candidate.version
    print "CandidateDownloadable: %s" % pkg.candidate.downloadable
    print "CandidateOrigins: %s" % pkg.candidate.origins
    print "SourcePkg: %s " % pkg.candidate.source_name
    print "Section: %s " % pkg.section
    print "Summary: %s" % pkg.candidate.summary
    print "Description (formated) :\n%s" % pkg.candidate.description
    print "Description (unformated):\n%s" % pkg.candidate.raw_description
    print "InstalledSize: %s " % pkg.candidate.installed_size
    print "PackageSize: %s " % pkg.candidate.size
    print "Dependencies: %s" % pkg.installed.dependencies
    for dep in pkg.candidate.dependencies:
        print ",".join("%s (%s) (%s) (%s)" % (o.name, o.version, o.relation,
                        o.pre_depend) for o in dep.or_dependencies)
    print "arch: %s" % pkg.candidate.architecture
    print "homepage: %s" % pkg.candidate.homepage
    print "rec: ", pkg.candidate.record


    print cache["2vcard"].get_changelog()
    for i in True, False:
        print "Running install on random upgradable pkgs with AutoFix: %s " % i
        for pkg in cache:
            if pkg.is_upgradable:
                if random.randint(0, 1) == 1:
                    pkg.mark_install(i)
        print "Broken: %s " % cache._depcache.BrokenCount
        print "InstCount: %s " % cache._depcache.InstCount

    print
    # get a new cache
    for i in True, False:
        print "Randomly remove some packages with AutoFix: %s" % i
        cache = apt.Cache(progress)
        for name in cache.keys():
            if random.randint(0, 1) == 1:
                try:
                    cache[name].mark_delete(i)
                except SystemError:
                    print "Error trying to remove: %s " % name
        print "Broken: %s " % cache._depcache.BrokenCount
        print "DelCount: %s " % cache._depcache.DelCount

# self-test
if __name__ == "__main__":
    _test()
