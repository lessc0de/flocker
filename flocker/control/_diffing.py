# Copyright ClusterHQ Inc.  See LICENSE file for details.
# -*- test-case-name: flocker.control.test.test_diffing -*-

"""
Code to calculate the difference between objects. This is particularly useful
for computing the difference between deeply pyrsisistent objects such as the
flocker configuration or the flocker state.
"""

from eliot import MessageType, Field

from pyrsistent import (
    PClass,
    PMap,
    PSet,
    field,
    pvector,
    pvector_field,
)
from pyrsistent._transformations import _get

from zope.interface import Interface, implementer


class _IDiffChange(Interface):
    """
    Interface for a diff change.

    This is simply something that can be applied to an object to create a new
    object.

    This interface is created as documentation rather than for any of the
    actual zope.interface mechanisms.
    """

    def apply(obj):
        """
        Apply this diff change to the passed in object and return a new object
        that is obj with the ``self`` diff applied.

        :param object obj: The object to apply the diff to.

        :returns: A new object that is the passed in object with the diff
            applied.
        """


@implementer(_IDiffChange)
class _Remove(PClass):
    """
    A ``_IDiffChange`` that removes an object from a ``PSet`` or a key from a
    ``PMap`` inside a nested object tree.

    :ivar path: The path in the nested object tree of the object to be removed
        from the import set.

    :ivar item: The item to be removed from the set or the key to be removed
        from the mapping.
    """
    path = pvector_field(object)
    item = field()

    def apply(self, obj):
        return obj.transform(self.path, lambda o: o.remove(self.item))


@implementer(_IDiffChange)
class _Set(PClass):
    """
    A ``_IDiffChange`` that sets a field in a ``PClass`` or sets a key in a
    ``PMap``.

    :ivar path: The path in the nested object to the field/key to be set to a
        new value.

    :ivar value: The value to set the field/key to.
    """
    path = pvector_field(object)
    value = field()

    def apply(self, obj):
        return obj.transform(
            self.path[:-1], lambda o: o.set(self.path[-1], self.value)
        )


@implementer(_IDiffChange)
class _Add(PClass):
    """
    A ``_IDiffChange`` that adds an item to a ``PSet``.

    :ivar path: The path to the set to which the item will be added.

    :ivar item: The item to be added to the set.
    """
    path = pvector_field(object)
    item = field()

    def apply(self, obj):
        return obj.transform(self.path, lambda x: x.add(self.item))


_sentinel = object()


