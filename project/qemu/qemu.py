#!/usr/bin/env python
"""Run Trusty under QEMU in different configurations"""
import argparse
import json
import os
import re
import socket
import subprocess
import shutil
import sys
import tempfile
import time
import threading


# ADB expects its first console on 5554, and control on 5555
ADB_BASE_PORT = 5554


class RunnerError(Exception):
    """Contains all kinds of errors .run() will intentionally throw"""


class RunnerGenericError(RunnerError):
    """Generic runner error message"""
    def __init__(self, msg):
        super(RunnerGenericError, self).__init__()
        self.msg = msg

    def __str__(self):
        return "Runner failed: %s" % self.msg


class ConfigError(RunnerError):
    """Invalid configuration"""
    def __init__(self, msg):
        super(ConfigError, self).__init__()
        self.msg = msg

    def __str__(self):
        return "Invalid configuration: %s" % self.msg


class AdbFailure(RunnerError):
    """An adb invocation failed"""

    def __init__(self, adb_args, code):
        super(AdbFailure, self).__init__(self)
        self.adb_args = adb_args
        self.code = code

    def __str__(self):
        return "'adb %s' failed with %d" % (" ".join(self.adb_args), self.code)


class Timeout(RunnerError):
    """A step timed out"""

    def __init__(self, step, timeout):
        super(Timeout, self).__init__(self)
        self.step = step
        self.timeout = timeout

    def __str__(self):
        return "%s timed out (%d s)" % (self.step, self.timeout)


class Config(object):
    """Stores a QEMU configuration for use with the runner

    Attributes:
        android: Path to a built Android tree or prebuilt.
        linux:   Path to a built Linux kernel tree or prebuilt.
        atf:     Path to the ATF build to use.
        qemu:    Path to the emulator to use
    Setting android or linux to None will result in a QEMU which starts
    without those components.
    """

    def __init__(self, config=None):
        """Qemu Configuration

        If config is passed in, it should be a file containing a json
        specification fields described in the docs.
        Unspecified fields will be defaulted.

        If you do not pass in a config, you will almost always need to
        override these values; the default is not especially useful.
        """
        config_dict = {}
        if config:
            config_dict = json.load(config)

        self.android = config_dict.get("android")
        self.linux = config_dict.get("linux")
        self.atf = config_dict.get("atf")
        self.qemu = config_dict.get("qemu", "qemu-system-aarch64")


def alloc_ports():
    """Allocates 2 sequential ports above 5554 for adb"""
    # adb uses ports in pairs
    PORT_WIDTH = 2

    # We can't actually reserve ports atomically for QEMU, but we can at
    # least scan and find two that are not currently in use.
    min_port = ADB_BASE_PORT
    while True:
        alloced_ports = []
        for port in range(min_port, min_port + PORT_WIDTH):
            # If the port is already in use, don't hand it out
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect(("localhost", port))
                break
            except IOError:
                alloced_ports += [port]
        if len(alloced_ports) == PORT_WIDTH:
            return alloced_ports

        # We could increment by only 1, but if we are competing with other
        # adb sessions for ports, this will be more polite
        min_port += PORT_WIDTH


def forward_ports(ports):
    """Generates arguments to forward ports in QEMU on a virtio network"""
    forwards = []
    remap_port = ADB_BASE_PORT
    for port in ports:
        forwards += ["hostfwd=tcp::%d-:%d" % (port, remap_port)]
        remap_port = remap_port + 1
    return [
        "-device", "virtio-net,netdev=adbnet0", "-netdev",
        "user,id=adbnet0,%s" % ",".join(forwards)
    ]


def gen_command_dir():
    """Produces pipes for talking to QEMU and args to enable them"""
    command_dir = tempfile.mkdtemp()
    os.mkfifo("%s/com.in" % command_dir)
    os.mkfifo("%s/com.out" % command_dir)
    command_args = [
        "-chardev",
        "pipe,id=command0,path=%s/com" % command_dir, "-mon",
        "chardev=command0"
    ]
    return command_dir, command_args


def qemu_exit(command_dir, qemu_proc):
    """Ensures QEMU is terminated"""
    unclean_exit = False

    if command_dir:
        # Ask QEMU to quit
        if qemu_proc and (qemu_proc.poll() is None):
            with open("%s/com.in" % command_dir, "w") as com_pipe:
                com_pipe.write("quit\n")
            # If it doesn't die immediately, wait a second
            if qemu_proc.poll() is None:
                time.sleep(1)
                # If it's still not dead, take it out
                if qemu_proc.poll() is None:
                    qemu_proc.kill()
                    print "QEMU refused quit"
                    unclean_exit = True
            qemu_proc.wait()

        # Clean up our command pipe
        shutil.rmtree(command_dir)

    else:
        # This was an interactive run or a boot test
        # QEMU should not be running at this point
        if qemu_proc and (qemu_proc.poll() is None):
            print "QEMU still running with no command channel"
            qemu_proc.kill()
            qemu_proc.wait()
            unclean_exit = True
    return unclean_exit


