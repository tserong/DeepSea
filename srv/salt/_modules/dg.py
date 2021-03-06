#!/usr/bin/env python
# -*- coding: utf-8 -*-
""" This module will match disks based on applied filter rules

Internally this will be called 'DriveGroups'
"""

from __future__ import absolute_import
import json
import re
import logging
from typing import Set, Tuple

log = logging.getLogger(__name__)

USAGE = """

The (D)rive (G)roup module exists to filter for devices on the node
based on the drive group specification received from the master.

It mainly exposes three functions (They are not ment to be called manually)

All function expect valid drive group specs as arguments.

list_drives:

This returns a dict() of matching drives.

c_v_commands:

Constructs valid ceph-volume calls based on the drive group specs and returns them

deploy:

A simple function that calls c_v_commands and executes it on the minion.


"""


class FilterNotSupported(Exception):
    """ A critical error when the user specified filter is unsupported
    """
    pass


class UnitNotSupported(Exception):
    """ A critical error which encouters when a unit is parsed which
    isn't supported.
    """
    pass


class Filter(object):
    """ Filter class to assign properties to bare filters.

    This is a utility class that tries to simplify working
    with information comming from a textfile/salt (drive_group.yaml)/(pillar)

    """

    def __init__(self, **kwargs):
        self.name: str = str(kwargs.get('name', None))
        self.matcher = kwargs.get('matcher', None)
        self.value: str = str(kwargs.get('value', None))
        self._assign_matchers()
        log.debug("Initializing filter for {} with value {}".format(
            self.name, self.value))

    @property
    def is_matchable(self) -> bool:
        """ A property to indicate if a Filter has a matcher

        Some filter i.e. 'limit' or 'osd_per_device' are valid filter
        attributes but cannot be applied to a disk set. In this case
        we return 'None'
        :return: If a matcher is present True/Flase
        :rtype: bool
        """
        return self.matcher is not None

    def _assign_matchers(self) -> None:
        """ Assign a matcher based on filter_name

        This method assigns an individual Matcher based
        on `self.name` and returns it.
        """
        if self.name == "size":
            self.matcher = SizeMatcher(self.name, self.value)
        elif self.name == "model":
            self.matcher = SubstringMatcher(self.name, self.value)
        elif self.name == "vendor":
            self.matcher = SubstringMatcher(self.name, self.value)
        elif self.name == "rotational":
            self.matcher = EqualityMatcher(self.name, self.value)
        elif self.name == "all":
            self.matcher = AllMatcher(self.name, self.value)
        else:
            log.debug("No suitable matcher for {} could be found.")

    def __repr__(self) -> str:
        """ Visual representation of the filter
        """
        return 'Filter<{}>'.format(self.name)


class Inventory():
    """ The Inventory class

    A container for the inventory call
    This may be extended in the future, depending on our needs.
    """

    def __init__(self):
        log.debug("Inventory init")

    @property
    def raw(self) -> str:
        """ Raw data from a ceph-volume inventory call via salt
        """
        log.debug('Querying ceph-volume inventory')
        return_code, stdout, stderr = __salt__['helper.run'](
            "ceph-volume inventory --format json")
        if return_code == 0:
            return stdout
        log.error('ceph-volume inventory --format json returned with {}'.
                  format(return_code))
        log.error(stderr)
        log.error(stdout)
        return '{}'

    @property
    def disks(self) -> list:
        """ All disks found on the 'target'

        Loads the json data from ceph-volume inventory
        """
        log.debug('Loading disks from inventory')
        return json.loads(self.raw)


