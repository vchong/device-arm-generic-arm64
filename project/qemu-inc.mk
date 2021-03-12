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

APPLOADER_ALLOW_NS_CONNECT := true

include project/$(QEMU_TRUSTY_PROJECT).mk

# Derive RPMB key using HKDF
WITH_HKDF_RPMB_KEY ?= true

# Always allow provisioning for emulator builds
STATIC_SYSTEM_STATE_FLAG_PROVISIONING_ALLOWED := 1

MODULES += \
	trusty/user/app/storage/rpmb_dev \

RPMB_DEV := $(BUILDDIR)/host_tools/rpmb_dev

PROJECT_KEYS_DIR := $(PROJECT_QEMU_INC_LOCAL_DIR)/keys

APPLOADER_SIGN_PRIVATE_KEY_0_FILE := \
	$(PROJECT_KEYS_DIR)/apploader_sign_test_private_key_0.der

APPLOADER_SIGN_PUBLIC_KEY_0_FILE := \
	$(PROJECT_KEYS_DIR)/apploader_sign_test_public_key_0.der

APPLOADER_SIGN_PRIVATE_KEY_1_FILE := \
	$(PROJECT_KEYS_DIR)/apploader_sign_test_private_key_1.der

APPLOADER_SIGN_PUBLIC_KEY_1_FILE := \
	$(PROJECT_KEYS_DIR)/apploader_sign_test_public_key_1.der

# The default signing key is key 0, but each application
# can specify a different key identifier
APPLOADER_SIGN_KEY_ID ?= 0

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
QEMU_TARGET := aarch64-softmmu,arm-softmmu
include project/qemu-qemu-inc.mk

LINUX_ARCH ?= arm64
include project/linux-inc.mk

EXTRA_BUILDRULES += external/trusty/bootloader/test-runner/test-runner-inc.mk
TEST_RUNNER_BIN := $(BUILDDIR)/test-runner/test-runner.bin

RUN_QEMU_SCRIPT := $(BUILDDIR)/run-qemu
RUN_SCRIPT := $(BUILDDIR)/run
STOP_SCRIPT := $(BUILDDIR)/stop
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
	$< --dev $(ATF_OUT_DIR)/RPMB_DATA --init --size 2048

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

# Copy Android prebuilts into the build directory so that the build does not
# depend on any files in the source tree. We want to package the build artifacts
# without any dependencies on the sources.
ANDROID_PREBUILT := $(BUILDDIR)/aosp/android
$(ANDROID_PREBUILT): trusty/prebuilts/aosp/android
	@echo copying Android prebuilts
	@$(MKDIR)
	@cp -r $< $@

EXTRA_BUILDDEPS += $(ANDROID_PREBUILT)

# Save variables to a json file to export paths known to the build system to
# the test system
$(QEMU_CONFIG): QEMU_BIN := $(subst $(BUILDDIR)/,,$(QEMU_BIN))
$(QEMU_CONFIG): EXTRA_QEMU_FLAGS := ["-machine", "gic-version=$(GIC_VERSION)"]
$(QEMU_CONFIG): ATF_OUT_DIR := $(subst $(BUILDDIR)/,,$(ATF_OUT_DIR))
$(QEMU_CONFIG): LINUX_BUILD_DIR := $(subst $(BUILDDIR)/,,$(LINUX_BUILD_DIR))
$(QEMU_CONFIG): LINUX_ARCH := $(LINUX_ARCH)
$(QEMU_CONFIG): ANDROID_PREBUILT := $(subst $(BUILDDIR)/,,$(ANDROID_PREBUILT))
$(QEMU_CONFIG): RPMB_DEV := $(subst $(BUILDDIR)/,,$(RPMB_DEV))
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
$(RUN_QEMU_SCRIPT): QEMU_BIN := $(subst $(BUILDDIR)/,,$(QEMU_BIN))
$(RUN_QEMU_SCRIPT): ATF_OUT_DIR := $(subst $(BUILDDIR)/,,$(ATF_OUT_DIR))
$(RUN_QEMU_SCRIPT): LINUX_BUILD_DIR := $(subst $(BUILDDIR)/,,$(LINUX_BUILD_DIR))
$(RUN_QEMU_SCRIPT): $(ATF_OUT_COPIED_FILES) $(ATF_SYMLINKS) $(ATF_OUT_DIR)/RPMB_DATA
	@echo generating $@
	@echo "#!/bin/sh" >$@
	@echo 'SCRIPT_DIR=$$(dirname "$$0")' >>$@
	@echo 'cd "$$SCRIPT_DIR/$(ATF_OUT_DIR)"' >>$@
	@echo 'KERNEL_DIR="$$SCRIPT_DIR/$(LINUX_BUILD_DIR)" QEMU="$$SCRIPT_DIR/$(QEMU_BIN)" ./run-qemu-helper "$$@"' >>$@
	@chmod +x $@