class Runner(object):
    """Executes tests in QEMU"""

    MACHINE = "virt,secure=on,virtualization=on"

    BASIC_ARGS = [
        "-nographic", "-cpu", "cortex-a57", "-smp", "4", "-m", "1024", "-d",
        "unimp", "-semihosting-config", "enable,target=native", "-no-acpi"
    ]

    LINUX_ARGS = (
        "earlyprintk console=ttyAMA0,38400 keep_bootcon "
        "root=/dev/vda ro init=/init androidboot.hardware=qemu_trusty")

    def __init__(self,
                 config,
                 boot_tests=None,
                 android_tests=None,
                 interactive=False,
                 verbose=False):
        """Initializes the runner with provided settings.

        See .run() for the meanings of these.
        """
        self.config = config
        self.boot_tests = boot_tests if boot_tests else []
        self.android_tests = android_tests if android_tests else []
        self.interactive = interactive
        self.verbose = verbose
        self.adb_transport = None
        self.dtb = None

        # Python 2.7 does not have subprocess.DEVNULL, emulate it
        devnull = open(os.devnull, "r+")
        # If we're not verbose or interactive, squelch command output
        if verbose or self.interactive:
            self.stdout = None
            self.stderr = None
        else:
            self.stdout = devnull
            self.stderr = devnull

        # If we're interactive connect stdin to the user
        if self.interactive:
            self.stdin = None
        else:
            self.stdin = devnull


    def drive_args(self, image, index):
        """Generates arguments for mapping a drive"""
        index_letter = chr(ord('a') + index)
        image_dir = "%s/out/target/product/trusty" % self.config.android
        return [
            "-drive",
            "file=%s/%s.img,index=%d,if=none,id=hd%s,format=raw,snapshot=on" %
            (image_dir, image, index, index_letter), "-device",
            "virtio-blk-device,drive=hd%s" % index_letter
        ]

    def android_drives_args(self):
        """Generates arguments for mapping all default drives"""
        args = []
        # This is order sensitive due to using e.g. root=/dev/vda
        args += self.drive_args("userdata", 2)
        args += self.drive_args("vendor", 1)
        args += self.drive_args("system", 0)
        return args

    def gen_dtb(self, args):
        """Computes a trusty device tree, returning a file for it"""
        with tempfile.NamedTemporaryFile() as dtb_gen:
            dump_dtb_cmd = [
                self.config.qemu, "-machine",
                "%s,dumpdtb=%s" % (self.MACHINE, dtb_gen.name)
            ] + args
            returncode = subprocess.call(dump_dtb_cmd)
            if returncode != 0:
                raise RunnerGenericError("dumping dtb failed with %d" %
                                         returncode)
            dtc = "%s/scripts/dtc/dtc" % self.config.linux
            dtb_to_dts_cmd = [dtc, "-q", "-O", "dts", dtb_gen.name]
            dtb_to_dts = subprocess.Popen(dtb_to_dts_cmd,
                                          stdout=subprocess.PIPE)
            dts = dtb_to_dts.communicate()[0]
            if dtb_to_dts.returncode != 0:
                raise RunnerGenericError("dtb_to_dts failed with %d" %
                                         dtb_to_dts.returncode)

        firmware = "%s/firmware.android.dts" % self.config.atf
        with open(firmware, "r") as firmware_file:
            dts += firmware_file.read()

        # Subprocess closes dtb, so we can't allow it to autodelete
        self.dtb = tempfile.NamedTemporaryFile(delete=False)
        dts_to_dtb_cmd = [dtc, "-q", "-O", "dtb"]
        dts_to_dtb = subprocess.Popen(dts_to_dtb_cmd,
                                      stdin=subprocess.PIPE,
                                      stdout=self.dtb)
        dts_to_dtb.communicate(dts)
        dts_to_dtb_ret = dts_to_dtb.wait()
        if dts_to_dtb_ret:
            raise RunnerError("dts_to_dtb failed with %d" % dts_to_dtb_ret)
        return ["-dtb", self.dtb.name]

    def semihosting_run(self, args):
        """Runs QEMU assuming it will quit with semihosting"""
        args += [
            "-semihosting-config",
            "arg=boottest " + ",".join(self.boot_tests)
        ]

        if self.interactive:
            args += ["-serial", "mon:stdio"]
        elif self.verbose:
            # This still leaves stdin connected, but doesn't connect a monitor
            args += ["-serial", "stdio", "-monitor", "none"]
        else:
            # Silence debugging output
            args += ["-serial", "null", "-monitor", "none"]

        cmd = [self.config.qemu] + args
        # Test output is sent via semihosting, so don't disconnect stdout
        return subprocess.call(
            cmd,
            cwd=self.config.atf,
            stdin=self.stdin)

    def adb_bin(self):
        """Returns location of adb"""
        return "%s/out/host/linux-x86/bin/adb" % self.config.android

    def adb(self, args, timeout=60, force_output=False):
        """Runs an adb command

        If self.adb_transport is set, specializes the command to that
        transport to allow for multiple simultaneous tests.

        Timeout specifies a timeout for the command in seconds.

        If force_output is set true, will send results to stdout and
        stderr regardless of the runner's preferences.
        """
        if self.adb_transport:
            args = ["-t", "%d" % self.adb_transport] + args

        if force_output:
            stdout = None
            stderr = None
        else:
            stdout = self.stdout
            stderr = self.stderr

        adb_proc = subprocess.Popen(
            [self.adb_bin()] + args, stdin=self.stdin, stdout=stdout,
            stderr=stderr)

        # This code simulates the timeout= parameter due to no python 3

        def kill_adb():
            """Kills the running adb"""
            # Technically this races with wait - it is possible, though
            # unlikely, to get a spurious timeout message and kill
            # if .wait() returns, this function is triggered, and then
            # .cancel() runs
            adb_proc.kill()
            print "Timed out (%d s)" % timeout

        kill_timer = threading.Timer(timeout, kill_adb)
        kill_timer.start()
        # Add finally here so that the python interpreter will exit quickly
        # in the event of an exception rather than waiting for the timer
        try:
            exit_code = adb_proc.wait()
            return exit_code
        finally:
            kill_timer.cancel()


    def check_adb(self, args):
        """As .adb(), but throws an exception if the command fails"""
        code = self.adb(args)
        if code != 0:
            raise AdbFailure(args, code)

    def scan_transport(self, port):
        """Given a port and `adb devices -l`, find the transport id"""
        output = subprocess.check_output([self.adb_bin(), "devices", "-l"])
        match = re.search(r"localhost:%d.*transport_id:(\d+)" % port, output)
        if not match:
            print "Failed to find transport for port %d in \n%s" % (port,
                                                                    output)
        self.adb_transport = int(match.group(1))

    def adb_up(self, port):
        """Ensures adb is connected to adbd on the selected port"""
        # Wait until we can connect to the target port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        CONNECT_MAX_TRIES = 15
        connect_tries = 0
        while True:
            try:
                sock.connect(("localhost", port))
                break
            except IOError:
                connect_tries += 1
                if connect_tries >= CONNECT_MAX_TRIES:
                    raise Timeout("Wait for adbd socket", CONNECT_MAX_TRIES)
                time.sleep(1)
        sock.close()
        self.check_adb(["connect", "localhost:%d" % port])
        self.scan_transport(port)
        self.check_adb(["wait-for-device"])
        self.check_adb(["root"])
        self.check_adb(["wait-for-device"])

    def adb_down(self, port):
        """Cleans up after adb connection to adbd on selected port"""
        self.adb_transport = None
        self.check_adb(["disconnect", "localhost:%d" % port])

        # Wait until QEMU's forward has expired
        CONNECT_MAX_TRIES = 15
        connect_tries = 0
        while True:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect(("localhost", port))
                sock.close()
                connect_tries += 1
                if connect_tries >= CONNECT_MAX_TRIES:
                    raise Timeout("Wait for port forward to go away",
                                  CONNECT_MAX_TRIES)
                time.sleep(1)
            except IOError:
                break

    def check_config(self):
        """Checks the runner/qemu config to make sure they are compatible"""
        # If we have any android tests, we need a linux dir and android dir
        if self.android_tests:
            if not self.config.linux:
                raise ConfigError("Need Linux to run android tests")
            if not self.config.android:
                raise ConfigError("Need Android to run android tests")

        # For now, we can't run boot tests and android tests at the same time,
        # because test-runner reports its exit code by terminating the
        # emulator.
        if self.android_tests:
            if self.boot_tests:
                raise ConfigError("Cannot run Android tests and boot"
                                  " tests from same runner")

        # Since boot_tests exit the machine with semihosting, it is not
        # compatible with interactive mode.
        if self.boot_tests:
            if self.interactive:
                raise ConfigError("Cannot run boot tests interactively")

        if self.config.android:
            if not self.config.linux:
                raise ConfigError("Cannot run Android without Linux")

    def universal_args(self):
        """Generates arguments used in all qemu invocations"""
        args = list(self.BASIC_ARGS)
        # Set ATF to be the bios
        args += ["-bios", "%s/bl1.bin" % self.config.atf]

        if self.config.linux:
            args += [
                "-kernel",
                "%s/arch/arm64/boot/Image" % self.config.linux
            ]
            args += ["-append", self.LINUX_ARGS]

        if self.config.android:
            args += self.android_drives_args()

        return args

    def run(self):
        """Launches the QEMU execution.

        Runs boot_tests through test_runner, android_tests through ADB,
        returning aggregated test return codes in a list.

        If interactive is specified, it will leave the user connected
        to the serial console/monitor, and they are responsible for
        terminating execution.

        Returns:
          A list of return codes for the provided tests.
          A negative return code indicates an internal tool failure.

        Limitations:
          Until test_runner is updated, only one of android_tests or boot_tests
          may be provided.
          Similarly, while boot_tests is a list, test_runner only knows how to
          correctly run a single test at a time.
          Again due to test_runner's current state, if boot_tests are
          specified, interactive will be ignored since the machine will
          terminate itself.

          If android_tests is provided, a Linux and Android dir must be
          provided in the config.

          If the adb port range is already in use, port forwarding may fail.
        """
        self.check_config()

        ports = None

        args = self.universal_args()

        test_results = []
        command_dir = None

        qemu_proc = None

        # Resource exists in multiple functions, wants to use the same
        # cleanup block regardless
        self.dtb = None

        try:
            if self.config.linux:
                args += self.gen_dtb(args)

            # Prepend the machine since we don't need to edit it as in gen_dtb
            args = ["-machine", self.MACHINE] + args

            # This codepath should go away when test_runner is changed to
            # not use semihosting exit to report
            if self.boot_tests:
                return [self.semihosting_run(args)]

            # Logging and terminal monitor
            args += ["-serial", "mon:stdio"]

            # If we're noninteractive (e.g. testing) we need a command channel
            # to tell the guest to exit
            if not self.interactive:
                command_dir, command_args = gen_command_dir()
                args += command_args

            # Reserve ADB ports
            ports = alloc_ports()
            # Forward ADB ports in qemu
            args += forward_ports(ports)

            qemu_cmd = [self.config.qemu] + args
            qemu_proc = subprocess.Popen(
                qemu_cmd,
                cwd=self.config.atf,
                stdin=self.stdin,
                stdout=self.stdout,
                stderr=self.stderr)

            # Bring ADB up talking to the command port
            self.adb_up(ports[1])

            # Run android tests
            for android_test in self.android_tests:
                test_result = self.adb(["shell", android_test],
                                       timeout=(60 * 5),
                                       force_output=True)
                test_results.append(test_result)
                if not test_result:
                    break

            if self.interactive:
                # When interactive, the user is responsible for quitting qemu
                qemu_proc.wait()
        finally:
            # Clean up generated device tree
            if self.dtb:
                os.remove(self.dtb.name)

            unclean_exit = qemu_exit(command_dir, qemu_proc)

            if self.adb_transport:
                # Disconnect ADB and wait for our port to be released by qemu
                self.adb_down(ports[1])

            if unclean_exit:
                raise RunnerGenericError("QEMU did not exit cleanly")
        return test_results


def main():
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("-c", "--config", type=file)
    argument_parser.add_argument("--headless", action="store_true")
    argument_parser.add_argument("-v", "--verbose", action="store_true")
    argument_parser.add_argument("--boot-test", action="append")
    argument_parser.add_argument("--shell-command", action="append")
    argument_parser.add_argument("--android")
    argument_parser.add_argument("--linux")
    argument_parser.add_argument("--atf")
    argument_parser.add_argument("--qemu")
    args = argument_parser.parse_args()

    config = Config(args.config)
    if args.android:
        config.android = args.android
    if args.linux:
        config.linux = args.linux
    if args.atf:
        config.atf = args.atf
    if args.qemu:
        config.qemu = args.qemu

    runner = Runner(config, boot_tests=args.boot_test,
                    android_tests=args.shell_command,
                    interactive=not args.headless,
                    verbose=args.verbose)

    try:
        results = runner.run()
        print "Command results: %r" % results

        if any(results):
            sys.exit(1)
        else:
            sys.exit(0)
    except RunnerError as exn:
        print exn
        sys.exit(2)


if __name__ == "__main__":
    main()