# pylint: disable=too-few-public-methods
class Matcher(object):
    """ The base class to all Matchers

    It holds utility methods such as _virtual, _get_disk_key
    and handles the initialization.

    """

    def __init__(self, key: str, value: str) -> None:
        """ Initialization of Base class

        :param str key: Attribute like 'model, size or vendor'
        :param str value: Value of attribute like 'X123, 5G or samsung'
        """
        self.key: str = key
        self.value: str = value
        self.fallback_key: str = ''
        self.virtual: bool = self._virtual()

    # pylint: disable=no-self-use
    def _virtual(self) -> bool:
        """ Detect if any of the hosts is virtual

        In vagrant(libvirt) environments the 'model' flag is not set.
        I assume this is flag is set everywhere else. However:

        This can possibly lead to bugs since all our testing
        runs on virtual environments. This is subject to be
        moved/changed/removed
        """
        virtual: str = __grains__['virtual']
        if virtual != "physical":
            log.debug("I seem to be a VM")
            return True
        return False

    # pylint: disable=inconsistent-return-statements
    def _get_disk_key(self, disk: dict) -> str:
        """ Helper method to safely extract values form the disk dict

        There is a 'key' and a _optional_ 'fallback' key that can be used.
        The reason for this is that the output of ceph-volume is not always
        consistent (due to a bug currently, but you never know).
        There is also a safety measure for a disk_key not existing on
        virtual environments. ceph-volume apparently sources its information
        from udev which seems to not populate certain fields on VMs.

        :param dict disk: A disk representation
        :raises: A generic Exception when no disk_key could be found.
        :return: A disk value
        :rtype: str
        """
        disk_value: str = disk.get(self.key, None)
        if not disk_value and self.fallback_key:
            disk_value = disk.get(self.fallback_key, None)
        if disk_value:
            return disk_value
        if self.virtual:
            log.info(
                "Virtual-env detected. Not raising Exception on missing keys."
                " {} and {} appear not to be present".format(
                    self.key, self.fallback_key))
            return ''
        else:
            raise Exception("No value found for {} or {}".format(
                self.key, self.fallback_key))

    def compare(self, disk: dict):
        """ Implements a valid comparison method for a SubMatcher
        This will get overwritten by the individual classes

        :param dict disk: A disk representation
        """
        pass


# pylint: disable=too-few-public-methods
class SubstringMatcher(Matcher):
    """ Substring matcher subclass
    """

    def __init__(self, key: str, value: str, fallback_key=None) -> None:
        Matcher.__init__(self, key, value)
        self.fallback_key = fallback_key

    def compare(self, disk: dict) -> bool:
        """ Overwritten method to match substrings

        This matcher does substring matching
        :param dict disk: A disk representation (see base for examples)
        :return: True/False if the match succeeded
        :rtype: bool
        """
        if not disk:
            return False
        disk_value: str = self._get_disk_key(disk)
        if str(self.value) in str(disk_value):
            return True
        return False


# pylint: disable=too-few-public-methods
class AllMatcher(Matcher):
    """ All matcher subclass
    """

    def __init__(self, key: str, value: str, fallback_key=None) -> None:
        Matcher.__init__(self, key, value)
        self.fallback_key = fallback_key

    def compare(self, disk: dict) -> bool:
        """ Overwritten method to match all

        A rather dump matcher that just accepts all disks
        (regardless of the value)
        # note:
            Should it be possible to set all: False ?
            I don't think so.. We have limit for that
        :param dict disk: A disk representation (see base for examples)
        :return: always True
        :rtype: bool
        """
        if not disk:
            return False
        return True


# pylint: disable=too-few-public-methods
class EqualityMatcher(Matcher):
    """ Equality matcher subclass
    """

    def __init__(self, key: str, value: str) -> None:
        Matcher.__init__(self, key, value)

    def compare(self, disk: dict) -> bool:
        """ Overwritten method to match equality

        This matcher does value comparison
        :param dict disk: A disk representation
        :return: True/False if the match succeeded
        :rtype: bool
        """
        if not disk:
            return False
        disk_value: str = self._get_disk_key(disk)
        if int(disk_value) == int(self.value):
            return True
        return False


