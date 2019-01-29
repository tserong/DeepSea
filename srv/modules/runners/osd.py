"""
Runner to remove and replace osds
"""

from __future__ import absolute_import
from __future__ import print_function
import logging
# pylint: disable=import-error,3rd-party-module-not-gated,redefined-builtin
import salt.client
import salt.runner
import json

log = logging.getLogger(__name__)


def help_():
    """
    Usage
    """
    usage = ('salt-run remove.osd id [id ...][force=True]:\n\n'
             '    Removes an OSD\n'
             '\n\n')
    print(usage)
    return ""


"""
Notes during implementation:

- Runner naming:

Rather than remove.osd and replace.osds

there should be a osd runner which can then
be called with
`osd.replace`
and
`osd.remove`

Iirc the initial thought behind swapping names is to
avoid mixing the namespaces between modules and runners..

Since they are invoked differently, I don't see a huge problem with this any longer.
Maybe there should be a different name though.. disks.replace, disks.remove, disks.generate

The reason for this remark is that remove and replace share a lot of common code.
In a python world this would not be a huge problem due to the import system. In salt
however, importing is different. Testing and structuring gets hard and cumbersome..


- Potential improvements on ceph-volume

ceph-volume lvm zap --osd-id {id} --destroy

removes all traces but the partition. I *think* this is the intended behavior.
In the past we always removed all traces of the OSD while removing(including the partition)

ceph-volume lvm zap {DISK}

removes also the partition.

so instead of going the extra mile and deduce the disk from the osd_id there could be an
additional flag to do that -> '--wipe-partition'

- command returns:

They suck -> {'host': 'message..'}. No returncode, no nothing.

- ceph.conf

It may be that a user has overwritten default values in the ceph.conf.
We may want to check for that.. (only in /srv/salt/ceph/configuration/files.. etc?)
"""


class OSDNotFound(Exception):
    pass


class OSDUnknownState(Exception):
    pass