class _EvolverProxy(object):
    """
    This attempts to bunch all the diff operations for a particular object into
    a single transaction so that related attributes can be ``set`` without
    triggering an in invariant error.
    Additionally, the leaf nodes are persisted first and in isolation, so as
    not to trigger invariant errors in ancestor nodes.
    """
    def __init__(self, original):
        """
        :param PClass original: The root object to which transformations will
            be applied.
        """
        self._original = original
        self._evolver = original.evolver()
        self._children = {}
        self._operations = []

    def _child(self, segment):
        child = self._children.get(segment)
        if child is not None:
            return child
        child = _get(self._original, segment, _sentinel)
        if child is _sentinel:
            raise KeyError(
                'Segment not found in path. '
                'Parent: {}, '
                'Segment: {}'.format(self, segment)
            )
        proxy_for_child = _EvolverProxy(child)
        self._children[segment] = proxy_for_child
        return proxy_for_child

    def transform(self, path, operation):
        """
        Traverse each segment of ``path`` to create a hierarchy of
        ``_EvolverProxy`` objects and perform the ``operation`` on the
        resulting leaf proxy object. This will infact perform the operation on
        an evolver of the original Pyrsistent object.

        :param PVector path: The path relative to ``original`` which will be
            operated on.
        :param callable operation: A function to be applied to an evolver of
             the object at ``path``
        :returns: ``self``
        """
        target = self
        for segment in path:
            target = target._child(segment)
        operation(target)
        return self

    def add(self, item):
        """
        Add ``item`` to the ``original`` ``Pset`` or if the item is itself a
        Pyrsistent object, add a new proxy for that item so that further
        operations can be performed on it without triggering invariant checks
        until the tree is finally committed.

        :param item: An object to be added to the ``PSet`` wrapped by this
            proxy.
        :returns: ``self``
        """
        if hasattr(item, 'evolver'):
            self._children[item] = _EvolverProxy(item)
        else:
            self._evolver.add(item)
        return self

    def set(self, key, item):
        """
        Set the ``item`` in an evolver of the ``original`` ``PMap`` or
        ``PClass`` or if the item is itself a Pyrsistent object, add a new
        proxy for that item so that further operations can be performed on it
        without triggering invariant checks until the tree is finally
        committed.

        :param item: An object to be added or set on the ``PMap`` wrapped by
            this proxy.
        :returns: ``self``
        """
        if hasattr(item, 'evolver'):
            # This will replace any existing proxy.
            self._children[key] = _EvolverProxy(item)
        else:
            self._evolver.set(key, item)
        return self

    def remove(self, item):
        """
        Remove the ``item`` in an evolver of the ``original`` ``PMap``,
        ``PClass``, or ``PSet`` and if the item is an uncommitted
        ``_EvolverProxy`` remove it from the list of children so that the item
        is not persisted when the structure is finally committed.

        :param item: The object to be removed from the wrapped ``PSet`` or the
            key to be removed from the wrapped ``PMap``
        :returns: ``self``
        """
        self._children.pop(item, None)
        # Attempt to remove the item from the evolver too.  It may be something
        # that was replaced rather than added by a previous ``set`` operation.
        try:
            self._evolver.remove(item)
        except KeyError:
            pass
        return self

    def commit(self):
        """
        Persist all the changes made to the descendants of this structure, then
        persist the resulting sub-objects and local changes to this root object
        and finally return the resulting immutable structure.

        :returns: The updated and persisted version of ``original``.
        """
        for segment, child_evolver_proxy in self._children.items():
            child = child_evolver_proxy.commit()
            # XXX this is ugly. Perhaps have a separate proxy for PClass, PMap
            # and PSet collections
            if hasattr(self._evolver, 'set'):
                self._evolver.set(segment, child)
            else:
                self._evolver.add(child)
        return self._evolver.persistent()


TARGET_OBJECT = Field(
    u"target_object", repr,
    u"The object to which the diff was applied."
)
CHANGES = Field(
    u"changes", repr,
    u"The changes being applied."
)

DIFF_COMMIT_ERROR = MessageType(
    u"flocker:control:Diff:commit_error",
    [TARGET_OBJECT, CHANGES],
    u"The target and changes that failed to apply."
)


@implementer(_IDiffChange)
class Diff(PClass):
    """
    A ``_IDiffChange`` that is simply the serial application of other diff
    changes.

    This is the object that external modules get and use to apply diffs to
    objects.

    :ivar changes: A vector of ``_IDiffChange`` s that represent a diff between
        two objects.
    """

    changes = pvector_field(object)

    def apply(self, obj):
        proxy = _EvolverProxy(original=obj)
        for c in self.changes:
            if len(c.path) > 0:
                proxy = c.apply(proxy)
            else:
                assert type(c) is _Set
                proxy = _EvolverProxy(original=c.value)
        try:
            return proxy.commit()
        except:
            # Imported here to avoid circular dependencies.
            from ._persistence import wire_encode
            DIFF_COMMIT_ERROR(
                target_object=wire_encode(obj),
                changes=wire_encode(self.changes),
            ).write()
            raise


def _create_diffs_for_sets(current_path, set_a, set_b):
    """
    Computes a series of ``_IDiffChange`` s to turn ``set_a`` into ``set_b``
    assuming that these sets are at ``current_path`` inside a nested pyrsistent
    object.

    :param current_path: An iterable of pyrsistent object describing the path
        inside the root pyrsistent object where the other arguments are
        located.  See ``PMap.transform`` for the format of this sort of path.

    :param set_a: The desired input set.

    :param set_b: The desired output set.

    :returns: An iterable of ``_IDiffChange`` s that will turn ``set_a`` into
        ``set_b``.
    """
    resulting_diffs = pvector([]).evolver()
    for item in set_a.difference(set_b):
        resulting_diffs.append(
            _Remove(path=current_path, item=item)
        )
    for item in set_b.difference(set_a):
        resulting_diffs.append(
            _Add(path=current_path, item=item)
        )
    return resulting_diffs.persistent()