class SizeMatcher(Matcher):
    """ Size matcher subclass
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(self, key: str, value: str) -> None:
        # The 'key' value is overwritten here because
        # the user_defined attribute does not neccessarily
        # correspond to the desired attribute
        # requested from the inventory output
        Matcher.__init__(self, key, value)
        self.key: str = "human_readable_size"
        self.fallback_key: str = "size"
        self._high = None
        self._high_suffix = None
        self._low = None
        self._low_suffix = None
        self._exact = None
        self._exact_suffix = None
        self._parse_filter()

    @property
    def low(self) -> Tuple:
        """ Getter for 'low' matchers
        """
        return self._low, self._low_suffix

    @low.setter
    def low(self, low: Tuple) -> None:
        """ Setter for 'low' matchers
        """
        self._low, self._low_suffix = low

    @property
    def high(self) -> Tuple:
        """ Getter for 'high' matchers
        """
        return self._high, self._high_suffix

    @high.setter
    def high(self, high: Tuple) -> None:
        """ Setter for 'high' matchers
        """
        self._high, self._high_suffix = high

    @property
    def exact(self) -> Tuple:
        """ Getter for 'exact' matchers
        """
        return self._exact, self._exact_suffix

    @exact.setter
    def exact(self, exact: Tuple) -> None:
        """ Setter for 'exact' matchers
        """
        self._exact, self._exact_suffix = exact

    @property
    def supported_suffixes(self) -> list:
        """ Only power of 10 notation is supported
        """
        return ["MB", "GB", "TB", "M", "G", "T"]

    def _normalize_suffix(self, suffix: str) -> str:
        """ Normalize any supported suffix

        Since the Drive Groups are user facing, we simply
        can't make sure that all users type in the requested
        form. That's why we have to internally agree on one format.
        It also checks if any of the supported suffixes was used
        and raises an Exception otherwise.

        :param str suffix: A suffix ('G') or ('M')
        :return: A normalized output
        :rtype: str
        """
        if suffix not in self.supported_suffixes:
            raise UnitNotSupported("Unit '{}' not supported".format(suffix))
        if suffix == "G":
            return "GB"
        if suffix == "T":
            return "TB"
        if suffix == "M":
            return "MB"
        return suffix

    def _parse_suffix(self, obj: str) -> str:
        """ Wrapper method to find and normalize a prefix

        :param str obj: A size filtering string ('10G')
        :return: A normalized unit ('GB')
        :rtype: str
        """
        return self._normalize_suffix(re.findall(r"[a-zA-Z]+", obj)[0])

    def _get_k_v(self, data: str) -> Tuple:
        """ Helper method to extract data from a string

        It uses regex to extract all digits and calls _parse_suffix
        which also uses a regex to extract all letters and normalizes
        the resulting suffix.

        :param str data: A size filtering string ('10G')
        :return: A Tuple with normalized output (10, 'GB')
        :rtype: tuple
        """
        return (re.findall(r"\d+", data)[0], self._parse_suffix(data))

    def _parse_filter(self):
        """ Identifies which type of 'size' filter is applied

        There are four different filtering modes:

        1) 10G:50G (high-low)
           At least 10G but at max 50G of size

        2) :60G
           At max 60G of size

        3) 50G:
           At least 50G of size

        4) 20G
           Exactly 20G in size

        This method uses regex to identify and extract this information
        and raises if none could be found.
        """
        low_high = re.match(r"\d+[A-Z]{1,2}:\d+[A-Z]{1,2}", self.value)
        if low_high:
            low, high = low_high.group().split(":")
            self.low = self._get_k_v(low)
            self.high = self._get_k_v(high)

        low = re.match(r"\d+[A-Z]{1,2}:$", self.value)
        if low:
            self.low = self._get_k_v(low.group())

        high = re.match(r"^:\d+[A-Z]{1,2}", self.value)
        if high:
            self.high = self._get_k_v(high.group())

        exact = re.match(r"^\d+[A-Z]{1,2}$", self.value)
        if exact:
            self.exact = self._get_k_v(exact.group())

        if not self.low and not self.high and not self.exact:
            raise Exception("Couldn't parse {}".format(self.value))

    @staticmethod
    # pylint: disable=inconsistent-return-statements
    def to_byte(tpl: Tuple) -> float:
        """ Convert any supported unit to bytes

        :param tuple tpl: A tuple with ('10', 'GB')
        :return: The converted byte value
        :rtype: float
        """
        value = float(tpl[0])
        suffix = tpl[1]
        if suffix == "MB":
            return value * 1e+6
        elif suffix == "GB":
            return value * 1e+9
        elif suffix == "TB":
            return value * 1e+12
        # checkers force me to return something, although
        # it's not quite good to return something here.. ignore?
        return 0.00

    # pylint: disable=inconsistent-return-statements
    def compare(self, disk: dict) -> bool:
        """ Convert MB/GB/TB down to bytes and compare

        1) Extracts information from the to-be-inspected disk.
        2) Depending on the mode, apply checks and return

        # This doesn't seem very solid and _may_
        be re-factored


        """
        if not disk:
            return False
        disk_value = self._get_disk_key(disk)
        # This doesn't neccessarily have to be a float.
        # The current output from ceph-volume gives a float..
        # This may change in the future..
        # todo: harden this paragraph
        disk_size = float(re.findall(r"\d+\.\d+", disk_value)[0])
        disk_suffix = self._parse_suffix(disk_value)
        disk_size_in_byte = self.to_byte((disk_size, disk_suffix))

        if all(self.high) and all(self.low):
            if disk_size_in_byte <= self.to_byte(
                    self.high) and disk_size_in_byte >= self.to_byte(self.low):
                return True
            # is a else: return False neccessary here?
            # (and in all other branches)
            log.debug("Disk didn't match for 'high/low' filter")

        elif all(self.low) and not all(self.high):
            if disk_size_in_byte >= self.to_byte(self.low):
                return True
            log.debug("Disk didn't match for 'low' filter")

        elif all(self.high) and not all(self.low):
            if disk_size_in_byte <= self.to_byte(self.high):
                return True
            log.debug("Disk didn't match for 'high' filter")

        elif all(self.exact):
            if disk_size_in_byte == self.to_byte(self.exact):
                return True
            log.debug("Disk didn't match for 'exact' filter")
        else:
            log.debug("Neither high, low, nor exact was given")
            raise Exception("No filters applied")
        return False


class DriveGroup(object):
    """ The Drive-Group class

    Targets one node and applies filters on the node's inventory.
    It mainly exposes:

    `data_devices`
    `wal_devices`
    `db_devices`
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(self, filter_args: dict) -> None:
        self.filter_args: dict = filter_args
        self._check_filter_support()
        self._data_devices = None
        self._wal_devices = None
        self._db_devices = None

    @property
    def db_slots(self) -> dict:
        """ Property of db_slots

        db_slots are essentially ratio indicators
        :return: The value of db_slots
        :rtype: dict
        """
        return self.filter_args.get("db_slots", False)

    @property
    def wal_slots(self) -> dict:
        """ Property of wal_slots

        wal_slots are essentially ratio indicators
        """
        return self.filter_args.get("wal_slots", False)

    @property
    def encryption(self) -> dict:
        """ Property of encryption

        True/Flase if encryption is enabled
        """
        return self.filter_args.get("encryption", False)

    @property
    def data_device_attrs(self) -> dict:
        """ Data Device attributes
        """
        return self.filter_args.get("data_devices", dict())

    @property
    def db_device_attrs(self) -> dict:
        """ Db Device attributes
        """
        return self.filter_args.get("db_devices", dict())

    @property
    def wal_device_attrs(self) -> dict:
        """ Wal Device attributes
        """
        return self.filter_args.get("wal_devices", dict())

    @property
    def limit(self) -> int:
        """ Limits the amount of devices assigned

        Limit 0 -> unlimited
        """
        return self.data_device_attrs.get("limit", 0)

    @property
    def inventory(self) -> dict:
        """
        Disks found in the inventory
        """
        return Inventory().disks

    @property
    def data_devices(self) -> list:
        """ Filter for (bluestore) DATA devices
        """
        log.debug("Scanning for data devices")
        return self._filter_devices(self.data_device_attrs)

    @property
    def wal_devices(self) -> list:
        """ Filter for bluestore WAL devices
        """
        log.debug("Scanning for WAL devices")
        return self._filter_devices(self.wal_device_attrs)

    @property
    def db_devices(self) -> list:
        """ Filter for bluestore DB devices
        """
        log.debug("Scanning for db devices")
        return self._filter_devices(self.db_device_attrs)

    def _limit_reached(self, len_devices: int, disk_path: str) -> bool:
        """ Check for the <limit> property and apply logic

        If a limit is set in 'device_attrs' we have to stop adding
        disks at some point.

        If limit is set (>0) and len(devices) >= limit

        :param int len_devices: Length of the already populated device set/list
        :param str disk_path: The disk identifier (for logging purposes)
        :return: True/False if the device should be added to the list of devices
        :rtype: bool
        """
        if self.limit > 0 and len_devices >= self.limit:
            log.info("Refuse to add {} due to limit policy of {}>".format(
                disk_path, self.limit))
            return True
        return False

    def _filter_devices(self, device_filter: dict) -> list:
        """ Filters devices with applied filters

        Iterates over all applied filter (there can be multiple):

        size: 10G:50G
        model: Fujitsu
        rotational: 1

        Question: ##############################
        This currently acts as a OR gate. Should this be a AND gate?
        Question: #############################

        Iterates over all known disk and checks
        for matches by using the matcher subclasses.

        :param dict device_filter: Device filter as in description above
        :return: Set of devices that matched the filter
        :rtype set:
        """
        devices: Set = set()
        for name, val in device_filter.items():
            _filter = Filter(name=name, value=val)
            for disk in self.inventory:
                # continue criterias
                if not _filter.is_matchable:
                    continue

                if not _filter.matcher.compare(self._reduce_inventory(disk)):
                    continue

                if not self._has_mandatory_idents(disk):
                    continue

                if self._limit_reached(len(devices), disk.get('path')):
                    continue

                devices.add(disk.get("path"))

        # sorted() returns a sorted list by the cost of losing the <set>
        return sorted(devices)

    @staticmethod
    def _has_mandatory_idents(disk: dict) -> bool:
        """ Check for mandatory indentification fields
        """
        if disk.get("path", None):
            log.debug("Found matching disk: {}".format(disk.get("path")))
            return True
        else:
            raise Exception(
                "Disk {} doesn't have a 'path' identifier".format(disk))

    @property
    def _supported_filters(self) -> list:
        """ List of supported filters
        """
        return [
            "size", "vendor", "model", "rotational", "limit",
            "osds_per_device", "all"
        ]

    def _check_filter_support(self) -> None:
        """ Iterates over attrs to check support
        """
        for attr in [
                self.data_device_attrs,
                self.wal_device_attrs,
                self.db_device_attrs,
        ]:
            self._check_filter(attr)

    def _check_filter(self, attr: dict) -> None:
        """ Check if the used filters are supported

        :param dict attr: A dict of filters
        :raises: FilterNotSupported if not supported
        :return: None
        """
        for applied_filter in list(attr.keys()):
            if applied_filter not in self._supported_filters:
                raise FilterNotSupported(
                    "Filtering for {} is not supported".format(applied_filter))

    @staticmethod
    # pylint: disable=inconsistent-return-statements
    def _reduce_inventory(disk: dict) -> dict:
        """ Wrapper to validate 'ceph-volume inventory' output
        """
        # Temp disable this check, only for testing purposes
        # maybe this check doesn't need to be here as ceph-volume
        # does this check aswell..
        # This also mostly exists due to:
        # https://github.com/ceph/ceph/pull/25390
        # maybe this can and should be dropped when the fix is public
        if not disk:
            return {}
        if disk["available"] is True:
            try:
                reduced_disk = {"path": disk.get("path")}

                reduced_disk["size"] = disk.get("sys_api", {}).get(
                    "human_readable_size", "")
                reduced_disk["vendor"] = disk.get("sys_api", {}).get(
                    "vendor", "")
                reduced_disk["bare_size"] = disk.get("sys_api", {}).get(
                    "size", "")
                reduced_disk["model"] = disk.get("sys_api", {}).get(
                    "model", "")
                reduced_disk["rotational"] = disk.get("sys_api", {}).get(
                    "rotational", "")

                return reduced_disk
            except KeyError("Could not retrieve mandatory key from disk spec"):
                raise


