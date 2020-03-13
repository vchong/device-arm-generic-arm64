# Copyright (C) 2018 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

PROJECT_QEMU_INC_LOCAL_DIR := $(GET_LOCAL_DIR)

include project/$(QEMU_TRUSTY_PROJECT).mk

MODULES += \
	trusty/user/app/storage/rpmb_dev \

RPMB_DEV := $(BUILDDIR)/host_tools/rpmb_dev

ATF_DEBUG := 1
ATF_PLAT := qemu
ATF_WITH_TRUSTY_GENERIC_SERVICES := true
ATF_BUILD_BASE := $(abspath $(BUILDDIR)/atf)
ATF_TOOLCHAIN_PREFIX := $(ARCH_arm64_TOOLCHAIN_PREFIX)
ATF_ROOT := external/arm-trusted-firmware
include project/qemu-atf-inc.mk

QEMU_ROOT := external/qemu
QEMU_BUILD_BASE := $(abspath $(BUILDDIR)/qemu-build)
QEMU_ARCH := aarch64
include project/qemu-qemu-inc.mk

LINUX_ARCH ?= arm64
include project/linux-inc.mk

EXTRA_BUILDRULES += external/trusty/bootloader/test-runner/test-runner-inc.mk
TEST_RUNNER_BIN := $(BUILDDIR)/test-runner/test-runner.bin

RUN_QEMU_SCRIPT := $(BUILDDIR)/run-qemu
RUN_SCRIPT := $(BUILDDIR)/run
QEMU_CONFIG := $(BUILDDIR)/config.json
QEMU_PY := $(BUILDDIR)/qemu.py
QEMU_ERROR_PY := $(BUILDDIR)/qemu_error.py
QEMU_OPTIONS_PY := $(BUILDDIR)/qemu_options.py

$(ATF_OUT_DIR):
	mkdir -p $@

# For ATF bootloader semihosting calls, bl32 and bl33 need to be in place
ATF_SYMLINKS := \
	$(ATF_OUT_DIR)/bl32.bin \
	$(ATF_OUT_DIR)/bl33.bin \

$(ATF_OUT_DIR)/bl32.bin: $(BUILDDIR)/lk.bin $(ATF_OUT_DIR)
	ln -sf $(abspath $<) $@

$(ATF_OUT_DIR)/bl33.bin: $(TEST_RUNNER_BIN) $(ATF_OUT_DIR)
	ln -sf $(abspath $<) $@

ATF_OUT_COPIED_FILES := \
	$(ATF_OUT_DIR)/firmware.android.dts \
	$(ATF_OUT_DIR)/run-qemu-helper \

$(ATF_OUT_COPIED_FILES): $(ATF_OUT_DIR)/% : $(PROJECT_QEMU_INC_LOCAL_DIR)/qemu/% $(ATF_OUT_DIR)
	@echo copying $@
	@cp $< $@

$(ATF_OUT_DIR)/RPMB_DATA: ATF_OUT_DIR := $(ATF_OUT_DIR)
$(ATF_OUT_DIR)/RPMB_DATA: $(RPMB_DEV)
	@echo Initialize rpmb device
	$< --dev $(ATF_OUT_DIR)/RPMB_DATA --init --key "ea df 64 44 ea 65 5d 1c 87 27 d4 20 71 0d 53 42 dd 73 a3 38 63 e1 d7 94 c3 72 a6 ea e0 64 64 e6" --size 2048

QEMU_SCRIPTS := \
	$(QEMU_PY) \
	$(QEMU_ERROR_PY) \
	$(QEMU_OPTIONS_PY)

$(QEMU_SCRIPTS): .PHONY
EXTRA_BUILDDEPS += $(QEMU_SCRIPTS)

# Copied so that the resulting build tree contains all files needed to run
$(QEMU_PY): $(PROJECT_QEMU_INC_LOCAL_DIR)/qemu/qemu.py
	@echo copying $@
	@cp $< $@

# Copied so that the resulting build tree contains all files needed to run
$(QEMU_ERROR_PY): $(PROJECT_QEMU_INC_LOCAL_DIR)/qemu/qemu_error.py
	@echo copying $@
	@cp $< $@

# Script used to generate qemu architecture options. Need to specify qemu
# options file name since different projects use different python script
$(QEMU_OPTIONS_PY): $(PROJECT_QEMU_INC_LOCAL_DIR)/qemu/qemu_arm64_options.py
	@echo copying $@
	@cp $< $@

# Save variables to a json file to export paths known to the build system to
# the test system
$(QEMU_CONFIG): QEMU_BIN := $(QEMU_BIN)
$(QEMU_CONFIG): EXTRA_QEMU_FLAGS := ["-machine", "gic-version=$(GIC_VERSION)"]
$(QEMU_CONFIG): ATF_OUT_DIR := $(ATF_OUT_DIR)
$(QEMU_CONFIG): LINUX_BUILD_DIR := $(LINUX_BUILD_DIR)
$(QEMU_CONFIG): ANDROID_PREBUILT := $(abspath trusty/prebuilts/aosp/android)
$(QEMU_CONFIG): RPMB_DEV := $(RPMB_DEV)
$(QEMU_CONFIG): $(ATF_OUT_COPIED_FILES) $(ATF_SYMLINKS) $(ATF_OUT_DIR)/RPMB_DATA
	@echo generating $@
	@echo '{ "linux": "$(LINUX_BUILD_DIR)",' > $@
	@echo '  "linux_arch": "$(LINUX_ARCH)",' >> $@
	@echo '  "atf": "$(ATF_OUT_DIR)", ' >> $@
	@echo '  "qemu": "$(QEMU_BIN)", ' >> $@
	@echo '  "extra_qemu_flags": $(EXTRA_QEMU_FLAGS), ' >> $@
	@echo '  "android": "$(ANDROID_PREBUILT)", ' >> $@
	@echo '  "rpmbd": "$(RPMB_DEV)", ' >> $@
	@echo '  "arch": "$(ARCH)" }' >> $@

EXTRA_BUILDDEPS += $(QEMU_CONFIG)

# Create a wrapper script around run-qemu-helper which defaults arguments to
# those needed to run this build
$(RUN_QEMU_SCRIPT): QEMU_BIN := $(QEMU_BIN)
$(RUN_QEMU_SCRIPT): ATF_OUT_DIR := $(ATF_OUT_DIR)
$(RUN_QEMU_SCRIPT): LINUX_BUILD_DIR := $(LINUX_BUILD_DIR)
$(RUN_QEMU_SCRIPT): $(ATF_OUT_COPIED_FILES) $(ATF_SYMLINKS) $(ATF_OUT_DIR)/RPMB_DATA
	@echo generating $@
	@echo "#!/bin/sh" >$@
	@echo 'cd "$(ATF_OUT_DIR)"' >>$@
	@echo 'KERNEL_DIR="$(LINUX_BUILD_DIR)" QEMU="$(QEMU_BIN)" ./run-qemu-helper "$$@"' >>$@
	@chmod +x $@

EXTRA_BUILDDEPS += $(RUN_QEMU_SCRIPT)

# Create a wrapper around qemu.py which defaults the config
# $(RUN_SCRIPT) will be the device-generic interface called by run-tests
$(RUN_SCRIPT): $(QEMU_SCRIPTS) $(QEMU_CONFIG)
	@echo generating $@
	@echo "#!/bin/sh" >$@
	@echo 'python $(abspath $(QEMU_PY)) -c $(abspath $(QEMU_CONFIG)) "$$@"' >>$@
	@chmod +x $@

EXTRA_BUILDDEPS += $(RUN_SCRIPT)