def _create_diffs_for_mappings(current_path, mapping_a, mapping_b):
    """
    Computes a series of ``_IDiffChange`` s to turn ``mapping_a`` into
    ``mapping_b`` assuming that these mappings are at ``current_path`` inside a
    nested pyrsistent object.

    :param current_path: An iterable of pyrsistent object describing the path
        inside the root pyrsistent object where the other arguments are
        located.  See ``PMap.transform`` for the format of this sort of path.

    :param mapping_a: The desired input mapping.

    :param mapping_b: The desired output mapping.

    :returns: An iterable of ``_IDiffChange`` s that will turn ``mapping_a``
        into ``mapping_b``.
    """
    resulting_diffs = pvector([]).evolver()
    a_keys = frozenset(mapping_a.keys())
    b_keys = frozenset(mapping_b.keys())
    for key in a_keys.intersection(b_keys):
        if mapping_a[key] != mapping_b[key]:
            resulting_diffs.extend(
                _create_diffs_for(
                    current_path.append(key),
                    mapping_a[key],
                    mapping_b[key]
                )
            )
    for key in b_keys.difference(a_keys):
        resulting_diffs.append(
            _Set(path=current_path.append(key), value=mapping_b[key])
        )
    for key in a_keys.difference(b_keys):
        resulting_diffs.append(
            _Remove(path=current_path, item=key)
        )
    return resulting_diffs.persistent()


def _create_diffs_for(current_path, subobj_a, subobj_b):
    """
    Computes a series of ``_IDiffChange`` s to turn ``subobj_a`` into
    ``subobj_b`` assuming that these subobjs are at ``current_path`` inside a
    nested pyrsistent object.

    :param current_path: An iterable of pyrsistent object describing the path
        inside the root pyrsistent object where the other arguments are
        located.  See ``PMap.transform`` for the format of this sort of path.

    :param subobj_a: The desired input sub object.

    :param subobj_b: The desired output sub object.

    :returns: An iterable of ``_IDiffChange`` s that will turn ``subobj_a``
        into ``subobj_b``.
    """
    if subobj_a == subobj_b:
        return pvector([])
    elif type(subobj_a) != type(subobj_b):
        return pvector([_Set(path=current_path, value=subobj_b)])
    elif isinstance(subobj_a, PClass) and isinstance(subobj_b, PClass):
        a_dict = subobj_a._to_dict()
        b_dict = subobj_b._to_dict()
        return _create_diffs_for_mappings(current_path, a_dict, b_dict)
    elif isinstance(subobj_a, PMap) and isinstance(subobj_b, PMap):
        return _create_diffs_for_mappings(
            current_path, subobj_a, subobj_b)
    elif isinstance(subobj_a, PSet) and isinstance(subobj_b, PSet):
        return _create_diffs_for_sets(
            current_path, subobj_a, subobj_b)
    # If the objects are not equal, and there is no intelligent way to recurse
    # inside the objects to make a smaller diff, simply set the current path
    # to the object in b.
    return pvector([_Set(path=current_path, value=subobj_b)])


def create_diff(object_a, object_b):
    """
    Constructs a diff from ``object_a`` to ``object_b``

    :param object_a: The desired input object.

    :param object_b: The desired output object.

    :returns:  A ``Diff`` that will convert ``object_a`` into ``object_b``
        when applied.
    """
    changes = _create_diffs_for(pvector([]), object_a, object_b)
    return Diff(changes=changes)


def compose_diffs(iterable_of_diffs):
    """
    Compose multiple ``Diff`` objects into a single diff.

    Assuming you have 3 objects, A, B, and C and you compute diff AB and BC.
    If you pass [AB, BC] into this function it will return AC, a diff that when
    applied to object A, will return C.

    :param iterable_of_diffs: An iterable of diffs to be composed.

    :returns: A new diff such that applying this diff is equivalent to applying
        each of the input diffs in serial.
    """
    return Diff(
        changes=reduce(
            lambda x, y: x.extend(y.changes),
            iterable_of_diffs,
            pvector().evolver()
        ).persistent()
    )


# Ensure that the representation of a ``Diff`` is entirely serializable:
DIFF_SERIALIZABLE_CLASSES = [
    _Set, _Remove, _Add, Diff
]