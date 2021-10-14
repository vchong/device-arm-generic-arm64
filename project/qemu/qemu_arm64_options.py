"""Generate QEMU options for Trusty test framework"""

import subprocess
import tempfile

from qemu_error import RunnerGenericError


class QemuArm64Options(object):

    MACHINE = "virt,secure=on,virtualization=on"

    BASIC_ARGS = [
        "-serial", "tcp:localhost:5552",
        #"-serial", "tcp:localhost:5553", # this is already done in qemu.py
        #"-serial", "tcp:localhost:5554",
        #"-serial", "tcp:localhost:5555",
        "-s", "-S",
        "-nographic", "-cpu", "cortex-a57", "-smp", "4", "-m", "1024", "-d",
        "unimp", "-semihosting-config", "enable,target=native", "-no-acpi",
    ]

    LINUX_ARGS = (
        "earlyprintk console=ttyAMA0,38400 keep_bootcon "
        "loglevel=7 androidboot.selinux=permissive "
        "root=/dev/vda init=/init androidboot.hardware=qemu_trusty")

    def __init__(self, config):
        self.args = []
        self.config = config

    def rpmb_data_path(self):
        return "%s/RPMB_DATA" % self.config.atf

    def rpmb_options(self, sock):
        return [
            "-device", "virtio-serial",
            "-device", "virtserialport,chardev=rpmb0,name=rpmb0",
            "-chardev", "socket,id=rpmb0,path=%s" % sock]

    def gen_dtb(self, args, dtb_tmp_file):
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
        dtb = dtb_tmp_file
        dts_to_dtb_cmd = [dtc, "-q", "-O", "dtb"]
        dts_to_dtb = subprocess.Popen(dts_to_dtb_cmd,
                                      stdin=subprocess.PIPE,
                                      stdout=dtb)
        dts_to_dtb.communicate(dts)
        dts_to_dtb_ret = dts_to_dtb.wait()
        if dts_to_dtb_ret:
            raise RunnerGenericError("dts_to_dtb failed with %d" % dts_to_dtb_ret)
        return ["-dtb", dtb.name]

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

    def machine_options(self):
        return ["-machine", self.MACHINE]

    def basic_options(self):
        return list(self.BASIC_ARGS)

    def bios_options(self):
        return ["-bios", "%s/bl1.bin" % self.config.atf]

    def linux_options(self):
        return [
            "-kernel", "%s/arch/%s/boot/Image" % (self.config.linux,
                                                  self.config.linux_arch),
            "-append", self.LINUX_ARGS
        ]

    def android_trusty_user_data(self):
        return "%s/out/target/product/trusty/data" % self.config.android
