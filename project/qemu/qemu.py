#!/usr/bin/env python2.7
"""Run Trusty under QEMU in different configurations"""
import argparse
import errno
import fcntl
import json
import os
import qemu_options
import re
import select
import socket
import subprocess
import shutil
import sys
import tempfile
import time
import threading

from qemu_error import AdbFailure, ConfigError, RunnerGenericError, RunnerError, Timeout


# ADB expects its first console on 5554, and control on 5555
ADB_BASE_PORT = 5554


class Config(object):
    """Stores a QEMU configuration for use with the runner

    Attributes:
        android:          Path to a built Android tree or prebuilt.
        linux:            Path to a built Linux kernel tree or prebuilt.
        linux_arch:       Architecture of Linux kernel.
        atf:              Path to the ATF build to use.
        qemu:             Path to the emulator to use.
        arch:             Architecture definition.
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

        script_dir = os.path.dirname(os.path.realpath(__file__))
        self.android = os.path.join(script_dir, config_dict.get("android"))
        self.linux = os.path.join(script_dir, config_dict.get("linux"))
        self.linux_arch = config_dict.get("linux_arch")
        self.atf = os.path.join(script_dir, config_dict.get("atf"))
        self.qemu = os.path.join(script_dir, config_dict.get("qemu", "qemu-system-aarch64"))
        self.rpmbd = os.path.join(script_dir, config_dict.get("rpmbd"))
        self.arch = config_dict.get("arch")
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


class QEMUCommandPipe(object):
    """Communicate with QEMU."""

    def __init__(self):
        """Produces pipes for talking to QEMU and args to enable them."""
        self.command_dir = tempfile.mkdtemp()
        os.mkfifo("%s/com.in" % self.command_dir)
        os.mkfifo("%s/com.out" % self.command_dir)
        self.command_args = [
            "-chardev",
            "pipe,id=command0,path=%s/com" % self.command_dir, "-mon",
            "chardev=command0,mode=control"
        ]
        self.com_pipe_in = None
        self.com_pipe_out = None

    def open(self):
        self.com_pipe_in = open("%s/com.in" % self.command_dir, "w", 0)
        self.com_pipe_out = open("%s/com.out" % self.command_dir, "r", 0)
        self.qmp_command({"execute": "qmp_capabilities"})

    def close(self):
        """Close and clean up command pipes."""

        if self.com_pipe_in:
            self.com_pipe_in.close()
        if self.com_pipe_out:
            self.com_pipe_out.close()

        # Onerror callback function to handle errors when we try to remove
        # command pipe directory, since we sleep one second if QEMU doesn't
        # die immediately, command pipe directory might has been removed
        # already during sleep period.
        def cb_handle_error(func, path, exc_info):
            if not os.access(path, os.F_OK):
                # Command pipe directory already removed, this case is
                # expected, pass this case.
                pass
            else:
                raise RunnerGenericError("Failed to clean up command pipe.")

        # Clean up our command pipe
        shutil.rmtree(self.command_dir, onerror=cb_handle_error)

    def qmp_command(self, qmp_command):
        """Send a qmp command and return result."""

        try:
            json.dump(qmp_command, self.com_pipe_in)
            for line in iter(self.com_pipe_out.readline, ""):
                res = json.loads(line)

                if res.has_key("error"):
                    sys.stderr.write("Command {} failed: {}\n".format(
                        qmp_command, res["error"]))
                    return res

                if res.has_key("return"):
                    return res

                if not res.has_key("QMP") and not res.has_key("event"):
                    # Print unexpected extra lines
                    sys.stderr.write("ignored:" + line)
        except IOError as e:
            print "qmp_command error ignored", e

    def qmp_execute(self, execute, arguments=None):
        """Send a qmp execute command and return result."""
        cmp_command = {"execute": execute}
        if arguments:
            cmp_command["arguments"] = arguments
        return self.qmp_command(cmp_command)

    def monitor_command(self, monitor_command):
        """Send a monitor command and write result to stderr."""

        res = self.qmp_execute("human-monitor-command",
                               {"command-line": monitor_command})
        if res and res.has_key("return"):
            sys.stderr.write(res["return"])


def qemu_handle_error(command_pipe, debug_on_error):
    """Dump registers and/or wait for debugger."""

    sys.stdout.flush()

    sys.stderr.write("QEMU register dump:\n")
    command_pipe.monitor_command("info registers -a")
    sys.stderr.write("\n")

    if debug_on_error:
        command_pipe.monitor_command("gdbserver")
        print "Connect gdb, press enter when done "
        select.select([sys.stdin], [], [])
        raw_input("\n")


def qemu_exit(command_pipe, qemu_proc, has_error, debug_on_error):
    """Ensures QEMU is terminated"""
    unclean_exit = False

    if command_pipe:
        # Ask QEMU to quit
        if qemu_proc and (qemu_proc.poll() is None):
            try:
                if has_error:
                    qemu_handle_error(command_pipe=command_pipe,
                                      debug_on_error=debug_on_error)
                command_pipe.qmp_execute("quit")
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

        command_pipe.close()

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

    def __init__(self,
                 config,
                 boot_tests=None,
                 android_tests=None,
                 interactive=False,
                 verbose=False,
                 rpmb=True,
                 debug=False,
                 debug_on_error=False,
                 timeout=None):
        """Initializes the runner with provided settings.

        See .run() for the meanings of these.
        """
        DEFAULT_TIMEOUT = 60 * 10 # 10 Minutes
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
        self.qemu_arch_options = None
        self.test_timeout = DEFAULT_TIMEOUT if timeout is None else timeout

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

        if self.config.arch == 'arm64' or self.config.arch == 'arm':
            self.qemu_arch_options = qemu_options.QemuArm64Options(self.config)
        elif self.config.arch == 'x86_64':
            self.qemu_arch_options = qemu_options.QemuX86_64Options(self.config)
        else:
            raise ConfigError("Architecture unspecified or unsupported!")

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

    def rpmb_up(self):
        """Brings up the rpmb daemon, returning QEMU args to connect"""
        rpmb_data = self.qemu_arch_options.rpmb_data_path()

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

        return self.qemu_arch_options.rpmb_options(rpmb_sock)

    def rpmb_down(self):
        """Kills the running rpmb daemon, cleaning up its socket directory"""
        if self.rpmb_proc:
            self.rpmb_proc.kill()
            self.rpmb_proc = None
        if self.rpmb_sock_dir:
            shutil.rmtree(self.rpmb_sock_dir)
            self.rpmb_sock_dir = None

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

        # Listen on message socket
        self.msg_sock.listen(1)

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
        """wait for testrunner to connect."""

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
            #print("###### Use -serial tcp:localhost:5552 instead of mon:stdio? #######")
            # NO! Disabling mon:stdio will break adb!
        elif self.verbose:
            # This still leaves stdin connected, but doesn't connect a monitor
            args = ["-serial", "stdio", "-monitor", "none"] + args
        else:
            # Silence debugging output
            args = ["-serial", "null", "-monitor", "none"] + args

        # Create command channel which used to quit QEMU after case execution
        command_pipe = QEMUCommandPipe()
        args += command_pipe.command_args
        cmd = [self.config.qemu] + args

        qemu_proc = subprocess.Popen(cmd, cwd=self.config.atf)

        command_pipe.open()
        self.msg_channel_wait_for_connection()

        def kill_testrunner():
            self.msg_channel_down()
            unclean_exit = qemu_exit(command_pipe, qemu_proc,
                                     has_error=True,
                                     debug_on_error=self.debug_on_error)
            raise Timeout("Wait for boottest to complete", timeout)

        kill_timer = threading.Timer(timeout, kill_testrunner)
        if not self.debug:
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
            unclean_exit = qemu_exit(command_pipe, qemu_proc,
                                     has_error=has_error,
                                     debug_on_error=self.debug_on_error)

        if unclean_exit:
            raise RunnerGenericError("QEMU did not exit cleanly")

        return result

    def adb_bin(self):
        """Returns location of adb"""
        return "%s/out/host/linux-x86/bin/adb" % self.config.android

    def adb(self, args, timeout=60, on_timeout=None, force_output=False):
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
            print "Timed out (%d s)" % timeout
            if on_timeout:
                on_timeout()

            try:
                adb_proc.kill()
            except OSError:
                pass

        if not self.debug:
            kill_timer = threading.Timer(timeout, kill_adb)
            kill_timer.start()
        # Add finally here so that the python interpreter will exit quickly
        # in the event of an exception rather than waiting for the timer
        try:
            exit_code = adb_proc.wait()
            return exit_code
        finally:
            if not self.debug:
                kill_timer.cancel()
                kill_timer.join()

    def check_adb(self, args, **kwargs):
        """As .adb(), but throws an exception if the command fails"""
        code = self.adb(args, **kwargs)
        if code != 0:
            raise AdbFailure(args, code)

    def adb_root(self):
        """Restarts adbd with root permissions and waits until it's back up"""
        MAX_TRIES = 10
        num_tries = 0

        self.check_adb(["root"])

        while True:
            # adbd might not be down by this point yet
            self.adb(["wait-for-device"])

            # Check that adbd is up and running with root permissions
            code = self.adb(["shell",
                             "if [[ $(id -u) -ne 0 ]] ; then exit 1; fi"])
            if code == 0:
                return

            num_tries += 1
            if num_tries >= MAX_TRIES:
                raise AdbFailure(["root"], code)
            time.sleep(1)

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
        self.check_adb(["wait-for-device"], timeout=120)
        self.adb_root()

        # Files put onto the data partition in the Android build will not
        # actually be populated into userdata.img when make dist is used.
        # To work around this, we manually update /data once the device is
        # booted by pushing it the files that would have been there.
        userdata = self.qemu_arch_options.android_trusty_user_data()
        self.check_adb(["push", userdata, "/"])

    def adb_down(self, port):
        """Cleans up after adb connection to adbd on selected port"""
        self.check_adb(["disconnect", "localhost:%d" % port])

        # Wait until QEMU's forward has expired
        CONNECT_MAX_TRIES = 120
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
        args = self.qemu_arch_options.basic_options()
        args += self.qemu_arch_options.bios_options()

        if self.config.linux:
            args += self.qemu_arch_options.linux_options()

        if self.config.android:
            args += self.qemu_arch_options.android_drives_args()

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
        command_pipe = None

        qemu_proc = None
        has_error = False

        # Resource exists in multiple functions, wants to use the same
        # cleanup block regardless
        self.temp_files = []

        try:
            if self.use_rpmb:
                args += self.rpmb_up()

            if self.config.linux:
                args += self.qemu_arch_options.gen_dtb(
                    args,
                    self.get_qemu_arg_temp_file())

            # Prepend the machine since we don't need to edit it as in gen_dtb
            args = self.qemu_arch_options.machine_options() + args

            if self.debug:
                args += ["-s", "-S"]

            # Create socket for communication channel
            args += self.msg_channel_up()

            if self.boot_tests:
                return [self.boottest_run(args, timeout=self.test_timeout)]

            # Logging and terminal monitor
            # Prepend so that it is the *first* serial port and avoid
            # conflicting with rpmb0.
            args = ["-serial", "mon:stdio"] + args
            #print("###### Use -serial tcp:localhost:5552 instead of mon:stdio? #######")
            # NO! Disabling mon:stdio will break adb!

            # If we're noninteractive (e.g. testing) we need a command channel
            # to tell the guest to exit
            if not self.interactive:
                command_pipe = QEMUCommandPipe()
                args += command_pipe.command_args

            # Reserve ADB ports
            ports = alloc_ports()

            # Write expected serial number (as given in adb) to stdout.
            sys.stdout.write('DEVICE_SERIAL: emulator-%d\n' % ports[0])
            sys.stdout.flush()

            # Forward ADB ports in qemu
            args += forward_ports(ports)

            qemu_cmd = [self.config.qemu] + args
            print(qemu_cmd)
            qemu_proc = subprocess.Popen(
                qemu_cmd,
                cwd=self.config.atf,
                stdin=self.stdin,
                stdout=self.stdout,
                stderr=self.stderr)

            if command_pipe:
                command_pipe.open()
            self.msg_channel_wait_for_connection()

            if self.debug:
                print "Run gdb and \"target remote :1234\" to debug"

            try:
                # Send request to boot secondary OS
                self.msg_channel_send_msg("Boot Secondary OS")

                # Bring ADB up talking to the command port
                self.adb_up(ports[1])

                def on_adb_timeout():
                    qemu_handle_error(command_pipe=command_pipe,
                                      debug_on_error=self.debug_on_error)

                # Run android tests
                for android_test in self.android_tests:
                    test_result = self.adb(["shell", android_test],
                                           timeout=self.test_timeout,
                                           on_timeout=on_adb_timeout,
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

            unclean_exit = qemu_exit(command_pipe, qemu_proc,
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
    argument_parser.add_argument("--arch")
    argument_parser.add_argument("--disable-rpmb", action="store_true")
    argument_parser.add_argument("--timeout", type=int)
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
    if args.arch:
        config.arch = args.arch
    if args.extra_qemu_flags:
        config.extra_qemu_flags += args.extra_qemu_flags

    runner = Runner(config, boot_tests=args.boot_test,
                    android_tests=args.shell_command,
                    interactive=not args.headless,
                    verbose=args.verbose,
                    rpmb=not args.disable_rpmb,
                    debug=args.debug,
                    debug_on_error=args.debug_on_error,
                    timeout=args.timeout)

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