class OSDUtil(object):
    def __init__(self, osd_id, **kwargs):
        self.osd_id = osd_id
        self.local = salt.client.LocalClient()
        self.master_minion = self._master_minion()
        self.host_osds = self._host_osds()
        self.host = self._find_host()
        self.osd_state = self._get_osd_state()
        # timeout, delay?
        self.force = kwargs.get('force', False)

    def replace(self):
        """
        1) ceph osd out $id
        2) systemctl stop ceph-osd@$id (maybe do more see osd.py (terminate()))
        2.1) also maybe wait if not force
        3) ceph osd destroy $id --yes-i-really-mean-it
        4) save id somehow -> to track position in crushmap
        (5) ceph-volume lvm zap --osd-id $id (maybe also with /dev/sdx))
        6) This time we will try to compute the destroyed osds and re-assign them
        6.1) This can work with
             `ceph osd tree destroyed --format json`
             This can be used to detect if a replace operation is pending.
             Just check for a match before deploying osds and if matched,
             call c-v with --osd-id $id
        """
        pass

    def remove(self):
        """
        1) Call 'ceph osd out $id'
        2) Call systemctl stop ceph-osd@$id (on minion)
        3) ceph osd purge $id --yes-i-really-mean-it
        3.1) (slide-in) remove the grain from the 'ceph:' struct
        4) call ceph-volume lvm zap --osd-id $id --destroy (on minion)
        5) zap doesn't destroy the partition -> find the associated partitions and
           call `ceph-volume lvm zap <device>` (optional)
        """

        # Has to be repeated 2-3 times to succeed currently
        # UPDATE: That no longer holds true

        if not self.host:
            log.error("No host found")
            return False

        try:
            self._mark_osd('out')
        except OSDNotFound:
            return False
        except OSDUnknownState:
            log.info("Attempting rollback to previous state")
            self.recover_osd_state()
            return False

        try:
            self._service('disable')
            self._service('stop')
            # The original implementation
            # pkilled (& -9 -f'd ) the osd-process
            # including a double check with pgrep
            # considering to add that again
        except Exception:
            log.error("Encoutered issue while operating on systemd service")
            log.warning("Attempting rollback to previous state")
            self.recover_osd_state()
            self._service('enable')
            self._service('start')
            return False

        if not self.force:
            # do not empty the OSD
            try:
                self._empty_osd()
            except Exception:
                log.error("Encoutered issue while purging osd")
                log.info("Attempting rollback to previous state")
                self.recover_osd_state()
                self._service('enable')
                self._service('start')
                return False

        try:
            self._purge_osd()
        except Exception:
            log.error("Encoutered issue while purging osd")
            log.info("Attempting rollback to previous state")
            self.recover_osd_state()
            self._service('enable')
            self._service('start')
            return False

        try:
            self._delete_grain()
        except Exception:
            log.error("Encoutered issue while zapping the disk")
            log.info("No rollback possible at this stage")
            return False

        try:
            self._lvm_zap()
        except Exception:
            log.error("Encoutered issue while zapping the disk")
            log.info("No rollback possible at this stage")
            return False

        return True

    def _delete_grain(self):
        log.info("Deleting grain for osd {}".format(self.osd_id))
        self.local.cmd(
            self.host, 'osd.delete_grain', [self.osd_id], tgt_type="glob")
        # There is no handling of returncodes in delete_grain whatsoever..
        # No point in checking messages
        return True

    def _empty_osd(self):
        log.info("Emptying osd {}".format(self.osd_id))
        ret = self.local.cmd(
            self.master_minion,
            'osd.empty',
            [self.osd_id],  # **kwargs
            tgt_type="glob")
        message = list(ret.values())[0]
        while message.startswith("Timeout"):
            print("  {}\nRetrying...".format(message))
        if message.startswith("osd.{} is safe to destroy".format(self.osd_id)):
            print(message)
        return True

    def recover_osd_state(self):
        if self.osd_state:
            if self.osd_state.get('_in', False):
                self._mark_osd('in')
            if self.osd_state.get('down', False):
                self._mark_osd('down')
            if self.osd_state.get('out', False):
                self._mark_osd('out')

    def _get_osd_state(self):
        """ Returns dict of up, in, down """
        cmd = 'ceph osd dump --format=json'
        log.info("Executing: {}".format(cmd))
        ret = self.local.cmd(
            self.master_minion, "cmd.run", [cmd], tgt_type="glob")
        message = list(ret.values())[0]
        message_json = json.loads(message)
        all_osds = message_json.get('osds', [])
        try:
            osd_info = [
                x for x in all_osds if x.get('osd', '') == self.osd_id
            ][0]
        except IndexError:
            raise OSDNotFound("OSD {} doesn't exist in the cluster".format(
                self.osd_id))
        return dict(
            up=bool(osd_info.get('up', 0)),
            _in=bool(osd_info.get('in', 0)),
            down=bool(osd_info.get('down', 0)))

    def _lvm_zap(self):
        cmd = 'ceph-volume lvm zap --osd-id {} --destroy'.format(self.osd_id)
        log.info("Executing: {}".format(cmd))
        ret = self.local.cmd(self.host, "cmd.run", [cmd], tgt_type="glob")
        message = list(ret.values())[0]
        if 'Zapping successful for OSD' not in message:
            log.error("Zapping the osd failed: {}".format(message))
            raise Exception
        return True

    def _purge_osd(self):
        cmd = "ceph osd purge {} --yes-i-really-mean-it".format(self.osd_id)
        log.info("Executing: {}".format(cmd))
        ret = self.local.cmd(
            self.master_minion,
            "cmd.run",
            [cmd],
            tgt_type="glob",
        )
        message = list(ret.values())[0]
        if not message.startswith('purged osd'):
            log.error("Purging the osd failed: {}".format(message))
            raise Exception
        return True

    def _service(self, action):
        log.info("Calling service.{} on {}".format(action, self.osd_id))
        ret = self.local.cmd(
            self.host,
            'service.stop', ['ceph-osd@{}'.format(self.osd_id)],
            tgt_type="glob")
        message = list(ret.values())[0]
        if not message:
            log.error("Stopping the systemd service resulted with {}".format(
                message))
            raise Exception
        return True

    def _mark_osd(self, state):
        cmd = "ceph osd {} {}".format(state, self.osd_id)
        log.info("Running command {}".format(cmd))
        ret = self.local.cmd(
            self.master_minion,
            "cmd.run",
            [cmd],
            tgt_type="glob",
        )
        message = list(ret.values())[0]
        if message.startswith('marked'):
            log.info("Marking osd {} - {} -".format(self.osd_id, state))
        elif message.startswith('osd.{} is already {}'.format(
                self.osd_id, state)):
            log.info(message)
        elif message.startswith('osd.{} does not exist'.format(self.osd_id)):
            log.error(message)
            raise OSDNotFound
        else:
            log.warning("OSD not in expected state. {}".format(message))
            raise OSDUnknownState
        return True

    def _find_host(self):
        """
        Search lists for ID, return host
        """
        for host in self.host_osds:
            if str(self.osd_id) in self.host_osds.get(host):
                return host
        return ""

    def _host_osds(self):
        """
        osd.list is a mix of 'mounts' and 'grains'
        in the future this should come from ceph-volume inventory
        - check
        """
        return self.local.cmd(
            "I@roles:storage", "osd.list", tgt_type="compound")
        #                       subject to change

    def _master_minion(self):
        """
        Load the master modules
        """
        __master_opts__ = salt.config.client_config("/etc/salt/master")
        __master_utils__ = salt.loader.utils(__master_opts__)
        __salt_master__ = salt.loader.minion_mods(
            __master_opts__, utils=__master_utils__)

        return __salt_master__["master.minion"]()


def remove(*args, **kwargs):
    results = dict()
    for osd_id in args:
        print("Removing osd {}".format(osd_id))
        rc = OSDUtil(osd_id, **kwargs).remove()
        results.update({osd_id: rc})
    return results


def replace(*args, **kwargs):
    for osd_id in args:
        print("Replacing osd {}".format(osd_id))
        OSDUtil(osd_id, **kwargs).replace()


__func_alias__ = {
    'help_': 'help',
}
