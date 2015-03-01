# -*- test-case-name: admin.test.test_release -*-
# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Helper utilities for the Flocker release process.

XXX This script is not automatically checked by buildbot. See
https://clusterhq.atlassian.net/browse/FLOC-397
"""

import sys
import tempfile

import os
import boto
from subprocess import check_call
from textwrap import dedent

from collections import namedtuple
from effect import (
    Effect, sync_perform, ComposedDispatcher, base_dispatcher)
from effect.do import do
from characteristic import attributes

from twisted.python.filepath import FilePath
from twisted.python.usage import Options, UsageError
from twisted.python.constants import Names, NamedConstant

import flocker

from flocker.docs import get_doc_version, is_release, is_weekly_release

from .aws import (
    boto_dispatcher,
    UpdateS3RoutingRule,
    ListS3Keys,
    DeleteS3Keys,
    CopyS3Keys,
    CreateCloudFrontInvalidation,
)


__all__ = ['rpm_version', 'make_rpm_version']

# Use characteristic instead.
# https://clusterhq.atlassian.net/browse/FLOC-1223
rpm_version = namedtuple('rpm_version', 'version release')


def make_rpm_version(flocker_version):
    """
    Parse the Flocker version generated by versioneer into an RPM compatible
    version and a release version.
    See: http://fedoraproject.org/wiki/Packaging:NamingGuidelines#Pre-Release_packages  # noqa

    :param flocker_version: The versioneer style Flocker version string.
    :return: An ``rpm_version`` tuple containing a ``version`` and a
        ``release`` attribute.
    """
    # E.g. 0.1.2-69-gd2ff20c-dirty
    # tag+distance+shortid+dirty
    parts = flocker_version.split('-')
    tag, remainder = parts[0], parts[1:]
    for suffix in ('pre', 'dev'):
        parts = tag.rsplit(suffix, 1)
        if len(parts) == 2:
            # A pre or dev suffix was present. ``version`` is the part before
            # the pre and ``suffix_number`` is the part after the pre, but
            # before the first dash.
            version = parts.pop(0)
            suffix_number = parts[0]
            if suffix_number.isdigit():
                # Given pre or dev number X create a 0 prefixed, `.` separated
                # string of version labels. E.g.
                # 0.1.2pre2  becomes
                # 0.1.2-0.pre.2
                release = ['0', suffix, suffix_number]
            else:
                # Non-integer pre or dev number found.
                raise Exception(
                    'Non-integer value "{}" for "{}". '
                    'Supplied version {}'.format(
                        suffix_number, suffix, flocker_version))
            break
    else:
        # Neither of the expected suffixes was found, the tag can be used as
        # the RPM version
        version = tag
        release = ['1']

    if remainder:
        # The version may also contain a distance, shortid which
        # means that there have been changes since the last
        # tag. Additionally there may be a ``dirty`` suffix which
        # indicates that there are uncommitted changes in the
        # working directory.  We probably don't want to release
        # untagged RPM versions, and this branch should probably
        # trigger and error or a warning. But for now we'll add
        # that extra information to the end of release number.
        # See https://clusterhq.atlassian.net/browse/FLOC-833
        release.extend(remainder)

    return rpm_version(version, '.'.join(release))


class NotTagged(Exception):
    """
    Raised if publishing to production and the version being published version
    isn't tagged.
    """


class NotARelease(Exception):
    """
    Raised if trying to publish to a version that isn't a release or upload
    packages for a version that isn't a release.
    """

class DocumentationRelease(Exception):
    """
    Raised if trying to upload packages for a documentation release.
    """

class Environments(Names):
    """
    The environments that documentation can be published to.
    """
    PRODUCTION = NamedConstant()
    STAGING = NamedConstant()


@attributes([
    'documentation_bucket',
    'cloudfront_cname',
    'dev_bucket',
])
class DocumentationConfiguration(object):
    """
    The configuration for publishing documentation.

    :ivar bytes documentation_bucket: The bucket to publish documentation to.
    :ivar bytes cloudfront_cname: a CNAME associated to the cloudfront
        distribution pointing at the documentation bucket.
    :ivar bytes dev_bucket: The bucket buildbot uploads documentation to.
    """

DOCUMENTATION_CONFIGURATIONS = {
    Environments.PRODUCTION:
        DocumentationConfiguration(
            documentation_bucket="clusterhq-docs",
            cloudfront_cname="docs.clusterhq.com",
            dev_bucket="clusterhq-dev-docs"),
    Environments.STAGING:
        DocumentationConfiguration(
            documentation_bucket="clusterhq-staging-docs",
            cloudfront_cname="docs.staging.clusterhq.com",
            dev_bucket="clusterhq-dev-docs"),
}


@do
def publish_docs(flocker_version, doc_version, environment):
    """
    Publish the flocker documentation.

    :param bytes flocker_version: The version of flocker to publish the
        documentation for.
    :param bytes doc_version: The version to publish the documentation as.
    :param Environments environment: The environment to publish the
        documentation to.
    :raises NotARelease: Raised if trying to publish to a version that isn't a
        release.
    :raises NotTagged: Raised if publishing to production and the version being
        published version isn't tagged.
    """
    if not (is_release(doc_version)
            or is_weekly_release(doc_version)):
        raise NotARelease

    if environment == Environments.PRODUCTION:
        if get_doc_version(flocker_version) != doc_version:
            raise NotTagged
    configuration = DOCUMENTATION_CONFIGURATIONS[environment]

    dev_prefix = '%s/' % (flocker_version,)
    version_prefix = 'en/%s/' % (doc_version,)

    # This might be clearer as ``is_weekly_release(doc_version)``,
    # but it is more important to never publish a non-marketing release as
    # /latest/, so we key off being a marketing release.
    is_dev = not is_release(doc_version)
    if is_dev:
        stable_prefix = "en/devel/"
    else:
        stable_prefix = "en/latest/"

    # Get the list of keys in the new documentation.
    new_version_keys = yield Effect(
        ListS3Keys(bucket=configuration.dev_bucket,
                   prefix=dev_prefix))
    # Get the list of keys already existing for the given version.
    # This should only be non-empty for documentation releases.
    existing_version_keys = yield Effect(
        ListS3Keys(bucket=configuration.documentation_bucket,
                   prefix=version_prefix))

    # Copy the new documentation to the documentation bucket.
    yield Effect(
        CopyS3Keys(source_bucket=configuration.dev_bucket,
                   source_prefix=dev_prefix,
                   destination_bucket=configuration.documentation_bucket,
                   destination_prefix=version_prefix,
                   keys=new_version_keys))

    # Delete any keys that aren't in the new documentation.
    yield Effect(
        DeleteS3Keys(bucket=configuration.documentation_bucket,
                     prefix=version_prefix,
                     keys=existing_version_keys - new_version_keys))

    # Update the redirect for the stable URL (en/latest/ or en/devel/)
    # to point to the new version. Returns the old target.
    old_prefix = yield Effect(
        UpdateS3RoutingRule(bucket=configuration.documentation_bucket,
                            prefix=stable_prefix,
                            target_prefix=version_prefix))

    # If we have changed versions, get all the keys from the old version
    if old_prefix:
        previous_version_keys = yield Effect(
            ListS3Keys(bucket=configuration.documentation_bucket,
                       prefix=old_prefix))
    else:
        previous_version_keys = set()

    # The changed keys are the new keys, the keys that were deleted from this
    # version, and the keys for the previous version.
    changed_keys = (new_version_keys |
                    existing_version_keys |
                    previous_version_keys)

    # S3 serves /index.html when given /, so any changed /index.html means
    # that / changed as well.
    # Note that we check for '/index.html' but remove 'index.html'
    changed_keys |= {key_name[:-len('index.html')]
                     for key_name in changed_keys
                     if key_name.endswith('/index.html')}

    # Always update the root.
    changed_keys |= {''}

    # The full paths are all the changed keys under the stable prefix, and
    # the new version prefix. This set is slightly bigger than necessary.
    changed_paths = {prefix + key_name
                     for key_name in changed_keys
                     for prefix in [stable_prefix, version_prefix]}

    # Invalidate all the changed paths in cloudfront.
    yield Effect(
        CreateCloudFrontInvalidation(cname=configuration.cloudfront_cname,
                                     paths=changed_paths))


class PublishDocsOptions(Options):
    """
    Arguments for ``publish-docs`` script.
    """

    optParameters = [
        ["flocker-version", None, flocker.__version__,
         "The version of flocker from which the documentation was built."],
        ["doc-version", None, None,
         "The version to publish the documentation as.\n"
         "This will differ from \"flocker-version\" for staging uploads and "
         "documentation releases."],
    ]

    optFlags = [
        ["production", None, "Publish documentation to production."],
    ]

    environment = Environments.STAGING

    def parseArgs(self):
        if self['doc-version'] is None:
            self['doc-version'] = get_doc_version(self['flocker-version'])

        if self['production']:
            self.environment = Environments.PRODUCTION


def publish_docs_main(args, base_path, top_level):
    """
    :param list args: The arguments passed to the script.
    :param FilePath base_path: The executable being run.
    :param FilePath top_level: The top-level of the flocker repository.
    """
    options = PublishDocsOptions()

    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)

    try:
        sync_perform(
            dispatcher=ComposedDispatcher([boto_dispatcher, base_dispatcher]),
            effect=publish_docs(
                flocker_version=options['flocker-version'],
                doc_version=options['doc-version'],
                environment=options.environment,
                ))
    except NotARelease:
        sys.stderr.write("%s: Can't publish non-release."
                         % (base_path.basename(),))
        raise SystemExit(1)
    except NotTagged:
        sys.stderr.write("%s: Can't publish non-tagged version to production."
                         % (base_path.basename(),))
        raise SystemExit(1)


class UploadOptions(Options):
    """
    Options for uploading packages.
    """
    optParameters = [
        ["target", None, b'clusterhq-yum-repository',
         "The bucket to upload packages to."],
        ["build-server", None,
         b'http://build.clusterhq.com',
         "The URL of the build-server."],
    ]

    def parseArgs(self, version):
        self['version'] = version


FLOCKER_PACKAGES = [
    b'clusterhq-python-flocker',
    b'clusterhq-flocker-cli',
    b'clusterhq-flocker-node',
]


def update_repo(rpm_directory, target_bucket, target_key, source_repo, packages):
    """
    Update ``target_repo`` yum repository with ``packages`` from
    ``source_repo`` repository.
    """
    # Import this here so that this file can be imported when yum is not
    # available.
    from yum import YumBase

    rpm_directory.createDirectory()
    s3 = boto.connect_s3()
    try:
        # Does not work if there is a '.' in the name
        bucket = s3.get_bucket(bucket_name=target_bucket)
    except boto.exception.S3ResponseError:
        bucket = s3.create_bucket(bucket_name=target_bucket)

    # Download existing repository
    for item in bucket.list(prefix=target_key):
        new_item_path = os.path.join(rpm_directory.path, str(item.key))
        if not os.path.exists(new_item_path):
            parent = FilePath(new_item_path).parent()
            if not parent.exists():
                parent.makedirs()
            item.get_contents_to_filename(new_item_path)

    # Download requested packages from source repository
    base = YumBase()
    # This is necessary to avoid permission issues with the default,
    # /var/cache/yum/x86_64/20
    base.setCacheDir()

    base.repos.disableRepo('*')
    repo = base.add_enable_repo(repoid='flocker', baseurls=[source_repo])

    # TODO Run twice without this to see if it is stil necessary.
    base.cleanMetadata()

    # XXX This could be more efficient by only downloading the changed files
    # https://clusterhq.atlassian.net/browse/FLOC-1506
    yum_packages = base.pkgSack.returnPackages(repoid=repo.name,
                                               patterns=packages)
    repo.pkgdir = os.path.join(rpm_directory.path, target_key)
    base.downloadPkgs(yum_packages)

    # Update repository metadata
    check_call([b'createrepo', b'--update',
                os.path.join(rpm_directory.path, target_key)])

    # Upload updated repository
    for root, dirs, files in os.walk(rpm_directory.path):
        for name in files:
            source_path = os.path.join(root, name)
            destination_path = os.path.relpath(source_path, rpm_directory.path)
            key = bucket.new_key(destination_path)
            key.set_contents_from_filename(source_path)
            key.make_public()


def upload_rpms(scratch_directory, target_bucket, version, build_server):
    """
    Upload RPMS from build server to yum repository.

    :param FilePath scratch_directory: Temporary directory to download
        repository to.
    :param bytes version: Version to download RPMs for.
    :param bytes build_server: Server to download new RPMs from.
    """
    if not (is_release(version)
            or is_weekly_release(version)):
        raise NotARelease

    if get_doc_version(version) != version:
        raise DocumentationRelease

    if is_release(version):
        release_type = "marketing"
    elif is_weekly_release(version):
        release_type = "development"

    update_repo(rpm_directory=scratch_directory.child(b'fedora-20-x86_64'),
                target_bucket=target_bucket,
                target_key=os.path.join(release_type, b'fedora', b'20', b'x86_64'),
                source_repo=os.path.join(build_server, b'results/omnibus',
                                         version, 'fedora-20'),
                packages=FLOCKER_PACKAGES)

    update_repo(rpm_directory=scratch_directory.child(b'centos-7-x86_64'),
                target_bucket=target_bucket,
                target_key=os.path.join(release_type, b'centos', b'7', b'x86_64'),
                source_repo=os.path.join(build_server, b'results/omnibus',
                                         version, 'centos-7'),
                packages=FLOCKER_PACKAGES)


def upload_rpms_main(args, base_path, top_level):
    """
    The ClusterHQ yum repository contains packages for Flocker, as well as the
    dependencies which aren't available in Fedora 20 or CentOS 7. It is
    currently hosted on Amazon S3. When doing a release, we want to add the
    new Flocker packages, while preserving the existing packages in the
    repository. To do this, we download the current repository, add the new
    package, update the metadata, and then upload the repository.

    :param list args: The arguments passed to the script.
    :param FilePath base_path: The executable being run.
    :param FilePath top_level: The top-level of the flocker repository.
    """
    options = UploadOptions()

    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)

    try:
        scratch_directory = FilePath(tempfile.mkdtemp(
                prefix=b'flocker-upload-rpm-'))

        upload_rpms(scratch_directory=scratch_directory,
                    target_bucket=options['target'],
                    version=options['version'],
                    build_server=options['build-server'])
    except NotARelease:
        sys.stderr.write("%s: Can't upload RPMs for a non-release."
                         % (base_path.basename(),))
        raise SystemExit(1)
    except DocumentationRelease:
        sys.stderr.write("%s: Can't upload RPMs for a documentation release."
                         % (base_path.basename(),))
        raise SystemExit(1)
    finally:
        scratch_directory.remove()

"""
TODOs

This also covers:
https://clusterhq.atlassian.net/browse/FLOC-506

docstrings

Test with FakeAWS?
Make it a verified fake?

createrepo - `pip install pakrat` might be able to do this
change to `mkvirtualenv --system-site-packages flocker; pip install --ignore-installed , for yum package

change install instructions to point to AWS

Change the package repository:
    # https://github.com/ClusterHQ/fedora-packages/
    - In GCS there is a SRPMS/ directory and clusterhq-release.fc20.noarch.rpm file.
      It seems like this can be duplicated in S3 by changing (and following)
      the instructions in the README
    - https://github.com/ClusterHQ/fedora-packages/blob/master/clusterhq.repo
      Should have a new section, [clusterhq-development], and should be changed
      to reference the new locations.
      Instead of hard coding /fedora/ use $osname which can be set as fedora
      or centos by putting e.g. ``$ echo "fedora" > /etc/yum/vars/osname`` in
      the install instructions.
    - The name of this repository and e.g. the description in
      https://github.com/ClusterHQ/fedora-packages/blob/master/clusterhq-release.spec
      are out of date. This should probably be changed to be less platform-specific.

Move 0.3.2 release to S3

How does http://archive.clusterhq.com work?
"""
