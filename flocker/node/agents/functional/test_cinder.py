# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Functional tests for ``flocker.node.agents.cinder`` using a real OpenStack
cluster.

Ideally, there'd be some in-memory tests too. Some ideas:
 * Maybe start a `mimic` server and use it to at test just the authentication
   step.
 * Mimic doesn't currently fake the cinder APIs but perhaps we could contribute
   that feature.

See https://github.com/rackerlabs/mimic/issues/218
"""

from uuid import uuid4

from bitmath import Byte

from eliot.testing import assertHasAction, capture_logging
from twisted.trial.unittest import SkipTest

from zope.interface import implementer

from keystoneclient.openstack.common.apiclient.exceptions import (
    RequestEntityTooLarge as KeystoneOverLimit
)

# make_iblockdeviceapi_tests should really be in flocker.node.agents.testtools,
# but I want to keep the branch size down
from ..test.test_blockdevice import (
    make_iblockdeviceapi_tests,
)
from ..test.blockdevicefactory import (
    InvalidConfig, ProviderType, get_blockdeviceapi_args,
    get_blockdeviceapi_with_cleanup, get_device_allocation_unit,
    get_minimum_allocatable_size,
)

from ..cinder import (
    wait_for_volume, INovaServerManager, auto_openstack_retry,
    MAX_OVERLIMIT_RETRIES
)

from .._logging import RETRY_ACTION

def cinderblockdeviceapi_for_test(test_case):
    """
    Create a ``CinderBlockDeviceAPI`` instance for use in tests.

    :param TestCase test_case: The test being run.

    :returns: A ``CinderBlockDeviceAPI`` instance.  Any volumes it creates will
        be cleaned up at the end of the test (using ``test_case``\ 's cleanup
        features).
    """
    return get_blockdeviceapi_with_cleanup(test_case, ProviderType.openstack)


# ``CinderBlockDeviceAPI`` only implements the ``create`` and ``list`` parts of
# ``IBlockDeviceAPI``. Skip the rest of the tests for now.
class CinderBlockDeviceAPIInterfaceTests(
        make_iblockdeviceapi_tests(
            blockdevice_api_factory=(
                lambda test_case: cinderblockdeviceapi_for_test(
                    test_case=test_case,
                )
            ),
            minimum_allocatable_size=get_minimum_allocatable_size(),
            device_allocation_unit=get_device_allocation_unit(),
            unknown_blockdevice_id_factory=lambda test: unicode(uuid4()),
        )
):
    """
    Interface adherence Tests for ``CinderBlockDeviceAPI``.
    """
    def test_foreign_volume(self):
        """
        Non-Flocker Volumes are not listed.
        """
        try:
            cls, kwargs = get_blockdeviceapi_args(ProviderType.openstack)
        except InvalidConfig as e:
            raise SkipTest(str(e))
        cinder_client = kwargs["cinder_client"]
        requested_volume = cinder_client.volumes.create(
            size=int(Byte(self.minimum_allocatable_size).to_GiB().value)
        )
        self.addCleanup(
            cinder_client.volumes.delete,
            requested_volume.id,
        )
        wait_for_volume(
            volume_manager=cinder_client.volumes,
            expected_volume=requested_volume
        )
        self.assertEqual([], self.api.list_volumes())

    def test_foreign_cluster_volume(self):
        """
        Test that list_volumes() excludes volumes belonging to
        other Flocker clusters.
        """
        blockdevice_api2 = cinderblockdeviceapi_for_test(
            test_case=self,
            )
        flocker_volume = blockdevice_api2.create_volume(
            dataset_id=uuid4(),
            size=self.minimum_allocatable_size,
            )
        self.assert_foreign_volume(flocker_volume)


    @capture_logging(assertHasAction, RETRY_ACTION,
                     succeeded=True,
                     startFields=dict(iteration=1))
    def test_retry_decorator(self, logger):
        """
        Test that ``auto_openstack_retry`` decorator reattempts
        failed ``INovaServerManager`` class methods.
        """
        @implementer(INovaServerManager)
        class DummyNovaServerManager(object):
            """
            Dummy class that implements ``INovaServerManager``.
            """
            def __init__(self, retry_limit):
                """
                Setup env to control success/failure of ``list``.

                """
                self._counter = 0
                self._retry_limit = retry_limit

            def list(self):
                """
                If the method is called up to _retry_limit times, raise
                ``KeystoneOverLimit``. Beyond retry_limit, succeed.
                """
                self._counter += 1
                if self._counter < self._retry_limit:
                    raise KeystoneOverLimit
                else:
                    return self._counter

        retry_limit = MAX_OVERLIMIT_RETRIES

        @auto_openstack_retry(INovaServerManager, "_dummy")
        class RetryDummy(object):
            def __init__(self, dummy):
                self._dummy = dummy

        retry_dummy1 = RetryDummy(DummyNovaServerManager(MAX_OVERLIMIT_RETRIES))
        self.assertEqual(retry_limit, retry_dummy1.list())

        retry_dummy2 = RetryDummy(DummyNovaServerManager(
                                  MAX_OVERLIMIT_RETRIES+1))
        self.assertRaises(KeystoneOverLimit, retry_dummy2.list())