def list_drives(**kwargs):
    """
    A public method that returns a dict
    of matching disks
    """
    filter_args = kwargs.get('filter_args', dict())
    if not filter_args:
        Exception("No filter_args provided")
    dgo = DriveGroup(filter_args)
    return dict(
        data_devices=dgo.data_devices,
        wal_devices=dgo.wal_devices,
        db_devices=dgo.db_devices)


def c_v_commands(**kwargs):
    """
    Construct the ceph-volume command based on the
    matching disks
    """
    filter_args = kwargs.get('filter_args', dict())
    if not filter_args:
        Exception("No filter_args provided")
    dgo = DriveGroup(filter_args)
    log.debug("Received call for ceph-volume command generation")
    log.error(kwargs)
    if not dgo.data_devices:
        return ""
    cmd = "ceph-volume lvm batch {}".format(' '.join(dgo.data_devices))

    # Compute difference between two lists

    # This validation should belong to ceph-volume
    # and will evetually end up there:
    # trackerbug: http://tracker.ceph.com/issues/38473

    extra_db_devices = [
        x for x in set(dgo.db_devices) if x not in set(dgo.wal_devices)
    ]

    if dgo.wal_devices and dgo.db_devices:
        cmd += " --wal-devices {}".format(' '.join(dgo.wal_devices))
        if extra_db_devices:
            log.info(
                "wal and db are same except for {}".format(extra_db_devices))
            cmd += "--db-devices {}".format(' '.join(extra_db_devices))
    elif dgo.wal_devices and not dgo.db_devices:
        cmd += " --wal-devices {}".format(' '.join(dgo.wal_devices))
    elif dgo.db_devices and not dgo.wal_devices:
        cmd += " --db-devices {}".format(' '.join(dgo.db_devices))
    else:
        log.info("Neither wal nor db devices are specified")
    if kwargs.get('dry_run', False):
        cmd += " --report"
    else:
        cmd += " --yes"
    return cmd


def deploy(**kwargs):
    """ Execute the generated ceph-volume commands """
    return __salt__['helper.run'](c_v_commands(**kwargs))


def _help():
    """ Help/Usage class
    """
    print(USAGE)


__func_alias__ = {
    'help_': 'help',
}
