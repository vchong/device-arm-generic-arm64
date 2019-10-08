#!/usr/bin/env python
"""Run Trusty under QEMU in different configurations"""
import argparse
import errno
import fcntl
import json
import os
import re
import select
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
        android:          Path to a built Android tree or prebuilt.
        linux:            Path to a built Linux kernel tree or prebuilt.
        atf:              Path to the ATF build to use.
        qemu:             Path to the emulator to use
        rpmbd:            Path to the rpmb daemon to use.
        extra_qemu_flags: Extra flags to pass to QEMU.
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
        self.rpmbd = config_dict.get("rpmbd")
        self.extra_qemu_flags = config_dict.get("extra_qemu_flags", [])


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


def qemu_exit(command_dir, qemu_proc, has_error, debug_on_error):
    """Ensures QEMU is terminated"""
    unclean_exit = False

    if command_dir:
        # Ask QEMU to quit
        if qemu_proc and (qemu_proc.poll() is None):
            # Open O_NONBLOCK to deal with a potential race between
            # qemu_proc.poll() and the open call. The poll() is purely
            # advisory now.
            try:
                com_pipe = os.open("%s/com.in" % command_dir,
                                   os.O_NONBLOCK | os.O_WRONLY)
                if has_error:
                    os.write(com_pipe, "info registers -a\n")
                    if debug_on_error:
                        os.write(com_pipe, "gdbserver\n")
                        try:
                            raw_input("Connect gdb, press enter when done ")
                        except:
                            pass
                os.write(com_pipe, "quit\n")
                if has_error:
                    sys.stdout.flush()
                    sys.stderr.write("QEMU exit register dump:\n")
                    with open("%s/com.out" % command_dir, "r") as com_pipe_out:
                        for line in com_pipe_out:
                            if not (line.startswith("QEMU") or
                                    line.startswith("(qemu)")):
                                sys.stderr.write(line)
                os.close(com_pipe)
            except OSError:
                pass

            # If it doesn't die immediately, wait a second
            if qemu_proc.poll() is None:
                time.sleep(1)
                # If it's still not dead, take it out
                if qemu_proc.poll() is None:
                    qemu_proc.kill()
                    print "QEMU refused quit"
                    unclean_exit = True
            qemu_proc.wait()

        # Onerror callback function to handle errors when we try to remove
        # command pipe directory, since we sleep one second if QEMU doesn't
        # die immediately, command pipe directory might has been removed
        # already during sleep period.
        def handleError(func, path, exc_info):
            if not os.access(path, os.F_OK):
                # Command pipe directory already removed, this case is
                # expected, pass this case.
                pass
            else:
                raise RunnerGenericError("Failed to clean up command pipe.")

        # Clean up our command pipe
        shutil.rmtree(command_dir, onerror=handleError)

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
        "unimp", "-semihosting-config", "enable,target=native", "-no-acpi",
        "-device", "virtio-serial",
    ]

    LINUX_ARGS = (
        "earlyprintk console=ttyAMA0,38400 keep_bootcon "
        "root=/dev/vda ro init=/init androidboot.hardware=qemu_trusty")

    def __init__(self,
                 config,
                 boot_tests=None,
                 android_tests=None,
                 interactive=False,
                 verbose=False,
                 rpmb=True,
                 debug=False,
                 debug_on_error=False):
        """Initializes the runner with provided settings.

        See .run() for the meanings of these.
        """
        self.config = config
        self.boot_tests = boot_tests if boot_tests else []
        self.android_tests = android_tests if android_tests else []
        self.interactive = interactive
        self.debug = debug
        self.verbose = verbose
        self.adb_transport = None
        self.temp_files = []
        self.use_rpmb = rpmb
        self.rpmb_proc = None
        self.rpmb_sock_dir = None
        self.msg_sock_conn = None
        self.msg_sock_dir = None
        self.debug_on_error = debug_on_error
        self.dump_stdout_on_error = False

        # Python 2.7 does not have subprocess.DEVNULL, emulate it
        devnull = open(os.devnull, "r+")
        # If we're not verbose or interactive, squelch command output
        if verbose or self.interactive:
            self.stdout = None
            self.stderr = None
        else:
            self.stdout = tempfile.TemporaryFile()
            self.stderr = subprocess.STDOUT
            self.dump_stdout_on_error = True

        # If we're interactive connect stdin to the user
        if self.interactive:
            self.stdin = None
        else:
            self.stdin = devnull

        if self.boot_tests and self.debug:
            print """\
Warning: Test selection does not work when --debug is set.
To run a test in test runner, run in GDB:

target remote :1234
break host_get_cmdline
c
next 6
set cmdline="boottest your.port.here"
set cmdline_len=sizeof("boottest your.port.here")-1
c
"""

    def error_dump_output(self):
        if self.dump_stdout_on_error:
            sys.stdout.flush()
            sys.stderr.write("System log:\n")
            self.stdout.seek(0)
            sys.stderr.write(self.stdout.read())

    def get_qemu_arg_temp_file(self):
        """Returns a temp file that will be deleted after qemu exits."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        self.temp_files.append(tmp.name)
        return tmp

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

    def rpmb_up(self):
        """Brings up the rpmb daemon, returning QEMU args to connect"""
        rpmb_data = "%s/RPMB_DATA" % self.config.atf
        self.rpmb_sock_dir = tempfile.mkdtemp()
        rpmb_sock = "%s/rpmb" % self.rpmb_sock_dir
        rpmb_proc = subprocess.Popen([self.config.rpmbd,
                                      "-d", rpmb_data,
                                      "--sock", rpmb_sock])
        self.rpmb_proc = rpmb_proc

        # Wait for RPMB socket to appear to avoid a race with QEMU
        test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        tries = 0
        max_tries = 10
        while True:
            tries += 1
            try:
                test_sock.connect(rpmb_sock)
                break
            except socket.error as exn:
                if tries >= max_tries:
                    raise exn
                time.sleep(1)

        return ["-device", "virtserialport,chardev=rpmb0,name=rpmb0",
                "-chardev", "socket,id=rpmb0,path=%s" % rpmb_sock]

    def rpmb_down(self):
        """Kills the running rpmb daemon, cleaning up its socket directory"""
        if self.rpmb_proc:
            self.rpmb_proc.kill()
            self.rpmb_proc = None
        if self.rpmb_sock_dir:
            shutil.rmtree(self.rpmb_sock_dir)
            self.rpmb_sock_dir = None

    def gen_dtb(self, args):
        """Computes a trusty device tree, returning a file for it"""
        with tempfile.NamedTemporaryFile() as dtb_gen:
            dump_dtb_cmd = [
                self.config.qemu, "-machine",
                "%s,dumpdtb=%s" % (self.MACHINE, dtb_gen.name)
            ] + [arg for arg in args if arg != "-S"]
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
        dtb = self.get_qemu_arg_temp_file()
        dts_to_dtb_cmd = [dtc, "-q", "-O", "dtb"]
        dts_to_dtb = subprocess.Popen(dts_to_dtb_cmd,
                                      stdin=subprocess.PIPE,
                                      stdout=dtb)
        dts_to_dtb.communicate(dts)
        dts_to_dtb_ret = dts_to_dtb.wait()
        if dts_to_dtb_ret:
            raise RunnerError("dts_to_dtb failed with %d" % dts_to_dtb_ret)
        return ["-dtb", dtb.name]

    def msg_channel_up(self):
        """Create message channel between host and QEMU guest

        Virtual serial console port 'testrunner0' is introduced as socket
        communication channel for QEMU guest and current process. Testrunner
        enumerates this port, reads test case which to be executed from
        testrunner0 port, sends output log message and test result to
        testrunner0 port.
        """

        self.msg_sock_dir = tempfile.mkdtemp()
        msg_sock_file = "%s/msg" % self.msg_sock_dir
        self.msg_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.msg_sock.bind(msg_sock_file)
        return ["-device",
                "virtserialport,chardev=testrunner0,name=testrunner0",
                "-chardev", "socket,id=testrunner0,path=%s" % msg_sock_file]

    def msg_channel_down(self):
        if self.msg_sock_conn:
            self.msg_sock_conn.close()
            self.msg_sock_conn = None
        if self.msg_sock_dir:
            shutil.rmtree(self.msg_sock_dir)
            self.msg_sock_dir = None

    def msg_channel_wait_for_connection(self):
        # Listen on message socket, wait for testrunner to connect
        self.msg_sock.listen(1)

        # Accept testrunner's connection request
        self.msg_sock_conn, _ = self.msg_sock.accept()

    def msg_channel_send_msg(self, msg):
        """Send message to testrunner via testrunner0 port

        Testrunner tries to connect port while message with following format
        "boottest your.port.here". Currently, we utilize this format to execute
        cases in boot test.
        If message does not comply above format, testrunner starts to launch
        secondary OS.

        """
        if self.msg_sock_conn:
            self.msg_sock_conn.send(msg)
        else:
            sys.stderr.write("Connection has not been established yet!")

    def msg_channel_recv(self):
        return self.msg_sock_conn.recv(64)

    def msg_channel_close(self):
        if self.msg_sock_conn:
            self.msg_sock_conn.close()

    def boottest_run(self, args, timeout=(60 * 2)):
        """Run boot test cases"""

        has_error = False
        result = 2

        if self.interactive:
            args = ["-serial", "mon:stdio"] + args
        elif self.verbose:
            # This still leaves stdin connected, but doesn't connect a monitor
            args = ["-serial", "stdio", "-monitor", "none"] + args
        else:
            # Silence debugging output
            args = ["-serial", "null", "-monitor", "none"] + args

        # Create command channel which used to quit QEMU after case execution
        command_dir, command_args = gen_command_dir()
        args += command_args
        cmd = [self.config.qemu] + args

        qemu_proc = subprocess.Popen(cmd, cwd=self.config.atf)

        self.msg_channel_wait_for_connection()

        def kill_testrunner():
            self.msg_channel_down()
            unclean_exit = qemu_exit(command_dir, qemu_proc,
                                     has_error=True,
                                     debug_on_error=self.debug_on_error)
            raise Timeout("Wait for boottest to complete", timeout)

        kill_timer = threading.Timer(timeout, kill_testrunner)
        kill_timer.start()

        testcase = "boottest " + "".join(self.boot_tests)
        try:
            self.msg_channel_send_msg(testcase)

            while True:
                ret = self.msg_channel_recv()

                # If connection is disconnected accidently by peer, for
                # instance child QEMU process crashed, a message with length
                # 0 would be received. We should drop this message, and
                # indicate test framework that something abnormal happened.
                if not len(ret):
                    has_error = True
                    break

                # Print message to STDOUT. Since we might meet EAGAIN IOError
                # when writting to STDOUT, use try except loop to catch EAGAIN
                # and waiting STDOUT to be available, then try to write again.
                def print_msg(msg):
                    while True:
                        try:
                            sys.stdout.write(msg)
                            break
                        except IOError as e:
                            if e.errno != errno.EAGAIN:
                                RunnerGenericError("Failed to print message")
                            select.select([], [sys.stdout], [])

                # Please align message structure definition in testrunner.
                if ord(ret[0]) == 0:
                    print_msg(ret[2 : 2 + ord(ret[1])])
                elif ord(ret[0]) == 1:
                    result = ord(ret[1])
                    break
                else:
                    # Unexpected type, return test result:TEST_FAILED
                    has_error = True
                    result = 1
                    break
        except:
            raise
        finally:
            kill_timer.cancel()
            self.msg_channel_down()
            unclean_exit = qemu_exit(command_dir, qemu_proc,
                                     has_error=has_error,
                                     debug_on_error=self.debug_on_error)

        if unclean_exit:
            raise RunnerGenericError("QEMU did not exit cleanly")

        return result

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

    def scan_transport(self, port, expect_none=False):
        """Given a port and `adb devices -l`, find the transport id"""
        output = subprocess.check_output([self.adb_bin(), "devices", "-l"])
        match = re.search(r"localhost:%d.*transport_id:(\d+)" % port, output)
        if not match:
            if expect_none:
                self.adb_transport = None
                return
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

        # Files put onto the data partition in the Android build will not
        # actually be populated into userdata.img when make dist is used.
        # To work around this, we manually update /data once the device is
        # booted by pushing it the files that would have been there.
        userdata = "%s/out/target/product/trusty/data" % self.config.android
        self.check_adb(["push", userdata, "/"])

    def adb_down(self, port):
        """Cleans up after adb connection to adbd on selected port"""
        self.check_adb(["disconnect", "localhost:%d" % port])

        # Wait until QEMU's forward has expired
        CONNECT_MAX_TRIES = 15
        connect_tries = 0
        while True:
            try:
                self.scan_transport(port, expect_none=True)
                if not self.adb_transport:
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

        # Since boot test utilizes virtio serial console port for communication
        # between QEMU guest and current process, it is not compatible with
        # interactive mode.
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

        # Append configured extra flags
        args += self.config.extra_qemu_flags

        return args

    def run(self):
        """Launches the QEMU execution.

        Runs boot_tests through test_runner, android_tests through ADB,
        returning aggregated test return codes in a list.

        If interactive is specified, it will leave the user connected
        to the serial console/monitor, and they are responsible for
        terminating execution.

        If debug is on, the main QEMU instance will be launched with -S and
        -s, which pause the CPU rather than booting, and starts a gdb server
        on port 1234 respectively.

        Note that if the boot_tests is specified, that argument will not be
        correctly read because semihosting-config does not work under the
        debugger.

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
        has_error = False

        # Resource exists in multiple functions, wants to use the same
        # cleanup block regardless
        self.temp_files = []

        try:
            if self.use_rpmb:
                args += self.rpmb_up()

            if self.config.linux:
                args += self.gen_dtb(args)

            # Prepend the machine since we don't need to edit it as in gen_dtb
            args = ["-machine", self.MACHINE] + args

            if self.debug:
                args += ["-s", "-S"]

            # Create socket for communication channel
            args += self.msg_channel_up()

            if self.boot_tests:
                return [self.boottest_run(args)]

            # Logging and terminal monitor
            # Prepend so that it is the *first* serial port and avoid
            # conflicting with rpmb0.
            args = ["-serial", "mon:stdio"] + args

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

            self.msg_channel_wait_for_connection()

            if self.debug:
                print "Run gdb and \"target remote :1234\" to debug"

            try:
                # Send request to boot secondary OS
                self.msg_channel_send_msg("Boot Secondary OS")

                # Bring ADB up talking to the command port
                self.adb_up(ports[1])

                # Run android tests
                for android_test in self.android_tests:
                    test_result = self.adb(["shell", android_test],
                                           timeout=(60 * 10),
                                           force_output=True)
                    test_results.append(test_result)
                    if test_result:
                        has_error = True
                        break
            # Finally is used here to ensure that ADB failures do not take away
            # the user's serial console in interactive mode.
            finally:
                if self.interactive:
                    # The user is responsible for quitting QEMU
                    qemu_proc.wait()
        except:
            has_error = True
            raise
        finally:
            # Clean up generated device tree
            for temp_file in self.temp_files:
                os.remove(temp_file)

            if has_error:
                self.error_dump_output()

            unclean_exit = qemu_exit(command_dir, qemu_proc,
                                     has_error=has_error,
                                     debug_on_error=self.debug_on_error)

            fcntl.fcntl(0, fcntl.F_SETFL,
                        fcntl.fcntl(0, fcntl.F_GETFL) & ~os.O_NONBLOCK)

            self.rpmb_down()

            self.msg_channel_down()

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
    argument_parser.add_argument("--debug", action="store_true")
    argument_parser.add_argument("--debug-on-error", action="store_true")
    argument_parser.add_argument("--boot-test", action="append")
    argument_parser.add_argument("--shell-command", action="append")
    argument_parser.add_argument("--android")
    argument_parser.add_argument("--linux")
    argument_parser.add_argument("--atf")
    argument_parser.add_argument("--qemu")
    argument_parser.add_argument("--disable-rpmb", action="store_true")
    argument_parser.add_argument("extra_qemu_flags", nargs="*")
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
    if args.extra_qemu_flags:
        config.extra_qemu_flags += args.extra_qemu_flags

    runner = Runner(config, boot_tests=args.boot_test,
                    android_tests=args.shell_command,
                    interactive=not args.headless,
                    verbose=args.verbose,
                    rpmb=not args.disable_rpmb,
                    debug=args.debug,
                    debug_on_error=args.debug_on_error)

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