EXTRA_BUILDDEPS += $(RUN_QEMU_SCRIPT)

# Create a wrapper around qemu.py which defaults the config
# $(RUN_SCRIPT) will be the device-generic interface called by run-tests
$(RUN_SCRIPT): QEMU_PY := $(subst $(BUILDDIR)/,,$(QEMU_PY))
$(RUN_SCRIPT): QEMU_CONFIG := $(subst $(BUILDDIR)/,,$(QEMU_CONFIG))
$(RUN_SCRIPT): $(QEMU_SCRIPTS) $(QEMU_CONFIG)
	@echo generating $@
	@echo "#!/bin/sh" >$@
	@echo 'SCRIPT_DIR=$$(dirname "$$0")' >>$@
	@echo 'python2.7 "$$SCRIPT_DIR/$(QEMU_PY)" -c "$$SCRIPT_DIR/$(QEMU_CONFIG)" "$$@"' >>$@
	@chmod +x $@

EXTRA_BUILDDEPS += $(RUN_SCRIPT)

# Create a script to stop all stale emulators.
$(STOP_SCRIPT):
	@echo generating $@
	@echo "#!/bin/sh" >$@
	@echo 'killall qemu-system-aarch64' >>$@
	@chmod +x $@

EXTRA_BUILDDEPS += $(STOP_SCRIPT)

ifeq (true,$(call TOBOOL,$(PACKAGE_QEMU_TRUSTY)))

# Files & directories to copy into QEMU package archive
QEMU_PACKAGE_FILES := \
	$(OUTBIN) $(QEMU_SCRIPTS) $(QEMU_CONFIG) $(RPMB_DEV) \
	$(RUN_SCRIPT) $(RUN_QEMU_SCRIPT) $(STOP_SCRIPT) $(ANDROID_PREBUILT) \
	$(QEMU_BIN) $(ATF_SYMLINKS) $(ATF_OUT_DIR)/bl31.bin \
	$(ATF_OUT_DIR)/RPMB_DATA $(ATF_OUT_COPIED_FILES) $(LINUX_IMAGE) \

# Other files/directories that should be included in the package but which are
# not make targets and therefore cannot be pre-requisites. The target that
# creates these files must be in the QEMU_PACKAGE_FILES variable.
QEMU_PACKAGE_EXTRA_FILES := \
	$(LINUX_BUILD_DIR)/arch $(LINUX_BUILD_DIR)/scripts $(ATF_BUILD_BASE) \
	$(QEMU_BUILD_BASE) \

include project/qemu-package-inc.mk
endif

ANDROID_PREBUILT :=
ATF_BUILD_BASE :=
ATF_OUT_COPIED_FILES :=
ATF_OUT_DIR :=
ATF_SYMLINKS :=
LINUX_ARCH :=
LINUX_BUILD_DIR :=
LINUX_IMAGE :=
RUN_QEMU_SCRIPT :=
RUN_SCRIPT :=
TEST_RUNNER_BIN :=
QEMU_BIN :=
QEMU_BUILD_BASE :=
QEMU_CONFIG :=
QEMU_ERROR_PY :=
QEMU_OPTIONS_PY :=
QEMU_PY :=
QEMU_SCRIPTS :=
