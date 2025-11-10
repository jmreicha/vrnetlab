#!/usr/bin/env python3

import datetime
import logging
import os
import re
import signal
import sys
import time

import vrnetlab
from scrapli import Scrapli


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class ASAv_vm(vrnetlab.VM):
    def __init__(self, username, password, conn_mode, install_mode=False):
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e

        super(ASAv_vm, self).__init__(
            username, password, disk_image=disk_image, ram=2048, cpu="Nehalem", use_scrapli=True
        )
        self.nic_type = "e1000"
        self.conn_mode = conn_mode
        self.install_mode = install_mode
        self.num_nics = 8

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.spins > 300:
            # too many spins with no result ->  give up
            self.stop()
            self.start()
            return

        # Check for login/prompt
        (ridx, match, res) = self.con_expect([b"ciscoasa>"])
        if match:  # got a match!
            if ridx == 0:  # matched prompt
                if self.install_mode:
                    self.logger.debug("Matched, ciscoasa>")
                    self.running = True
                    return

                self.logger.debug("Matched prompt, applying config")

                try:
                    # run main config!
                    self.apply_config()
                    # startup time?
                    startup_time = datetime.datetime.now() - self.start_time
                    self.logger.debug("Startup complete in: %s" % startup_time)
                    # mark as running
                    self.running = True
                    return
                except Exception as e:
                    self.logger.error(f"Failed to apply config: {e}")
                    raise

        # no match, if we saw some output from the device it's probably booting
        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
        # no output, and no match -- increment spin count
        self.spins += 1
        return

    def apply_config(self):
        """Apply the full configuration using Scrapli with proper privilege escalation"""
        self.logger.debug("Applying bootstrap configuration")

        # Handle the initial enable password setup using the base telnet connection
        # This must be done BEFORE commandeering with scrapli
        self.logger.debug("Setting up initial enable password via telnet")
        self.scrapli_tn.channel.write("enable\r")
        time.sleep(1)
        self.scrapli_tn.channel.write(f"{self.password}\r") # Enter Password
        time.sleep(1)
        self.scrapli_tn.channel.write(f"{self.password}\r") # Repeat Password
        time.sleep(2)
        _ = self.scrapli_tn.channel.read()

        self.logger.debug("Entering configuration mode")
        self.scrapli_tn.channel.write("configure terminal\r")
        time.sleep(1)

        self.logger.debug("Handling call-home prompt")
        time.sleep(1)
        self.scrapli_tn.channel.write("N\r")
        time.sleep(1)
        self.scrapli_tn.channel.write("\r")
        time.sleep(1)
        _ = self.scrapli_tn.channel.read()

        # Now that we're in config mode, commandeer with scrapli for better command handling
        scrapli_timeout = os.getenv("SCRAPLI_TIMEOUT", vrnetlab.DEFAULT_SCRAPLI_TIMEOUT)
        asa_scrapli_dev = {
            "platform": "cisco_asa",
            "host": "127.0.0.1",
            "auth_bypass": True,
            "auth_strict_key": False,
            "auth_secondary": self.password,
            "timeout_socket": scrapli_timeout,
            "timeout_transport": scrapli_timeout,
            "timeout_ops": scrapli_timeout,
        }

        con = Scrapli(**asa_scrapli_dev)
        con.commandeer(conn=self.scrapli_tn)

        # Send configuration commands
        config_commands = f"""aaa authentication ssh console LOCAL
aaa authentication enable console LOCAL
username {self.username} password {self.password} privilege 15
interface Management0/0
nameif management
security-level 100
ip address 10.0.0.15 255.255.255.0
no shutdown
exit
route management 0.0.0.0 0.0.0.0 10.0.0.2 1
access-list MGMT_IN extended permit tcp any any eq ssh
access-group MGMT_IN in interface management
crypto key generate ecdsa elliptic-curve 256
ssh key-exchange group dh-group14-sha256
ssh 0.0.0.0 0.0.0.0 management
no ssh stricthostkeycheck
ssh timeout 60"""

        self.logger.debug("Sending configuration commands")
        con.send_configs(config_commands.splitlines())
        self.logger.debug("Saving configuration")
        # Exit to privilege exec mode then save
        con.acquire_priv("privilege_exec")
        con.send_command("write memory")
        self.logger.debug("Closing connection")
        con.close()


class ASAv(vrnetlab.VR):
    def __init__(self, username, password, conn_mode):
        super(ASAv, self).__init__(username, password)
        self.vms = [ASAv_vm(username, password, conn_mode)]


class ASAv_installer(ASAv):
    """ASAv installer"""

    def __init__(self, username, password, conn_mode):
        super(ASAv_installer, self).__init__(username, password, conn_mode)
        self.vms = [ASAv_vm(username, password, conn_mode, install_mode=True)]

    def install(self):
        self.logger.info("Installing ASAv")
        asav = self.vms[0]
        while not asav.running:
            asav.work()
        asav.stop()
        self.logger.info("Installation complete")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--username", default="vrnetlab", help="Username")
    parser.add_argument("--password", default="VR-netlab9", help="Password")
    parser.add_argument("--install", action="store_true", help="Install ASAv")
    parser.add_argument(
        "--connection-mode",
        default="vrxcon",
        help="Connection mode to use in the datapath"
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    if args.install:
        vr = ASAv_installer(args.username, args.password, args.connection_mode)
        vr.install()
    else:
        vr = ASAv(args.username, args.password, args.connection_mode)
        vr.start()
